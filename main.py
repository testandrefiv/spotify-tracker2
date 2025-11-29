import os
import smtplib
import logging
from datetime import datetime, date
from typing import List, Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from fastapi import FastAPI, Depends, HTTPException, status, Request, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import create_engine, desc, func, and_
from sqlalchemy.orm import sessionmaker
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# Import models and core logic
from db_models import Base, User, Playlist, Track, StreamHistory, UpdateLog
from core_tracker import SpotifyStreamTracker
from auth import (
    verify_password, get_password_hash, create_access_token, 
    get_current_user, Token
)

# --- CONFIGURATION ---
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Spotify Stream Tracker")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="static")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- PYDANTIC MODELS ---
class PlaylistCreate(BaseModel):
    url: str

class PlaylistRename(BaseModel):
    custom_name: str

class PlaylistResponse(BaseModel):
    id: int
    name: str
    custom_name: Optional[str]
    status: str
    url: str
    is_active: bool
    last_updated: Optional[datetime]
    track_count: int

# --- EMAIL LOGIC (Internal) ---
def get_stats_string(db: Session):
    today = date.today()
    playlists = db.query(Playlist).all()
    total_streams_all = 0
    total_tracks_all = db.query(Track).count()
    
    # Calculate Total Streams across all playlists
    subquery = db.query(
        StreamHistory.track_id,
        func.max(StreamHistory.date).label('max_date')
    ).group_by(StreamHistory.track_id).subquery()
    
    latest_streams = db.query(func.sum(StreamHistory.total_streams)).join(
        subquery, and_(StreamHistory.track_id == subquery.c.track_id, StreamHistory.date == subquery.c.max_date)
    ).scalar()
    
    if latest_streams: total_streams_all = latest_streams

    msg = f"SPOTIFY TRACKER REPORT - {today}\n"
    msg += f"================================\n"
    msg += f"Overall Total: {total_streams_all:,} streams\n"
    msg += f"Total Tracks:  {total_tracks_all}\n"
    msg += f"================================\n\n"
    msg += "Playlist Breakdown:\n"
    
    for p in playlists:
        tracks = db.query(Track).filter(Track.playlist_id == p.id).all()
        p_total = 0
        for t in tracks:
            last_entry = db.query(StreamHistory).filter(
                StreamHistory.track_id == t.id
            ).order_by(desc(StreamHistory.date)).first()
            if last_entry: p_total += last_entry.total_streams
            
        display_name = p.custom_name if p.custom_name else p.name
        msg += f"- {display_name}: {p_total:,} streams ({len(tracks)} tracks)\n"
    return msg

def send_daily_summary_email(db: Session):
    sender_email = os.getenv("SENDER_EMAIL")
    sender_password = os.getenv("SENDER_PASSWORD")
    smtp_server = os.getenv("SMTP_SERVER", "send.one.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    
    if not sender_email or not sender_password: return False

    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = sender_email
    msg['Subject'] = f"Spotify Tracker Daily Summary - {date.today()}"
    msg.attach(MIMEText(get_stats_string(db), 'plain'))

    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, sender_email, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        raise e

# --- SCHEDULER LOGIC (Internal) ---
def run_tracker_job():
    db = SessionLocal()
    try:
        print("SCHEDULER: Starting daily update job")
        playlists = db.query(Playlist).filter(Playlist.is_active == True).all()
        
        for p in playlists:
            try:
                tracker = SpotifyStreamTracker(p.url)
                tracker.run_and_save(db, p)
            except Exception as e:
                print(f"Failed to update playlist {p.name}: {e}")
                # Mark as failed in DB if crashed
                p.status = "Failed"
                db.commit()
                continue
                
        try:
            send_daily_summary_email(db)
            db.add(UpdateLog(status="Success", message="Daily Email Sent", playlist_name="SYSTEM"))
            db.commit()
        except Exception as e:
            db.add(UpdateLog(status="Warning", message=f"Email Failed: {e}", playlist_name="SYSTEM"))
            db.commit()
            
    except Exception as e:
        print(f"CRITICAL JOB FAILURE: {e}")
    finally:
        db.close()

def start_scheduler():
    scheduler = BackgroundScheduler()
    # scheduled for 3:00 AM UTC
    scheduler.add_job(run_tracker_job, CronTrigger(hour=3, minute=0), id="daily_update", replace_existing=True)
    scheduler.start()
    print("✓ Scheduler started (Daily updates at 03:00)")

def run_single_playlist_job(playlist_id: int):
    db = SessionLocal()
    try:
        p = db.query(Playlist).filter(Playlist.id == playlist_id).first()
        if p:
            tracker = SpotifyStreamTracker(p.url)
            tracker.run_and_save(db, p)
    finally:
        db.close()

# --- ROUTES ---

@app.on_event("startup")
def startup_event():
    # Setup Admin
    db = SessionLocal()
    user = db.query(User).filter(User.username == "admin").first()
    if not user:
        admin_user = User(username="admin", hashed_password=get_password_hash("admin123"), role="admin")
        db.add(admin_user)
        db.commit()
    # Start Timer
    start_scheduler()
    db.close()

@app.post("/token", response_model=Token)
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    access_token = create_access_token(data={"sub": user.username})
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})

@app.get("/users/me")
async def read_users_me(current_user: User = Depends(get_current_user)):
    return {"username": current_user.username, "role": current_user.role}

@app.get("/api/playlists", response_model=List[PlaylistResponse])
def get_playlists(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    playlists = db.query(Playlist).order_by(Playlist.created_at).all()
    results = []
    for p in playlists:
        track_count = db.query(Track).filter(Track.playlist_id == p.id).count()
        results.append({
            "id": p.id,
            "name": p.name,
            "custom_name": p.custom_name,
            "status": p.status,
            "url": p.url,
            "is_active": p.is_active,
            "last_updated": p.last_updated,
            "track_count": track_count
        })
    return results

@app.post("/api/playlists")
def add_playlist(playlist: PlaylistCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    tracker = SpotifyStreamTracker(playlist.url)
    if not tracker.playlist_id: raise HTTPException(status_code=400, detail="Invalid Spotify URL")
    if not tracker.setup_spotipy(): raise HTTPException(status_code=500, detail="Spotify API Error")
    try:
        p_data = tracker.sp.playlist(tracker.playlist_id)
        name = p_data['name']
    except: name = "Unknown Playlist"
    
    new_playlist = Playlist(spotify_id=tracker.playlist_id, name=name, url=playlist.url, status="Idle")
    db.add(new_playlist)
    db.commit()
    # Trigger background update
    background_tasks.add_task(run_single_playlist_job, new_playlist.id)
    return {"message": "Playlist added", "name": name}

@app.put("/api/playlists/{playlist_id}/rename")
def rename_playlist(playlist_id: int, rename_data: PlaylistRename, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    playlist = db.query(Playlist).filter(Playlist.id == playlist_id).first()
    if not playlist: raise HTTPException(status_code=404, detail="Playlist not found")
    playlist.custom_name = rename_data.custom_name
    db.commit()
    return {"message": "Playlist renamed successfully"}

@app.delete("/api/playlists/{playlist_id}")
def delete_playlist(playlist_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    playlist = db.query(Playlist).filter(Playlist.id == playlist_id).first()
    if not playlist: raise HTTPException(status_code=404, detail="Playlist not found")
    db.delete(playlist)
    db.commit()
    return {"message": "Playlist deleted"}

@app.get("/api/summary")
def get_summary(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    total_playlists = db.query(Playlist).count()
    total_tracks = db.query(Track).count()
    total_streams = 0
    subquery = db.query(StreamHistory.track_id, func.max(StreamHistory.date).label('max_date')).group_by(StreamHistory.track_id).subquery()
    latest_streams = db.query(func.sum(StreamHistory.total_streams)).join(subquery, and_(StreamHistory.track_id == subquery.c.track_id, StreamHistory.date == subquery.c.max_date)).scalar()
    if latest_streams: total_streams = latest_streams
    return {"total_playlists": total_playlists, "total_tracks": total_tracks, "total_streams": total_streams}

@app.post("/api/force_update")
def force_update(background_tasks: BackgroundTasks, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    background_tasks.add_task(run_tracker_job)
    return {"message": "Update job triggered in background"}

@app.get("/api/full_data")
def get_full_data(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    playlists = db.query(Playlist).all()
    data = []
    for p in playlists:
        tracks = db.query(Track).filter(Track.playlist_id == p.id).all()
        track_list = []
        playlist_total = 0
        for t in tracks:
            history = db.query(StreamHistory).filter(StreamHistory.track_id == t.id).order_by(desc(StreamHistory.date)).first()
            if history:
                track_list.append({
                    "name": t.name,
                    "artist": t.artist,
                    "total": history.total_streams,
                    "daily": history.daily_streams,
                    "weekly": history.weekly_streams,
                    "monthly": history.monthly_streams,
                    "status": "New" if history.is_new else "Reset" if history.is_reset else "OK",
                    "url": t.url
                })
                playlist_total += history.total_streams
            else:
                track_list.append({"name": t.name, "artist": t.artist, "total": 0, "daily": 0, "weekly": 0, "monthly": 0, "status": "Pending", "url": t.url})
        
        # This part ensures we send the Custom Name and Status to the frontend
        data.append({
            "playlist_name": p.custom_name if p.custom_name else p.name, 
            "playlist_id": p.id, 
            "status": p.status, 
            "track_count": len(tracks), 
            "total_streams": playlist_total, 
            "tracks": track_list
        })
    return data

@app.get("/api/logs")
def get_logs(limit: int = 50, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    logs = db.query(UpdateLog).order_by(desc(UpdateLog.timestamp)).limit(limit).all()
    return logs
