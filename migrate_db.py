"""
Quick migration script to add new columns to existing PostgreSQL database
Run this once to update your schema without losing data
"""

import os
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./stream_tracker.db")

print(f"Connecting to database...")
engine = create_engine(DATABASE_URL)

# SQL to add new columns (PostgreSQL will skip if they already exist)
migrations = [
    # Playlist table additions
    "ALTER TABLE playlists ADD COLUMN IF NOT EXISTS update_status VARCHAR DEFAULT 'idle' NOT NULL;",
    "ALTER TABLE playlists ADD COLUMN IF NOT EXISTS update_started_at TIMESTAMP;",
    "ALTER TABLE playlists ADD COLUMN IF NOT EXISTS update_completed_at TIMESTAMP;",
    "ALTER TABLE playlists ADD COLUMN IF NOT EXISTS last_successful_update TIMESTAMP;",
    
    # StreamHistory table additions
    "ALTER TABLE stream_history ADD COLUMN IF NOT EXISTS is_simulated BOOLEAN DEFAULT FALSE;",
    "ALTER TABLE stream_history ADD COLUMN IF NOT EXISTS scrape_method VARCHAR;",
    "ALTER TABLE stream_history ADD COLUMN IF NOT EXISTS confidence_score FLOAT;",
    "ALTER TABLE stream_history ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;",
]

try:
    with engine.connect() as conn:
        for i, migration in enumerate(migrations, 1):
            print(f"[{i}/{len(migrations)}] Executing: {migration[:70]}...")
            conn.execute(text(migration))
            conn.commit()
    
    print("\n✅ Database migration completed successfully!")
    print("All new columns added without affecting existing data.")
    
except Exception as e:
    print(f"\n❌ Error during migration: {e}")
    print("\nIf using SQLite, run: python main.py (it will auto-create columns)")
    print("If using PostgreSQL, ensure you have ALTER TABLE permissions.")
