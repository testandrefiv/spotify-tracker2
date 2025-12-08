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
import requests
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_result
import random
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
        self.driver = None
        self.tracks_data = []
        self.session = requests.Session()
        self.session.headers.update({
             "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
             "Accept-Language": "en-US,en;q=0.9",
        })

    def _rotate_user_agent(self):
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0"
        ]
        self.session.headers.update({"User-Agent": random.choice(user_agents)})

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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _fetch_page_content(self, url):
        """Robust page fetch using requests with timeout"""
        try:
            self._rotate_user_agent()
            response = self.session.get(url, timeout=15)
            if response.status_code == 429:
                print("  ⚠ Rate limited (429), backing off...")
                time.sleep(10)
                raise Exception("Rate limited")
            response.raise_for_status()
            return response.text
        except Exception as e:
            print(f"  ⚠ HTTP Request failed: {e}")
            raise

    def fetch_stream_count_requests(self, url):
        """
        Primary strategy: Fast HTML parsing with requests + BeautifulSoup
        """
        try:
            html = self._fetch_page_content(url)
            soup = BeautifulSoup(html, 'html.parser')
            
            # Look for specific data-testid="playcount"
            playcount_element = soup.find(attrs={"data-testid": "playcount"})
            
            if playcount_element:
                text = playcount_element.get_text().strip()
                if text and any(c.isdigit() for c in text):
                    return self._extract_stream_count_helper(text)
            
            return None
        except Exception:
            return None

    def estimate_streams_from_history(self, track_id: int, db: Session) -> tuple:
        """
        Estimate stream count based on historical data when scraping fails.
        Returns: (estimated_value, confidence_score)
        """
        try:
            # Get last 7 days of data for this track
            history = db.query(StreamHistory).filter(
                StreamHistory.track_id == track_id,
                StreamHistory.is_simulated == False  # Only use real data
            ).order_by(StreamHistory.date.desc()).limit(7).all()
            
            if not history:
                return (None, 0.0)
            
            # Get the most recent value
            last_known = history[0]
            
            # Calculate average daily growth
            if len(history) >= 2:
                daily_changes = []
                for i in range(len(history) - 1):
                    change = history[i].total_streams - history[i+1].total_streams
                    if change > 0:  # Only count positive growth
                        daily_changes.append(change)
                
                if daily_changes:
                    avg_daily_growth = sum(daily_changes) / len(daily_changes)
                    
                    # Calculate days since last update
                    days_passed = (date.today() - last_known.date).days
                    if days_passed <= 0:
                        days_passed = 1
                    
                    estimated_value = int(last_known.total_streams + (avg_daily_growth * days_passed))
                    
                    # Confidence based on data quality
                    confidence = min(len(history) / 7.0, 1.0)  # More history = higher confidence
                    confidence *= 0.8  # Cap at 80% since it's simulated
                    
                    print(f"  📊 Simulated: {estimated_value:,} (confidence: {confidence:.0%}, based on {len(history)} days)")
                    return (estimated_value, confidence)
            
            # If we can't calculate growth, just use last known value
            confidence = 0.5 if len(history) >= 3 else 0.3
            print(f"  📊 Simulated: {last_known.total_streams:,} (last known, confidence: {confidence:.0%})")
            return (last_known.total_streams, confidence)
            
        except Exception as e:
            print(f"  ⚠ Estimation error: {e}")
            return (None, 0.0)

    def scrape_stream_count_with_telemetry(self, url, track_name, track_id, db):
        """
        Enhanced scraping with method tracking and simulation fallback
        Returns: (stream_count, method_used, confidence_score)
        """
        # 1. Try Requests first (Fast, low resource)
        try:
            time.sleep(random.uniform(0.5, 1.5))
            streams = self.fetch_stream_count_requests(url)
            if streams and self._is_reasonable_stream_count(streams):
                self.scrape_stats["requests_success"] += 1
                return (streams, "requests", 1.0)
        except Exception:
            pass

        print(f"  ℹ Falling back to Selenium for: {track_name}")
        
        # 2. Selenium Fallback
        if not self.driver:
            if not self.setup_driver():
                print(f"  ✗ Failed to initialize Selenium, trying simulation")
                # Go straight to simulation
                estimated, confidence = self.estimate_streams_from_history(track_id, db)
                if estimated:
                    self.scrape_stats["simulated"] += 1
                    return (estimated, "simulated", confidence)
                self.scrape_stats["failed"] += 1
                return (0, "failed", 0.0)

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
                        # print(f"  → Found {len(playcount_elements)} playcount element(s)")
                        
                        for idx, elem in enumerate(playcount_elements):
                            try:
                                text = elem.text.strip()
                                # print(f"    Element {idx+1}: '{text}'")
                                
                                # Only extract if text contains digits
                                if text and any(c.isdigit() for c in text):
                                    streams = self._extract_stream_count_helper(text)
                                    
                                    # Validate it's a reasonable stream count
                                    if streams and self._is_reasonable_stream_count(streams):
                                        print(f"  ✓ {track_name}: {streams:,} streams")
                                        self.driver.close()
                                        self.driver.switch_to.window(original_window)
                                        self.scrape_stats["selenium_success"] += 1
                                        return (streams, "selenium", 1.0)
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
                                self.scrape_stats["selenium_success"] += 1
                                return (streams, "selenium", 1.0)
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

        # 3. All scraping failed - try simulation
        print(f"  ✗ {track_name}: Selenium failed, trying simulation")
        estimated, confidence = self.estimate_streams_from_history(track_id, db)
        if estimated:
            self.scrape_stats["simulated"] += 1
            return (estimated, "simulated", confidence)
        
        # 4. Complete failure
        print(f"  ✗ {track_name}: Could not find valid stream count (no history for simulation)")
        self.scrape_stats["failed"] += 1
        return (0, "failed", 0.0)

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
            if not self.setup_spotipy():
                raise Exception("Failed to initialize Spotify API")
            
            # Setup Selenium later only if needed or preemptively if preferred
            # We delay Selenium setup to save resources if not everything needs it
            # But existing logic might expect it enabled. Let's keep it for safety for now
            # or make it lazy. For "robustness" on Railway, let's MAKE IT LAZY.
            # if not self.setup_driver():
            #     raise Exception("Failed to initialize WebDriver")

            # Update playlist status to "updating"
            playlist_obj.update_status = "updating"
            playlist_obj.update_started_at = datetime.utcnow()
            db.commit()
            
            api_tracks = self.fetch_tracks_api()
            if not api_tracks:
                playlist_obj.update_status = "failed"
                db.commit()
                raise Exception("No tracks found via API")

            today_date = date.today()
            processed_count = 0
            
            for idx, t_data in enumerate(api_tracks, 1):
                # 1. FIX: Look for the song SPECIFICALLY in this playlist (Separate duplicate songs)
                db_track = db.query(Track).filter(
                    and_(
                        Track.spotify_id == t_data['spotify_id'],
                        Track.playlist_id == playlist_obj.id  # <--- Added playlist ID filter
                    )
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
                    # SILENT SKIP: No print, just continue
                    continue

                # PRINT HERE: Only print if we are actually processing the song
                print(f"\n[{idx}/{len(api_tracks)}] Processing: {t_data['name']}")

                # Use telemetry-based scraping
                total_streams, scrape_method, confidence = self.scrape_stream_count_with_telemetry(
                    t_data['url'], t_data['name'], db_track.id, db
                )
                
                last_entry = db.query(StreamHistory).filter(
                    StreamHistory.track_id == db_track.id
                ).order_by(desc(StreamHistory.date)).first()

                
                is_new = False
                is_reset = False
                is_imputed = False
                is_hidden = (total_streams == 0)
                is_simulated = (scrape_method == "simulated")
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
                    is_hidden=is_hidden,
                    is_simulated=is_simulated,
                    scrape_method=scrape_method,
                    confidence_score=confidence if is_simulated else None
                )
                db.add(new_history)
                processed_count += 1
                
                # 2. FIX: Save immediately to prevent duplicate "New" entries in same playlist
                db.commit()

            # Mark playlist as completed
            playlist_obj.last_updated = datetime.utcnow()
            playlist_obj.update_completed_at = datetime.utcnow()
            playlist_obj.last_successful_update = datetime.utcnow()
            playlist_obj.update_status = "completed"
            db.commit()
            
            print(f"\n{'='*60}")
            print(f"✓ Successfully processed {processed_count} tracks")
            print(f"\n📊 Scraping Statistics:")
            print(f"  Requests Success: {self.scrape_stats['requests_success']}")
            print(f"  Selenium Success: {self.scrape_stats['selenium_success']}")
            print(f"  Simulated: {self.scrape_stats['simulated']}")
            print(f"  Failed: {self.scrape_stats['failed']}")
            print(f"{'='*60}\n")
            
        except Exception as e:
            # Mark playlist as failed
            playlist_obj.update_status = "failed"
            playlist_obj.update_completed_at = datetime.utcnow()
            db.commit()
            
            print(f"\n✗ ERROR: {e}")
            traceback.print_exc()
            raise
        finally:
            if self.driver:
                self.driver.quit()

                print("✓ WebDriver closed")
