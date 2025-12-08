from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Date
from sqlalchemy.orm import relationship, declarative_base
from datetime import datetime

Base = declarative_base()

class User(Base):
    """
    User management for Admin and Regular users.
    """
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(String, default="regular", nullable=False)  # 'admin' or 'regular'
    created_at = Column(DateTime, default=datetime.utcnow)

class Playlist(Base):
    """
    Stores Spotify playlists to be tracked.
    """
    __tablename__ = "playlists"
    id = Column(Integer, primary_key=True, index=True)
    spotify_id = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=False)
    custom_name = Column(String, nullable=True)  # Added for frontend display
    url = Column(String, nullable=False)
    is_active = Column(Boolean, default=True)
    last_updated = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # NEW: Update status tracking (columns already exist in PostgreSQL)
    update_status = Column(String, default="idle")  # "idle", "updating", "completed", "failed"
    update_started_at = Column(DateTime, nullable=True)
    update_completed_at = Column(DateTime, nullable=True)
    last_successful_update = Column(DateTime, nullable=True)
    
    tracks = relationship("Track", back_populates="playlist", cascade="all, delete-orphan")

class Track(Base):
    """
    Stores metadata for individual tracks.
    """
    __tablename__ = "tracks"
    id = Column(Integer, primary_key=True, index=True)
    
    # --- FIX IS HERE: Removed 'unique=True' ---
    spotify_id = Column(String, index=True, nullable=False)
    
    name = Column(String, nullable=False)
    artist = Column(String, nullable=False)
    url = Column(String)
    
    playlist_id = Column(Integer, ForeignKey("playlists.id"))
    playlist = relationship("Playlist", back_populates="tracks")
    
    stream_history = relationship("StreamHistory", back_populates="track", cascade="all, delete-orphan")

class StreamHistory(Base):
    """
    Daily snapshots of stream counts with all data rules implemented.
    """
    __tablename__ = "stream_history"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, nullable=False, index=True)
    
    track_id = Column(Integer, ForeignKey("tracks.id"), index=True)
    track = relationship("Track", back_populates="stream_history")
    
    # Raw total scraped from Spotify
    total_streams = Column(Integer, nullable=False)
    
    # Calculated fields
    daily_streams = Column(Integer, default=0)
    weekly_streams = Column(Integer, default=0)
    monthly_streams = Column(Integer, default=0)
    
    # Data rule flags
    is_imputed = Column(Boolean, default=False)  # Missing data filled mathematically
    is_reset = Column(Boolean, default=False)    # Streams decreased (reset)
    is_new = Column(Boolean, default=False)      # First appearance
    is_hidden = Column(Boolean, default=False)   # Stream count not visible
    
    # NEW: Scraping metadata (columns already exist in PostgreSQL)
    is_simulated = Column(Boolean, default=False)  # Estimated from historical data
    scrape_method = Column(String, nullable=True)  # "requests", "selenium", "simulated"
    confidence_score = Column(Integer, nullable=True)  # 0-100 quality score
    recorded_at = Column(DateTime, default=datetime.utcnow)  # When record was created

class UpdateLog(Base):
    """
    Logs for scheduler and manual updates.
    """
    __tablename__ = "update_logs"
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    status = Column(String, nullable=False)  # 'Success' or 'Failure'
    message = Column(String)
    playlist_name = Column(String, nullable=True)

    error_details = Column(String, nullable=True)
