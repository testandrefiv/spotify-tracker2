import os
import time
import secrets
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, date
from typing import List, Optional
import re

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker, Session
from passlib.context import CryptContext
from jose import JWTError, jwt
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from db_models import Base, User, Playlist, Track, StreamHistory, UpdateLog

# Configuration
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./stream_tracker.db")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_urlsafe(32))
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours

# Email Configuration
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD", "")
RECIPIENT_EMAIL = "andre@sevenstudios.se"

# Database Setup
engine = create_engine(
    DATABASE_URL, 
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Security
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# Pydantic Models
class Token(BaseModel):
    access_token: str
    token_type: str

class UserData(BaseModel):
    username: str
    role: str

class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "regular"

class PasswordChange(BaseModel):
    old_password: str
    new_password: str

class PlaylistCreate(BaseModel):
    url: str

class PlaylistUpdate(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None

# Dependencies
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Auth Helpers
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    
    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise credentials_exception
    return user

def get_admin_user(current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return current_user

# ============================================================================
# EMAIL SERVICE
# ============================================================================
def send_daily_summary_email(db: Session):
    """Calculates totals for today and sends an email."""
    print("Preparing daily summary email...")
    
    if not SENDER_EMAIL or not SENDER_PASSWORD:
        print("✗ Email credentials not set. Skipping email.")
        return

    # Get today's stats
    latest_date = db.query(func.max(StreamHistory.date)).scalar()
    
    if not latest_date:
        print("✗ No data found to email.")
        return

    # Aggregate Data
    stats = db.query(
        func.sum(StreamHistory.total_streams).label("total"),
        func.sum(StreamHistory.daily_streams).label("daily"),
        func.sum(StreamHistory.weekly_streams).label("weekly"),
        func.sum(StreamHistory.monthly_streams).label("monthly"),
        func.count(StreamHistory.id).label("tracks")
    ).filter(StreamHistory.date == latest_date).first()

    total_playlists = db.query(Playlist).filter(Playlist.is_active == True).count()

    # Create Email Content
    msg = MIMEMultipart("alternative")
    msg['Subject'] = f"Daily Spotify Stream Update - {latest_date.strftime('%Y-%m-%d')}"
    msg['From'] = SENDER_EMAIL
    msg['To'] = RECIPIENT_EMAIL

    html_content = f"""
    <html>
      <body style="font-family: Arial, sans-serif; color: #333;">
        <h2 style="color: #1DB954;">Spotify Daily Analytics</h2>
        <p>Here are the aggregated stats for <strong>{latest_date.strftime('%Y-%m-%d')}</strong>:</p>
        
        <table style="border-collapse: collapse; width: 100%; max-width: 600px;">
          <tr style="background-color: #f2f2f2;">
            <th style="padding: 10px; border: 1px solid #ddd; text-align: left;">Metric</th>
            <th style="padding: 10px; border: 1px solid #ddd; text-align: right;">Value</th>
          </tr>
          <tr>
            <td style="padding: 10px; border: 1px solid #ddd;">Total Streams</td>
            <td style="padding: 10px; border: 1px solid #ddd; text-align: right;"><strong>{stats.total:,.0f}</strong></td>
          </tr>
          <tr>
            <td style="padding: 10px; border: 1px solid #ddd;">Daily Growth</td>
            <td style="padding: 10px; border: 1px solid #ddd; text-align: right; color: #1DB954;">+{stats.daily:,.0f}</td>
          </tr>
          <tr>
            <td style="padding: 10px; border: 1px solid #ddd;">Weekly Growth (7d)</td>
            <td style="padding: 10px; border: 1px solid #ddd; text-align: right;">+{stats.weekly:,.0f}</td>
          </tr>
          <tr>
            <td style="padding: 10px; border: 1px solid #ddd;">Monthly Growth (30d)</td>
            <td style="padding: 10px; border: 1px solid #ddd; text-align: right;">+{stats.monthly:,.0f}</td>
          </tr>
          <tr>
            <td style="padding: 10px; border: 1px solid #ddd;">Active Playlists</td>
            <td style="padding: 10px; border: 1px solid #ddd; text-align: right;">{total_playlists}</td>
          </tr>
          <tr>
            <td style="padding: 10px; border: 1px solid #ddd;">Tracks Tracked</td>
            <td style="padding: 10px; border: 1px solid #ddd; text-align: right;">{stats.tracks}</td>
          </tr>
        </table>
        
        <p style="font-size: 12px; color: #777; margin-top: 20px;">
          Sent automatically by Spotify Stream Tracker.
        </p>
      </body>
    </html>
    """

    msg.attach(MIMEText(html_content, 'html'))

    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, RECIPIENT_EMAIL, msg.as_string())
        server.quit()
        print(f"✓ Email successfully sent to {RECIPIENT_EMAIL}")
        return True
    except Exception as e:
        print(f"✗ Failed to send email: {e}")
        return False

# Scheduler
scheduler = BackgroundScheduler()

def run_tracker_job():
    """Background job to update all active playlists"""
    print(f"\n{'='*70}")
    print(f"[{datetime.now()}] SCHEDULER: Starting daily update job")
    print(f"{'='*70}\n")
    
    from core_tracker import SpotifyStreamTracker
    
    db = SessionLocal()
    try:
        playlists = db.query(Playlist).filter(Playlist.is_active == True).all()
        
        if not playlists:
            print("No active playlists to update")
            db.add(UpdateLog(
                status="Info",
                message="No active playlists found",
                playlist_name="SYSTEM"
            ))
            db.commit()
            return
        
        print(f"Found {len(playlists)} active playlist(s) to update\n")
        
        for idx, playlist in enumerate(playlists, 1):
            print(f"\n[{idx}/{len(playlists)}] Processing: {playlist.name}")
            print("-" * 60)
            
            try:
                tracker = SpotifyStreamTracker(playlist.url)
                tracker.run_and_save(db, playlist)
                
                db.add(UpdateLog(
                    status="Success",
                    message=f"Successfully updated playlist",
                    playlist_name=playlist.name
                ))
                print(f"✓ {playlist.name} completed successfully")
                
            except Exception as e:
                error_msg = str(e)
                print(f"✗ Error updating {playlist.name}: {error_msg}")
                
                db.add(UpdateLog(
                    status="Failure",
                    message=f"Failed to update playlist",
                    playlist_name=playlist.name,
                    error_details=error_msg
                ))
        
        # === EMAIL INTEGRATION ===
        # Send email after all playlists are processed
        try:
            email_sent = send_daily_summary_email(db)
            db.add(UpdateLog(
                status="Success" if email_sent else "Warning",
                message="Daily Email Sent" if email_sent else "Daily Email Failed",
                playlist_name="SYSTEM"
            ))
        except Exception as e:
            print(f"Critical Email Error: {e}")
            
        db.commit()
        print(f"\n{'='*70}")
        print(f"Daily update job completed at {datetime.now()}")
        print(f"{'='*70}\n")
        
    except Exception as e:
        print(f"\n✗ CRITICAL SCHEDULER ERROR: {e}")
        db.add(UpdateLog(
            status="Failure",
            message=f"Scheduler error: {str(e)}",
            playlist_name="SYSTEM"
        ))
        db.commit()
    finally:
        db.close()

# FastAPI App
app = FastAPI(
    title="Spotify Stream Tracker",
    description="Advanced Spotify playlist stream tracking with data analytics",
    version="2.0"
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount Static Files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Startup & Shutdown
@app.on_event("startup")
def startup_event():
    print("\n" + "="*70)
    print("SPOTIFY STREAM TRACKER - Starting Up")
    print("="*70 + "\n")
    
    # Create tables
    Base.metadata.create_all(bind=engine)
    print("✓ Database tables created/verified")
    
    # Create default admin
    db = SessionLocal()
    try:
        if not db.query(User).filter(User.username == "admin").first():
            admin_user = User(
                username="admin",
                hashed_password=get_password_hash("admin123"),
                role="admin"
            )
            db.add(admin_user)
            db.commit()
            print("✓ Default admin created (username: admin, password: admin123)")
        else:
            print("✓ Admin user already exists")
    finally:
        db.close()
    
    # Start scheduler (runs daily at 3 AM)
    scheduler.add_job(
        run_tracker_job,
        CronTrigger(hour=3, minute=0),
        id='daily_update',
        name='Daily Playlist Update',
        replace_existing=True
    )
    scheduler.start()
    print("✓ Scheduler started (Daily updates at 03:00)")
    
    print("\n" + "="*70)
    print("System Ready!")
    print("="*70 + "\n")

@app.on_event("shutdown")
def shutdown_event():
    scheduler.shutdown()
    print("\n✓ Scheduler stopped")

# ============================================================================
# AUTH ROUTES
# ============================================================================

@app.post("/token", response_model=Token)
async def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(), 
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username}, 
        expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/users/me", response_model=UserData)
async def read_users_me(current_user: User = Depends(get_current_user)):
    return {"username": current_user.username, "role": current_user.role}

# ============================================================================
# USER MANAGEMENT (ADMIN ONLY)
# ============================================================================

@app.get("/api/users")
async def get_all_users(
    db: Session = Depends(get_db), 
    admin: User = Depends(get_admin_user)
):
    users = db.query(User).all()
    return [{
        "id": u.id,
        "username": u.username,
        "role": u.role,
        "created_at": u.created_at.isoformat()
    } for u in users]

@app.post("/api/users", status_code=201)
async def create_user(
    user_data: UserCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    if db.query(User).filter(User.username == user_data.username).first():
        raise HTTPException(status_code=400, detail="Username already exists")
    
    new_user = User(
        username=user_data.username,
        hashed_password=get_password_hash(user_data.password),
        role=user_data.role
    )
    db.add(new_user)
    db.commit()
    return {"message": "User created successfully"}

@app.delete("/api/users/{user_id}")
async def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.username == "admin":
        raise HTTPException(status_code=400, detail="Cannot delete default admin")
    
    db.delete(user)
    db.commit()
    return {"message": "User deleted successfully"}

@app.post("/api/change-password")
async def change_password(
    password_data: PasswordChange,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if not verify_password(password_data.old_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Incorrect old password")
    
    current_user.hashed_password = get_password_hash(password_data.new_password)
    db.commit()
    return {"message": "Password changed successfully"}

# ============================================================================
# PLAYLIST MANAGEMENT
# ============================================================================

@app.post("/api/playlists", status_code=201)
async def add_playlist(
    playlist: PlaylistCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    match = re.search(r'playlist/([a-zA-Z0-9]+)', playlist.url)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid Spotify playlist URL")
    
    spotify_id = match.group(1)
    
    if db.query(Playlist).filter(Playlist.spotify_id == spotify_id).first():
        raise HTTPException(status_code=400, detail="Playlist already exists")
    
    # Fetch playlist name from API
    try:
        from core_tracker import SpotifyStreamTracker
        tracker = SpotifyStreamTracker(playlist.url)
        if tracker.setup_spotipy():
            playlist_data = tracker.sp.playlist(spotify_id)
            playlist_name = playlist_data['name']
        else:
            playlist_name = f"Playlist {spotify_id}"
    except:
        playlist_name = f"Playlist {spotify_id}"
    
    new_playlist = Playlist(
        spotify_id=spotify_id,
        name=playlist_name,
        url=playlist.url
    )
    db.add(new_playlist)
    db.commit()
    return {"message": f"Playlist '{playlist_name}' added successfully"}

@app.get("/api/playlists")
async def get_playlists(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    playlists = db.query(Playlist).all()
    return [{
        "id": p.id,
        "name": p.name,
        "url": p.url,
        "spotify_id": p.spotify_id,
        "is_active": p.is_active,
        "last_updated": p.last_updated.isoformat() if p.last_updated else None,
        "track_count": len(p.tracks)
    } for p in playlists]

@app.put("/api/playlists/{playlist_id}")
async def update_playlist(
    playlist_id: int,
    update_data: PlaylistUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    playlist = db.query(Playlist).filter(Playlist.id == playlist_id).first()
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    
    if update_data.name is not None:
        playlist.name = update_data.name
    if update_data.is_active is not None:
        playlist.is_active = update_data.is_active
    
    db.commit()
    return {"message": "Playlist updated successfully"}

@app.delete("/api/playlists/{playlist_id}")
async def delete_playlist(
    playlist_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    playlist = db.query(Playlist).filter(Playlist.id == playlist_id).first()
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    
    db.delete(playlist)
    db.commit()
    return {"message": "Playlist deleted successfully"}

# ============================================================================
# DATA ENDPOINTS
# ============================================================================

@app.get("/api/summary")
async def get_summary_data(
    playlist_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    latest_date = db.query(func.max(StreamHistory.date)).scalar()
    if not latest_date:
        return {"tracks": [], "playlist_totals": [], "overall_total": {}}
    
    query = db.query(StreamHistory).join(Track).join(Playlist).filter(
        StreamHistory.date == latest_date
    )
    
    if playlist_id:
        query = query.filter(Playlist.id == playlist_id)
    
    results = query.all()
    
    # Track-level data
    tracks = [{
        "track": item.track.name,
        "artist": item.track.artist,
        "playlist": item.track.playlist.name,
        "playlist_id": item.track.playlist.id,
        "total": item.total_streams,
        "daily": item.daily_streams,
        "weekly": item.weekly_streams,
        "monthly": item.monthly_streams,
        "status": "imputed" if item.is_imputed else ("reset" if item.is_reset else ("new" if item.is_new else ("hidden" if item.is_hidden else "ok")))
    } for item in results]
    
    # Calculate playlist-wise totals
    playlist_totals = {}
    for item in results:
        pid = item.track.playlist.id
        pname = item.track.playlist.name
        
        if pid not in playlist_totals:
            playlist_totals[pid] = {
                "playlist_id": pid,
                "playlist_name": pname,
                "total_streams": 0,
                "daily_streams": 0,
                "weekly_streams": 0,
                "monthly_streams": 0,
                "track_count": 0
            }
        
        playlist_totals[pid]["total_streams"] += item.total_streams
        playlist_totals[pid]["daily_streams"] += item.daily_streams
        playlist_totals[pid]["weekly_streams"] += item.weekly_streams
        playlist_totals[pid]["monthly_streams"] += item.monthly_streams
        playlist_totals[pid]["track_count"] += 1
    
    # Calculate overall total across all playlists
    overall_total = {
        "total_streams": sum(p["total_streams"] for p in playlist_totals.values()),
        "daily_streams": sum(p["daily_streams"] for p in playlist_totals.values()),
        "weekly_streams": sum(p["weekly_streams"] for p in playlist_totals.values()),
        "monthly_streams": sum(p["monthly_streams"] for p in playlist_totals.values()),
        "total_tracks": sum(p["track_count"] for p in playlist_totals.values()),
        "total_playlists": len(playlist_totals)
    }
    
    return {
        "tracks": tracks,
        "playlist_totals": list(playlist_totals.values()),
        "overall_total": overall_total
    }

@app.get("/api/sheets_view")
async def get_sheets_view(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Returns data organized by playlist in a sheet-like format.
    Each playlist gets its own 'sheet' with all its tracks and totals.
    """
    latest_date = db.query(func.max(StreamHistory.date)).scalar()
    if not latest_date:
        return []
    
    playlists = db.query(Playlist).all()
    sheets = []
    
    for playlist in playlists:
        results = db.query(StreamHistory).join(Track).filter(
            Track.playlist_id == playlist.id,
            StreamHistory.date == latest_date
        ).all()
        
        if not results:
            continue
        
        tracks = [{
            "track": item.track.name,
            "artist": item.track.artist,
            "spotify_id": item.track.spotify_id,
            "url": item.track.url,
            "total": item.total_streams,
            "daily": item.daily_streams,
            "weekly": item.weekly_streams,
            "monthly": item.monthly_streams,
            "status": "imputed" if item.is_imputed else ("reset" if item.is_reset else ("new" if item.is_new else ("hidden" if item.is_hidden else "ok")))
        } for item in results]
        
        # Calculate totals for this playlist
        totals = {
            "total_streams": sum(t["total"] for t in tracks),
            "daily_streams": sum(t["daily"] for t in tracks),
            "weekly_streams": sum(t["weekly"] for t in tracks),
            "monthly_streams": sum(t["monthly"] for t in tracks),
            "track_count": len(tracks)
        }
        
        sheets.append({
            "playlist_id": playlist.id,
            "playlist_name": playlist.name,
            "playlist_url": playlist.url,
            "spotify_id": playlist.spotify_id,
            "is_active": playlist.is_active,
            "last_updated": playlist.last_updated.isoformat() if playlist.last_updated else None,
            "tracks": tracks,
            "totals": totals
        })
    
    return sheets

@app.get("/api/full_data")
async def get_full_data(
    playlist_id: Optional[int] = None,
    limit: int = 2000,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(StreamHistory).join(Track).join(Playlist)
    
    if playlist_id:
        query = query.filter(Playlist.id == playlist_id)
    
    history = query.order_by(StreamHistory.date.desc()).limit(limit).all()
    
    return [{
        "date": h.date.strftime("%Y-%m-%d"),
        "track": h.track.name,
        "artist": h.track.artist,
        "playlist": h.track.playlist.name,
        "streams": h.total_streams,
        "change": h.daily_streams,
        "weekly": h.weekly_streams,
        "monthly": h.monthly_streams,
        "is_imputed": h.is_imputed,
        "is_reset": h.is_reset,
        "is_new": h.is_new,
        "is_hidden": h.is_hidden
    } for h in history]

@app.get("/api/track_history/{track_id}")
async def get_track_history(
    track_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    history = db.query(StreamHistory).filter(
        StreamHistory.track_id == track_id
    ).order_by(StreamHistory.date.asc()).all()
    
    return [{
        "date": h.date.strftime("%Y-%m-%d"),
        "total_streams": h.total_streams,
        "daily_streams": h.daily_streams
    } for h in history]

@app.get("/api/stats")
async def get_system_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    total_playlists = db.query(Playlist).count()
    active_playlists = db.query(Playlist).filter(Playlist.is_active == True).count()
    total_tracks = db.query(Track).count()
    total_history = db.query(StreamHistory).count()
    last_update = db.query(func.max(StreamHistory.date)).scalar()
    
    return {
        "total_playlists": total_playlists,
        "active_playlists": active_playlists,
        "total_tracks": total_tracks,
        "total_records": total_history,
        "last_update": last_update.isoformat() if last_update else None
    }

# ============================================================================
# ADMIN ACTIONS
# ============================================================================

@app.post("/api/force_update")
async def force_update(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    scheduler.add_job(
        run_tracker_job,
        'date',
        run_date=datetime.now() + timedelta(seconds=2),
        id=f'manual_update_{int(time.time())}',
        name='Manual Update'
    )
    
    db.add(UpdateLog(
        status="Info",
        message="Manual update triggered by admin",
        playlist_name="SYSTEM"
    ))
    db.commit()
    
    return {"message": "Update job triggered. Check logs for progress."}

@app.get("/api/logs")
async def get_logs(
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    logs = db.query(UpdateLog).order_by(
        UpdateLog.timestamp.desc()
    ).limit(limit).all()
    
    return [{
        "id": log.id,
        "timestamp": log.timestamp.isoformat(),
        "status": log.status,
        "message": log.message,
        "playlist_name": log.playlist_name,
        "error_details": log.error_details
    } for log in logs]

# ============================================================================
# HTML PAGES
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def read_root():
    file_path = os.path.join("static", "login.html")
    if not os.path.exists(file_path):
        return HTMLResponse("<h1>login.html not found</h1>", status_code=404)
    with open(file_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/dashboard", response_class=HTMLResponse)
async def read_dashboard():
    file_path = os.path.join("static", "dashboard.html")
    if not os.path.exists(file_path):
        return HTMLResponse("<h1>dashboard.html not found</h1>", status_code=404)
    with open(file_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
