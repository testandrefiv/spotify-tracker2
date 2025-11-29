import os
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from db_models import Playlist, UpdateLog
from core_tracker import SpotifyStreamTracker

# Setup Database Connection
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def run_tracker_job():
    """
    The main logic that runs every day or when Force Update is clicked.
    """
    db = SessionLocal()
    try:
        print("======================================================================")
        print("SCHEDULER: Starting daily update job")
        print("======================================================================")
        
        # 1. Get Active Playlists
        playlists = db.query(Playlist).filter(Playlist.is_active == True).all()
        print(f"\nFound {len(playlists)} active playlist(s) to update\n")
        
        # 2. Scrape Each Playlist
        for p in playlists:
            try:
                tracker = SpotifyStreamTracker(p.url)
                tracker.run_and_save(db, p)
            except Exception as e:
                print(f"Failed to update playlist {p.name}: {e}")
                continue
            
        # 3. Send Email Summary
        try:
            # We import inside the function to avoid circular error
            from email_sender import send_daily_summary_email
            print("Preparing daily summary email...")
            send_daily_summary_email(db)
            
            db.add(UpdateLog(status="Success", message="Daily Email Sent", playlist_name="SYSTEM"))
            db.commit()
            print("✓ Email successfully sent")
            
        except Exception as e:
            print(f"✗ Failed to send email: {e}")
            db.add(UpdateLog(status="Warning", message=f"Email Failed: {e}", playlist_name="SYSTEM"))
            db.commit()

    except Exception as e:
        print(f"CRITICAL JOB FAILURE: {e}")
    finally:
        db.close()

def start_scheduler():
    """
    Starts the background timer.
    """
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        run_tracker_job,
        CronTrigger(hour=3, minute=0), # Runs at 3:00 AM UTC
        id="daily_update",
        replace_existing=True
    )
    scheduler.start()
    print("✓ Scheduler started (Daily updates at 03:00)")
