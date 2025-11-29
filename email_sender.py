import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from sqlalchemy.orm import Session
from sqlalchemy import desc, func, and_
from datetime import date
from db_models import Playlist, Track, StreamHistory

def get_stats_string(db: Session):
    """
    Generates a text summary of the day's stats.
    """
    today = date.today()
    playlists = db.query(Playlist).all()
    
    total_streams_all = 0
    total_tracks_all = db.query(Track).count()
    
    # Calculate Total Streams
    subquery = db.query(
        StreamHistory.track_id,
        func.max(StreamHistory.date).label('max_date')
    ).group_by(StreamHistory.track_id).subquery()
    
    latest_streams = db.query(func.sum(StreamHistory.total_streams)).join(
        subquery,
        and_(
            StreamHistory.track_id == subquery.c.track_id,
            StreamHistory.date == subquery.c.max_date
        )
    ).scalar()
    
    if latest_streams:
        total_streams_all = latest_streams

    msg = f"SPOTIFY TRACKER REPORT - {today}\n"
    msg += f"================================\n"
    msg += f"Overall Total: {total_streams_all:,} streams\n"
    msg += f"Total Tracks:  {total_tracks_all}\n"
    msg += f"================================\n\n"
    
    msg += "Playlist Breakdown:\n"
    
    for p in playlists:
        # Get tracks for this playlist
        tracks = db.query(Track).filter(Track.playlist_id == p.id).all()
        
        # Calculate playlist total
        p_total = 0
        for t in tracks:
            last_entry = db.query(StreamHistory).filter(
                StreamHistory.track_id == t.id
            ).order_by(desc(StreamHistory.date)).first()
            if last_entry:
                p_total += last_entry.total_streams
        
        display_name = p.custom_name if p.custom_name else p.name
        msg += f"- {display_name}: {p_total:,} streams ({len(tracks)} tracks)\n"
        
    return msg

def send_daily_summary_email(db: Session):
    sender_email = os.getenv("SENDER_EMAIL")
    sender_password = os.getenv("SENDER_PASSWORD")
    smtp_server = os.getenv("SMTP_SERVER", "send.one.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    
    if not sender_email or not sender_password:
        print("Skipping email: SENDER_EMAIL or SENDER_PASSWORD not set.")
        return False

    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = sender_email
    msg['Subject'] = f"Spotify Tracker Daily Summary - {date.today()}"

    body = get_stats_string(db)
    msg.attach(MIMEText(body, 'plain'))

    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(sender_email, sender_password)
        text = msg.as_string()
        server.sendmail(sender_email, sender_email, text)
        server.quit()
        return True
    except Exception as e:
        raise e
