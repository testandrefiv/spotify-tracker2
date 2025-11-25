import re
import traceback
import time
from datetime import datetime, timedelta, date 
from typing import List, Dict, Optional

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from sqlalchemy.orm import Session
from sqlalchemy import desc, and_

from db_models import Playlist, Track, StreamHistory

# Spotify API Credentials
CLIENT_ID = "a2960e69a9ec414bb708bf002b224b25"
CLIENT_SECRET = "9622dfa07b3745de8b60de42775bd356"

class SpotifyStreamTracker:
    def __init__(self, playlist_url: str):
        self.playlist_url = playlist_url
        self.playlist_id = self._parse_playlist_id(playlist_url)
        self.sp = None
        self.driver = None
        self.tracks_data = []

    def _parse_playlist_id(self, url):
        match = re.search(r'playlist/([a-zA-Z0-9]+)', url)
        return match.group(1) if match else None

    def setup_spotipy(self):
        try:
            auth_manager = SpotifyClientCredentials(
                client_id=CLIENT_ID, 
                client_secret=CLIENT_SECRET
            )
            self.sp = spotipy.Spotify(auth_manager=auth_manager)
            print("✓ Spotify API authenticated")
            return True
        except Exception as e:
            print(f"Spotipy Auth Error: {e}")
            return False

    def setup_driver(self):
        options = Options()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        options.add_experimental_option('useAutomationExtension', False)
        options.add_argument("log-level=3")
        
        try:
            self.driver = webdriver.Chrome(options=options)
            self.driver.set_page_load_timeout(60)
            
            # Execute stealth scripts
            self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": """
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    })
                """
            })
            
            print("✓ WebDriver initialized")
            return True
        except Exception as e:
            print(f"WebDriver Error: {e}")
            return False

    def fetch_tracks_api(self):
        """Fetch track metadata via Spotify API"""
        print(f"Fetching API data for playlist {self.playlist_id}...")
        tracks = []
        try:
            results = self.sp.playlist_items(self.playlist_id, limit=100)
            items = results['items']
            
            while results['next']:
                results = self.sp.next(results)
                items.extend(results['items'])
            
            for item in items:
                track = item.get('track')
                if track and track.get('id'):
                    tracks.append({
                        'spotify_id': track['id'],
                        'name': track['name'],
                        'artist': track['artists'][0]['name'] if track['artists'] else "Unknown",
                        'url': track['external_urls']['spotify']
                    })
            
            print(f"✓ Found {len(tracks)} tracks via API")
            return tracks
        except Exception as e:
            print(f"API Fetch Error: {e}")
            traceback.print_exc()
            return []

    def _extract_stream_count_helper(self, text):
        """
        Extract numeric stream count from text (handles K, M, B suffixes)
        """
        if not text:
            return None
        
        text = text.strip().replace(',', '').lower()
        
        # Look for patterns like "123.4M" or "1.2B" or "456K"
        match = re.search(r'([\d\.]+)\s*([kmb])?', text)
        if match:
            number = float(match.group(1))
            suffix = match.group(2)
            
            if suffix == 'k':
                return int(number * 1_000)
            elif suffix == 'm':
                return int(number * 1_000_000)
            elif suffix == 'b':
                return int(number * 1_000_000_000)
            else:
                try:
                    return int(number)
                except ValueError:
                    return int(round(number))
        
        return None

    def _is_reasonable_stream_count(self, count):
        """
        Validate if a number looks like a reasonable stream count.
        Filters out timestamps, dates, and other metadata.
        """
        if count is None or count < 1000:
            return False
        
        # Filter out Unix timestamps (too large)
        # 1,609,946,000,000,000 is clearly a timestamp in milliseconds
        if count > 100_000_000_000:  # More than 100 billion is likely a timestamp
            return False
        
        # Reasonable max for any single track (less than 10 billion)
        if count > 10_000_000_000:
            return False
        
        return True

    def scrape_stream_count(self, url, track_name):
        """
        Scrapes ONLY from data-testid='playcount' elements.
        Ignores all other numbers on the page to avoid false matches.
        """
        original_window = self.driver.current_window_handle
        
        for attempt in range(2):
            try:
                # Open track in new tab
                self.driver.execute_script("window.open('');")
                self.driver.switch_to.window(self.driver.window_handles[-1])
                self.driver.get(url)
                
                # Wait for page to fully load
                time.sleep(2.5)
                
                # === PRIMARY STRATEGY: ONLY use data-testid='playcount' elements ===
                try:
                    # Wait for elements to be present
                    WebDriverWait(self.driver, 5).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='playcount']"))
                    )
                    
                    playcount_elements = self.driver.find_elements(By.CSS_SELECTOR, "[data-testid='playcount']")
                    
                    if playcount_elements:
                        print(f"  → Found {len(playcount_elements)} playcount element(s)")
                        
                        for idx, elem in enumerate(playcount_elements):
                            try:
                                text = elem.text.strip()
                                print(f"    Element {idx+1}: '{text}'")
                                
                                # Only extract if text contains digits
                                if text and any(c.isdigit() for c in text):
                                    streams = self._extract_stream_count_helper(text)
                                    
                                    # Validate it's a reasonable stream count
                                    if streams and self._is_reasonable_stream_count(streams):
                                        print(f"  ✓ {track_name}: {streams:,} streams")
                                        self.driver.close()
                                        self.driver.switch_to.window(original_window)
                                        return streams
                                    elif streams:
                                        print(f"    → Rejected {streams:,} (likely timestamp/metadata)")
                                        
                            except Exception as elem_error:
                                print(f"    → Error reading element {idx+1}: {elem_error}")
                                continue
                                
                except Exception as e:
                    print(f"  → No playcount elements found: {e}")

                # === FALLBACK: JavaScript extraction from playcount elements only ===
                try:
                    js_script = """
                    let elements = document.querySelectorAll('[data-testid="playcount"]');
                    let results = [];
                    
                    for (let elem of elements) {
                        let text = elem.textContent.trim();
                        if (text && /\\d/.test(text)) {
                            results.push(text);
                        }
                    }
                    
                    return results.length > 0 ? results : null;
                    """
                    
                    result = self.driver.execute_script(js_script)
                    if result:
                        print(f"  → JS found {len(result)} playcount text(s): {result}")
                        for text in result:
                            streams = self._extract_stream_count_helper(text)
                            if streams and self._is_reasonable_stream_count(streams):
                                print(f"  ✓ {track_name}: {streams:,} streams (JS extraction)")
                                self.driver.close()
                                self.driver.switch_to.window(original_window)
                                return streams
                            elif streams:
                                print(f"    → Rejected {streams:,} (unreasonable)")
                except Exception as js_error:
                    print(f"  → JavaScript extraction failed: {js_error}")

                # Close tab and retry
                self.driver.close()
                self.driver.switch_to.window(original_window)
                
                if attempt < 1:
                    print(f"  → Retry in 1 second...")
                    time.sleep(1)

            except Exception as e:
                try:
                    if len(self.driver.window_handles) > 1:
                        self.driver.close()
                    self.driver.switch_to.window(original_window)
                except:
                    pass
                print(f"  ⚠ Attempt {attempt+1} error: {type(e).__name__}")

        print(f"  ✗ {track_name}: Could not find valid stream count")
        return 0

    def calculate_aggregates(self, db: Session, track_id: int, today_daily: int, today_date: date):
        """Calculate weekly and monthly aggregates"""
        
        # Weekly (last 7 days including today)
        week_start = today_date - timedelta(days=6)
        week_history = db.query(StreamHistory).filter(
            and_(
                StreamHistory.track_id == track_id,
                StreamHistory.date >= week_start,
                StreamHistory.date < today_date
            )
        ).all()
        weekly_sum = sum(h.daily_streams for h in week_history) + today_daily
        
        # Monthly (last 30 days including today)
        month_start = today_date - timedelta(days=29)
        month_history = db.query(StreamHistory).filter(
            and_(
                StreamHistory.track_id == track_id,
                StreamHistory.date >= month_start,
                StreamHistory.date < today_date
            )
        ).all()
        monthly_sum = sum(h.daily_streams for h in month_history) + today_daily
        
        return weekly_sum, monthly_sum

    def run_and_save(self, db: Session, playlist_obj: Playlist):
        print(f"\n{'='*60}")
        print(f"Starting update for: {playlist_obj.name}")
        print(f"{'='*60}\n")
        
        try:
            if not self.setup_spotipy():
                raise Exception("Failed to initialize Spotify API")
            if not self.setup_driver():
                raise Exception("Failed to initialize WebDriver")

            api_tracks = self.fetch_tracks_api()
            if not api_tracks:
                raise Exception("No tracks found via API")

            today_date = date.today()
            processed_count = 0
            
            for idx, t_data in enumerate(api_tracks, 1):
                print(f"\n[{idx}/{len(api_tracks)}] Processing: {t_data['name']}")
                
                db_track = db.query(Track).filter(
                    Track.spotify_id == t_data['spotify_id']
                ).first()
                
                if not db_track:
                    db_track = Track(
                        spotify_id=t_data['spotify_id'],
                        name=t_data['name'],
                        artist=t_data['artist'],
                        url=t_data['url'],
                        playlist_id=playlist_obj.id
                    )
                    db.add(db_track)
                    db.commit()
                    db.refresh(db_track)
                    print(f"  → New track added to database")

                existing_today = db.query(StreamHistory).filter(
                    and_(
                        StreamHistory.track_id == db_track.id,
                        StreamHistory.date == today_date
                    )
                ).first()
                
                if existing_today:
                    print(f"  → Already updated today, skipping")
                    continue

                total_streams = self.scrape_stream_count(t_data['url'], t_data['name'])
                
                last_entry = db.query(StreamHistory).filter(
                    StreamHistory.track_id == db_track.id
                ).order_by(desc(StreamHistory.date)).first()

                is_new = False
                is_reset = False
                is_imputed = False
                is_hidden = (total_streams == 0)
                daily_diff = 0

                if is_hidden and last_entry:
                    total_streams = last_entry.total_streams
                    print(f"  → Stream count unavailable, using last known: {total_streams:,}")

                if not last_entry:
                    is_new = True
                    daily_diff = 0
                    print(f"  → NEW TRACK: Starting tracking with {total_streams:,} streams")
                else:
                    days_gap = (today_date - last_entry.date).days
                    
                    if days_gap > 1:
                        raw_diff = total_streams - last_entry.total_streams
                        if raw_diff < 0:
                            is_reset = True
                            daily_diff = 0
                            print(f"  → RESET detected during {days_gap}-day gap")
                        else:
                            daily_average = int(raw_diff / days_gap)
                            daily_diff = daily_average
                            is_imputed = True
                            print(f"  → IMPUTED: {days_gap}-day gap, avg {daily_average:,} streams/day")
                            
                            for i in range(1, days_gap):
                                missing_date = last_entry.date + timedelta(days=i)
                                imputed_total = last_entry.total_streams + (daily_average * i)
                                missing_history = StreamHistory(
                                    date=missing_date,
                                    track_id=db_track.id,
                                    total_streams=imputed_total,
                                    daily_streams=daily_average,
                                    weekly_streams=0, 
                                    monthly_streams=0,
                                    is_imputed=True,
                                    is_new=False,
                                    is_reset=False,
                                    is_hidden=False
                                )
                                db.add(missing_history)
                    
                    elif total_streams < last_entry.total_streams:
                        is_reset = True
                        daily_diff = 0
                        print(f"  → RESET detected: {last_entry.total_streams:,} → {total_streams:,}")
                    
                    else:
                        daily_diff = total_streams - last_entry.total_streams
                        print(f"  → Standard update: +{daily_diff:,} streams")

                weekly_sum, monthly_sum = self.calculate_aggregates(
                    db, db_track.id, daily_diff, today_date
                )

                new_history = StreamHistory(
                    date=today_date,
                    track_id=db_track.id,
                    total_streams=total_streams,
                    daily_streams=daily_diff,
                    weekly_streams=weekly_sum,
                    monthly_streams=monthly_sum,
                    is_new=is_new,
                    is_reset=is_reset,
                    is_imputed=is_imputed,
                    is_hidden=is_hidden
                )
                db.add(new_history)
                processed_count += 1

            playlist_obj.last_updated = datetime.utcnow()
            db.commit()
            
            print(f"\n{'='*60}")
            print(f"✓ Successfully processed {processed_count} tracks")
            print(f"{'='*60}\n")
            
        except Exception as e:
            print(f"\n✗ ERROR: {e}")
            traceback.print_exc()
            raise
        finally:
            if self.driver:
                self.driver.quit()
                print("✓ WebDriver closed")