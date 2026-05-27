#!/usr/bin/env python3
import eventlet
import logging

logging.getLogger('werkzeug').setLevel(logging.ERROR)

# Patch logging's internal weakref callback to ignore greenlet finalization
_original_removeHandlerRef = logging._removeHandlerRef
def _safe_removeHandlerRef(wr):
    try:
        _original_removeHandlerRef(wr)
    except RuntimeError:
        pass  # ignore greenlet finalization error
logging._removeHandlerRef = _safe_removeHandlerRef

eventlet.monkey_patch()

import time
import threading
import subprocess
import requests
import sqlite3
import json
import os
import pylast
import keyring
import signal
import sys
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, jsonify, send_file
from flask_socketio import SocketIO
import io
import fcntl

_last_http_error_time = 0

# ------------------------- CONFIGURATION -------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(SCRIPT_DIR, "scrobbles.db")
SYNC_META_FILE = "scrobbler_sync_meta.json"
DIRTY_FLAG_FILE = os.path.join(SCRIPT_DIR, ".scrobbler_dirty")

# Command-line flags
SKIP_SYNC_ON_EXIT = "--no-sync" in sys.argv

BASE_URL = "http://127.0.0.1:47836"
CURRENT_URL = f"{BASE_URL}/current"
POLL_INTERVAL = 1
SCROBBLE_THRESHOLD = 0.5
MIN_SECONDS_TO_SCROBBLE = 240

LASTFM_SERVICE = "LastFM"

album_cache = {}
last_cached_album = None

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

current_track_data = {
    "track": "", "artist": "", "album": "", "art": "", "current": "0:00", "duration": "0:00",
    "duration_sec": 0, "progress": 0, "volume": 50, "shuffle": "off", "repeat": "OFF",
    "playing_from": "Playing from: Unknown", "quality_raw": "", "quality": "low",
    "bitDepth": 0, "sampleRate": 0, "codec": "", "badgeText": ""
}

session = {
    "current_track_id": None, "track_start_time": 0, "track_start_timestamp": 0,
    "max_position": 0, "last_track_data": None, "lock": threading.Lock()
}

# Playlists to ignore (do not store playlist name, treat as album)
IGNORED_PLAYLISTS = {"Top Tracks", "Mix", "My Daily Discovery"}  # add any others you want to ignore

# ------------------------- BACKUP SYSTEM -------------------------
import glob
import shutil

BACKUP_DIR = os.path.join(SCRIPT_DIR, "backups")
BACKUP_INTERVAL_HOURS = 24          # how often to auto‑backup
MAX_BACKUPS = 30                    # keep the last N backups

def backup_database():
    """Create a consistent snapshot of the database using SQLite's backup API."""
    if not os.path.exists(BACKUP_DIR):
        os.makedirs(BACKUP_DIR, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    backup_filename = f"scrobbles_{timestamp}.db"
    backup_path = os.path.join(BACKUP_DIR, backup_filename)

    try:
        src = sqlite3.connect(DATABASE)
        dst = sqlite3.connect(backup_path)
        src.backup(dst)
        dst.close()
        src.close()
        print(f"💾 Backup created: {backup_filename}")
        return backup_path
    except Exception as e:
        print(f"❌ Backup failed: {e}")
        return None

def migrate_favourites_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS favourites (
        artist TEXT NOT NULL,
        track TEXT NOT NULL,
        album TEXT,
        art_url TEXT,
        added_at INTEGER NOT NULL,
        PRIMARY KEY (artist, track)
    )''')
    conn.commit()
    conn.close()

def migrate_favourite_albums_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS favourite_albums (
        artist TEXT NOT NULL,
        album TEXT NOT NULL,
        art_url TEXT,
        added_at INTEGER NOT NULL,
        PRIMARY KEY (artist, album)
    )''')
    conn.commit()
    conn.close()

def migrate_artist_ignore_genre():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS artist_ignore_genre (
        artist TEXT PRIMARY KEY
    )''')
    conn.commit()
    conn.close()

def migrate_mb_blacklist_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS musicbrainz_tag_blacklist (
        tag TEXT PRIMARY KEY
    )''')
    conn.commit()
    conn.close()

def get_mb_tag_blacklist():
    """Return a set of blacklisted MusicBrainz tags (lowercased)."""
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT tag FROM musicbrainz_tag_blacklist")
    rows = c.fetchall()
    conn.close()
    return {row[0].lower() for row in rows}

@app.route('/api/mb_blacklist/add', methods=['POST'])
def api_mb_blacklist_add():
    tag = request.get_json().get('tag', '').strip()
    if not tag:
        return jsonify({"error": "Missing tag"}), 400
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO musicbrainz_tag_blacklist (tag) VALUES (?)", (tag.lower(),))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "tag": tag.lower()})

@app.route('/api/mb_blacklist')
def api_mb_blacklist():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT tag FROM musicbrainz_tag_blacklist ORDER BY tag")
    rows = c.fetchall()
    conn.close()
    return jsonify([row[0] for row in rows])

@app.route('/api/mb_blacklist/remove', methods=['POST'])
def api_mb_blacklist_remove():
    tag = request.get_json().get('tag', '').strip()
    if not tag:
        return jsonify({"error": "Missing tag"}), 400
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("DELETE FROM musicbrainz_tag_blacklist WHERE tag = ?", (tag.lower(),))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

def cleanup_old_backups():
    """Remove oldest backups exceeding MAX_BACKUPS."""
    backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "scrobbles_*.db")))
    while len(backups) > MAX_BACKUPS:
        oldest = backups.pop(0)
        try:
            os.remove(oldest)
            print(f"🗑️ Removed old backup: {os.path.basename(oldest)}")
        except OSError as e:
            print(f"⚠️ Could not delete old backup {oldest}: {e}")

def vacuum_database():
    """Rebuild the database file to reclaim space and optimise performance."""
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute("VACUUM")
        conn.close()
        print("🧹 Database vacuumed successfully.")
    except Exception as e:
        print(f"⚠️ Vacuum failed: {e}")

def get_most_recent_backup():
    """Return the path of the most recent backup file, or None if none exist."""
    backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "scrobbles_*.db")))
    return backups[-1] if backups else None

def backup_scheduler():
    """On startup, backs up only if the last backup is older than 24h, then schedules every 24h."""
    backup_count = 0

    # --- Determine the last backup time ---
    most_recent = get_most_recent_backup()
    if most_recent:
        basename = os.path.basename(most_recent)
        date_str = basename[len("scrobbles_"):-3]
        try:
            last_backup_time = datetime.strptime(date_str, "%Y-%m-%dT%H-%M-%S")
        except ValueError:
            last_backup_time = datetime.fromtimestamp(os.path.getmtime(most_recent))
    else:
        last_backup_time = None

    now = datetime.now()

    # --- Startup backup (only if older than 24h) ---
    if last_backup_time is None or (now - last_backup_time).total_seconds() >= BACKUP_INTERVAL_HOURS * 3600:
        print("💾 Performing initial backup on startup...")
        backup_database()
        cleanup_old_backups()
        backup_count += 1
        if backup_count % 7 == 0:
            vacuum_database()

        # Set baseline to now (the backup just happened)
        last_backup_time = datetime.now()
    else:
        hours_since = (now - last_backup_time).total_seconds() / 3600
        print(f"ℹ️ Last backup was {hours_since:.1f} hours ago — skipping startup backup.")

    # --- Main loop ---
    while True:
        # Always schedule the next backup 24h from the last successful backup
        next_backup_time = last_backup_time + timedelta(hours=BACKUP_INTERVAL_HOURS)
        seconds_until_next = (next_backup_time - datetime.now()).total_seconds()

        if seconds_until_next > 0:
            print(f"⏳ Next automatic backup at {next_backup_time.strftime('%Y-%m-%d %H:%M:%S')} "
                  f"(in {seconds_until_next/3600:.1f} hours)")
            eventlet.sleep(seconds_until_next)

        # Perform backup (even if we wake a bit late)
        print("⏰ Performing scheduled backup...")
        backup_database()
        cleanup_old_backups()
        backup_count += 1
        if backup_count % 7 == 0:
            vacuum_database()

        # Advance the baseline to now – this guarantees we never loop instantly
        last_backup_time = datetime.now()

# ------------------------- MUSICBRAINZ INTEGRATION -------------------------
import time
import re

MUSICBRAINZ_USER_AGENT = "TIDALScrobbler/1.0 (kerr_avon@live.com)"  # change email
MUSICBRAINZ_API_BASE = "https://musicbrainz.org/ws/2"
MB_REQUEST_DELAY = 1.1   # seconds between API calls (be polite)

_last_mb_request_time = 0 
_last_mb_error_time = 0

def _rate_limit():
    """Ensure at least MB_REQUEST_DELAY seconds between MusicBrainz requests."""
    global _last_mb_request_time
    now = time.time()
    wait = _last_mb_request_time + MB_REQUEST_DELAY - now
    if wait > 0:
        eventlet.sleep(wait)
    _last_mb_request_time = time.time()

def _get_cached_mb_data(artist_lower):
    """Return (mbid, genres) from cache, or (None, None) if not cached."""
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT mbid, genres FROM artist_musicbrainz_cache WHERE artist_lower=?", (artist_lower,))
    row = c.fetchone()
    conn.close()
    return (row[0], row[1]) if row else (None, None)

def _save_mb_cache(artist_lower, mbid, genres):
    """Insert or update the MusicBrainz cache entry."""
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    genres_str = ",".join(genres) if genres else ""
    c.execute("INSERT OR REPLACE INTO artist_musicbrainz_cache (artist_lower, mbid, genres, fetched_at) VALUES (?,?,?,?)",
              (artist_lower, mbid, genres_str, int(time.time())))
    conn.commit()
    conn.close()

def _mb_request(url, params, max_retries=3):
    """Wrapper for MusicBrainz API calls with retry on transient errors."""
    global _last_mb_error_time    # add this line
    headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
    for attempt in range(max_retries):
        _rate_limit()
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            if resp.status_code == 200:
                return resp
            elif resp.status_code == 503:
                print(f"   MusicBrainz busy (503), retrying in {2 ** attempt} sec...")
                eventlet.sleep(2 ** attempt)
            else:
                print(f"   MusicBrainz returned {resp.status_code}, skipping.")
                return None
        except requests.exceptions.SSLError as e:
            if time.time() - _last_mb_error_time > 10:
                print(f"   SSL error (attempt {attempt+1}): {e}")
                _last_mb_error_time = time.time()
            eventlet.sleep(1)
        except requests.exceptions.ConnectionError as e:
            if time.time() - _last_mb_error_time > 10:
                print(f"   Connection error (attempt {attempt+1}): {e}")
                _last_mb_error_time = time.time()
            eventlet.sleep(1)
        except Exception as e:
            if time.time() - _last_mb_error_time > 10:
                print(f"   Unexpected request error: {e}")
                _last_mb_error_time = time.time()
            return None
    return None

def _search_artist_mbid(artist_name):
    """Search MusicBrainz for an artist and return the first MBID, or None."""
    params = {
        "query": f'artist:"{artist_name}"',
        "fmt": "json",
        "limit": 1
    }
    resp = _mb_request(f"{MUSICBRAINZ_API_BASE}/artist/", params)
    if not resp:
        return None
    try:
        data = resp.json()
        artists = data.get("artists", [])
        return artists[0]["id"] if artists else None
    except Exception as e:
        print(f"   Error parsing search response: {e}")
        return None

def _fetch_artist_tags(mbid):
    """Fetch genre tags for a MusicBrainz artist ID. Returns a list of tag names."""
    params = {"inc": "tags", "fmt": "json"}
    resp = _mb_request(f"{MUSICBRAINZ_API_BASE}/artist/{mbid}", params)
    if not resp:
        return []
    try:
        data = resp.json()
        tags = data.get("tags", [])
        sorted_tags = sorted(tags, key=lambda t: t.get("count", 0), reverse=True)
        return [t["name"] for t in sorted_tags]
    except Exception as e:
        print(f"   Error parsing tags response: {e}")
        return []

def get_musicbrainz_genres(artist_name):
    """
    Return a list of genre strings for the given artist.
    Uses local cache to avoid repeated API calls.
    Returns empty list if lookup fails.
    """
    artist_lower = artist_name.strip().lower()
    if not artist_lower:
        return []

    # Check cache first
    mbid, cached_genres = _get_cached_mb_data(artist_lower)
    if cached_genres is not None:
        # cached genres are already filtered (saved below)
        return [g.strip() for g in cached_genres.split(",") if g.strip()]

    # Not cached – perform lookup
    print(f"🔍 MusicBrainz: looking up artist '{artist_name}'")
    mbid = _search_artist_mbid(artist_name)
    if not mbid:
        _save_mb_cache(artist_lower, "", [])
        return []

    genres = _fetch_artist_tags(mbid)
    if not genres:
        print("   No genres found.")
        _save_mb_cache(artist_lower, "", [])
        return []

    # Filter out blacklisted tags BEFORE caching
    blacklist = get_mb_tag_blacklist()
    genres = [g for g in genres if g.lower() not in blacklist]

    if genres:
        print(f"   Found genres: {', '.join(genres[:5])}")
    else:
        print("   All genres blacklisted or empty.")

    # Save the already‑filtered list
    _save_mb_cache(artist_lower, mbid, genres)
    return genres

# ------------------------- LAST.FM INTEGRATION -------------------------
def get_lastfm_credentials():
    try:
        username = keyring.get_password(LASTFM_SERVICE, "username")
        api_key = keyring.get_password(LASTFM_SERVICE, "api_key")
        api_secret = keyring.get_password(LASTFM_SERVICE, "api_secret")
        password = keyring.get_password(LASTFM_SERVICE, "password")
        if all([username, api_key, api_secret, password]):
            return username, api_key, api_secret, password
    except Exception as e:
        print(f"Keyring error: {e}")
    return None, None, None, None

def get_lastfm_network():
    username, api_key, api_secret, password = get_lastfm_credentials()
    if not all([username, api_key, api_secret, password]):
        return None
    try:
        password_hash = pylast.md5(password)
        network = pylast.LastFMNetwork(
            api_key=api_key,
            api_secret=api_secret,
            username=username,
            password_hash=password_hash
        )
        network.get_authenticated_user()
        print(f"✅ Authenticated with Last.fm as: {username}")
        return network
    except Exception as e:
        print(f"❌ Last.fm auth error: {e}")
        return None

def update_now_playing(network, artist, title, album):
    if not network:
        return
    try:
        network.update_now_playing(artist=artist, title=title, album=album)
        print(f"📡 Now playing on Last.fm: {artist} - {title}")
    except Exception as e:
        print(f"⚠️ Now playing error: {e}")

def scrobble_to_lastfm(network, artist, title, album, timestamp):
    if not network:
        return
    try:
        network.scrobble(artist=artist, title=title, album=album, timestamp=timestamp)
        print(f"📀 Scrobbled to Last.fm: {artist} - {title}")
    except Exception as e:
        print(f"❌ Scrobble error: {e}")

# ------------------------- DATABASE -------------------------
def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL;") 
    c.execute('''CREATE TABLE IF NOT EXISTS scrobbles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp INTEGER NOT NULL,
        track TEXT NOT NULL,
        artist TEXT NOT NULL,
        album TEXT,
        art_url TEXT,
        duration_sec INTEGER,
        quality TEXT,
        bit_depth INTEGER,
        sample_rate INTEGER,
        codec TEXT,
        playlist TEXT,
        lastfm_scrobbled INTEGER DEFAULT 0,
        genre TEXT
    )''')
    conn.commit()
    conn.close()
    migrate_db()
    migrate_genre_db()
    migrate_album_genre_db()
    migrate_artist_genre_db()
    migrate_album_genre_db()
    migrate_musicbrainz_cache_db()
    migrate_indexes()
    migrate_favourites_db()
    migrate_artist_ignore_genre()
    migrate_mb_blacklist_db()
    migrate_favourite_albums_db()

def migrate_indexes():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    # Speeds up stats, search, and the poller's genre lookups
    c.execute("CREATE INDEX IF NOT EXISTS idx_scrobbles_artist ON scrobbles(artist)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_scrobbles_timestamp ON scrobbles(timestamp)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_scrobbles_genre ON scrobbles(genre)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_mb_cache_artist_lower ON artist_musicbrainz_cache(artist_lower)")
    conn.commit()
    conn.close()
    print("📊 Database indexes verified/created.")

def migrate_musicbrainz_cache_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS artist_musicbrainz_cache (
        artist_lower TEXT PRIMARY KEY,
        mbid TEXT,
        genres TEXT,
        fetched_at INTEGER
    )''')
    conn.commit()
    conn.close()

def migrate_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("PRAGMA table_info(scrobbles)")
    columns = [col[1] for col in c.fetchall()]
    if 'playlist' not in columns:
        c.execute("ALTER TABLE scrobbles ADD COLUMN playlist TEXT")
    if 'lastfm_scrobbled' not in columns:
        c.execute("ALTER TABLE scrobbles ADD COLUMN lastfm_scrobbled INTEGER DEFAULT 0")
    conn.commit()
    conn.close()

def migrate_genre_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("PRAGMA table_info(scrobbles)")
    columns = [col[1] for col in c.fetchall()]
    if 'genre' not in columns:
        c.execute("ALTER TABLE scrobbles ADD COLUMN genre TEXT")
    c.execute('''CREATE TABLE IF NOT EXISTS playlist_genre_map (
        playlist_name TEXT PRIMARY KEY,
        genre TEXT NOT NULL
    )''')
    conn.commit()
    conn.close()

def migrate_album_genre_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS album_genre_map (
        album TEXT,
        artist TEXT,
        genre TEXT NOT NULL,
        PRIMARY KEY (album, artist)
    )''')
    conn.commit()
    conn.close()

def migrate_artist_genre_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS artist_genre_map (
        artist TEXT PRIMARY KEY,
        genre TEXT NOT NULL
    )''')
    conn.commit()
    conn.close()

def add_scrobble(track, artist, album, art_url, duration_sec, quality, bit_depth, sample_rate, codec, playlist=None):
    timestamp = int(time.time())
        # If we're skipping exit sync, mark that we have unsynced scrobbles
    if SKIP_SYNC_ON_EXIT and not os.path.exists(DIRTY_FLAG_FILE):
        with open(DIRTY_FLAG_FILE, "w") as f:
            f.write(str(timestamp))
    genre = None

    # --- Determine genre with priority: Artist > Album > MusicBrainz > Playlist ---
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()

    # 1. Artist genre (highest priority)
    c.execute("SELECT genre FROM artist_genre_map WHERE artist = ?", (artist,))
    row = c.fetchone()
    if row:
        genre = row[0]
    else:
        # 2. Album genre
        if album:
            c.execute("SELECT genre FROM album_genre_map WHERE album = ? AND artist = ?", (album, artist))
            row = c.fetchone()
            if row:
                genre = row[0]
    conn.close()

    # 3. MusicBrainz auto‑suggestion (now before playlist)
    if not genre:
        mb_genres = get_musicbrainz_genres(artist)
        if mb_genres:
            genre = mb_genres[0]

    # 4. Playlist genre (lowest priority)
    if not genre and playlist:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT genre FROM playlist_genre_map WHERE playlist_name = ?", (playlist,))
        row = c.fetchone()
        if row:
            genre = row[0]
        conn.close()

    # --- Deduplication: skip if the same track/artist was scrobbled in the last 5 seconds ---
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("""SELECT id FROM scrobbles 
                 WHERE track=? AND artist=? 
                   AND timestamp BETWEEN ? AND ? 
                 LIMIT 1""",
              (track, artist, timestamp - 5, timestamp))
    existing = c.fetchone()
    conn.close()
    if existing:
        print(f"⏭️ Duplicate scrobble skipped: {artist} - {track} (already scrobbled within 5 seconds)")
        return  # do not insert

    # --- Insert into scrobbles (including the determined genre) ---
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('''INSERT INTO scrobbles 
        (timestamp, track, artist, album, art_url, duration_sec, quality, bit_depth, sample_rate, codec, playlist, lastfm_scrobbled, genre)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (timestamp, track, artist, album, art_url, duration_sec, quality, bit_depth, sample_rate, codec, playlist, 0, genre))
    conn.commit()
    conn.close()
    print(f"📀 Scrobbled locally: {artist} - {track}" + (f" [playlist: {playlist}]" if playlist else "") + (f" [genre: {genre}]" if genre else ""))

    # Send to Last.fm
    network = get_lastfm_network()
    if network:
        scrobble_to_lastfm(network, artist, track, album, timestamp)

def get_all_scrobbles(limit=100, offset=0):
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('''SELECT * FROM scrobbles ORDER BY timestamp DESC LIMIT ? OFFSET ?''', (limit, offset))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def count_scrobbles():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM scrobbles')
    count = c.fetchone()[0]
    conn.close()
    return count

def get_top_artists_with_art(limit=50):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("""
        SELECT artist, COUNT(*) as playcount,
            (SELECT art_url FROM scrobbles s2 
             WHERE s2.artist = s1.artist AND s2.art_url IS NOT NULL AND s2.art_url != ''
             ORDER BY s2.timestamp DESC LIMIT 1) as art_url
        FROM scrobbles s1
        GROUP BY artist
        ORDER BY playcount DESC
        LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    return [{"artist": row[0], "playcount": row[1], "art_url": row[2]} for row in rows]

def get_top_albums_with_art(limit=50):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("""
        SELECT artist, album, COUNT(*) as playcount,
            (SELECT art_url FROM scrobbles s2 
             WHERE s2.artist = s1.artist AND s2.album = s1.album 
               AND s2.art_url IS NOT NULL AND s2.art_url != ''
             ORDER BY s2.timestamp DESC LIMIT 1) as art_url
        FROM scrobbles s1
        WHERE album IS NOT NULL AND album != ''
        GROUP BY artist, album
        ORDER BY playcount DESC
        LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    return [{"artist": row[0], "album": row[1], "playcount": row[2], "art_url": row[3]} for row in rows]

def get_top_tracks_with_art(limit=50):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("""
        SELECT artist, track, COUNT(*) as playcount,
            (SELECT art_url FROM scrobbles s2 
             WHERE s2.artist = s1.artist AND s2.track = s1.track 
               AND s2.art_url IS NOT NULL AND s2.art_url != ''
             ORDER BY s2.timestamp DESC LIMIT 1) as art_url
        FROM scrobbles s1
        GROUP BY artist, track
        ORDER BY playcount DESC
        LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    return [{"artist": row[0], "track": row[1], "playcount": row[2], "art_url": row[3]} for row in rows]

def get_top_playlists(limit=50):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('''SELECT playlist, COUNT(*) as count 
                 FROM scrobbles 
                 WHERE playlist IS NOT NULL AND playlist != ''
                 GROUP BY playlist 
                 ORDER BY count DESC 
                 LIMIT ?''', (limit,))
    rows = c.fetchall()
    conn.close()
    return [{"playlist": row[0], "count": row[1]} for row in rows]

def get_listening_time():
    now = datetime.now()
    today_start = int(datetime(now.year, now.month, now.day).timestamp())
    week_start = int((now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    month_start = int(datetime(now.year, now.month, 1).timestamp())
    year_start = int(datetime(now.year, 1, 1).timestamp())
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    def sum_seconds(since_ts):
        c.execute('SELECT SUM(duration_sec) FROM scrobbles WHERE timestamp >= ? AND duration_sec > 0', (since_ts,))
        result = c.fetchone()[0]
        return result if result else 0
    
    today_sec = sum_seconds(today_start)
    week_sec = sum_seconds(week_start)
    month_sec = sum_seconds(month_start)
    year_sec = sum_seconds(year_start)
    conn.close()
    
    return {
        "today": round(today_sec / 3600, 1),
        "week": round(week_sec / 3600, 1),
        "month": round(month_sec / 3600, 1),
        "year": round(year_sec / 3600, 1)
    }

def export_scrobbles_to_json():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM scrobbles ORDER BY timestamp')
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def import_scrobbles_from_json(data):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    inserted = 0
    for item in data:
        c.execute('SELECT id FROM scrobbles WHERE timestamp=? AND track=? AND artist=?',
                  (item['timestamp'], item['track'], item['artist']))
        if not c.fetchone():
            c.execute('''INSERT INTO scrobbles 
                (timestamp, track, artist, album, art_url, duration_sec, quality, bit_depth, sample_rate, codec, playlist, lastfm_scrobbled, genre)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (item['timestamp'], item['track'], item['artist'], item.get('album'),
                 item.get('art_url'), item.get('duration_sec'), item.get('quality'),
                 item.get('bit_depth'), item.get('sample_rate'), item.get('codec'), 
                 item.get('playlist'), 0, item.get('genre')))
            inserted += 1
    conn.commit()
    conn.close()
    return inserted

# ------------------------- PLAYERCTL & TIDAL API -------------------------
PLAYERCTL_CMD = ["playerctl", "-i", "plasma-browser-integration"]

def run_playerctl(*args):
    try:
        result = subprocess.run(PLAYERCTL_CMD + list(args), capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as e:
        print(f"playerctl error: {e}")
    return None

def get_playerctl_metadata():
    title = run_playerctl("metadata", "xesam:title")
    if title is None:
        return None
    artist = run_playerctl("metadata", "xesam:artist") or ""
    album = run_playerctl("metadata", "xesam:album") or ""
    length_us = run_playerctl("metadata", "mpris:length")
    position_sec = run_playerctl("position")
    duration_sec = int(length_us) / 1_000_000 if length_us and length_us.isdigit() else 0
    pos = float(position_sec) if position_sec else 0
    return {"track": title, "artist": artist, "album": album, "duration_sec": duration_sec, "position": pos}

def fetch_http_details():
    global current_track_data, last_cached_album
    try:
        resp = requests.get(CURRENT_URL, timeout=2, headers={'Cache-Control': 'no-cache'})
        if resp.status_code != 200:
            return
        data = resp.json()
        audio_quality = data.get("audioQuality", {})
        quality_raw = audio_quality.get("quality", "")
        badge_text = audio_quality.get("badgeText", "")
        bit_depth = audio_quality.get("bitDepth", 0)
        sample_rate = audio_quality.get("sampleRate", 0)
        codec = audio_quality.get("codec", "")
        art = data.get("image", "")
        album_name = data.get("album", "")
        artist_name = data.get("artist", "")
        playing_from = data.get("playingFrom", "Unknown")

        current_track_data["art"] = art
        player = data.get("player", {})
        current_track_data["shuffle"] = "on" if player.get("shuffle") else "off"
        current_track_data["repeat"] = player.get("repeat", "OFF")
        current_track_data["volume"] = round(data.get("volume", 0) * 100)
        current_track_data["playing_from"] = f"Playing from: {playing_from}"
        current_track_data["album"] = album_name
        current_track_data["artist"] = artist_name

        has_detailed = (bit_depth > 0 and sample_rate > 0) or (badge_text and ('kHz' in badge_text or 'kbps' in badge_text))
        if has_detailed and album_name and artist_name:
            cache_key = f"{artist_name}|{album_name}".lower()
            existing = album_cache.get(cache_key)
            if not existing or (sample_rate > existing.get("sampleRate", 0)):
                album_cache[cache_key] = audio_quality
                print(f"💿 Cached quality for {album_name}: {badge_text}")

        if quality_raw.lower() in ["max", "high"] and album_name and artist_name:
            cache_key = f"{artist_name}|{album_name}".lower()
            cached = album_cache.get(cache_key)
            if cached:
                if last_cached_album != cache_key:
                    print(f"Using cached quality for {album_name}: {cached.get('badgeText')}")
                    last_cached_album = cache_key
                quality_raw = cached.get("quality", quality_raw)
                badge_text = cached.get("badgeText", badge_text)
                bit_depth = cached.get("bitDepth", bit_depth)
                sample_rate = cached.get("sampleRate", sample_rate)
                codec = cached.get("codec", codec)

        current_track_data["badgeText"] = badge_text
        current_track_data["quality_raw"] = quality_raw
        current_track_data["bitDepth"] = bit_depth
        current_track_data["sampleRate"] = sample_rate
        current_track_data["codec"] = codec

        ql = quality_raw.lower()
        if ql in ["hi_res_lossless", "max"]:
            current_track_data["quality"] = "hi_res_lossless"
        elif ql in ["lossless", "high"]:
            current_track_data["quality"] = "lossless"
        else:
            current_track_data["quality"] = "low"
    except Exception as e:
        global _last_http_error_time
        now = time.time()
        if now - _last_http_error_time > 10:   # print only every 10 seconds
            print(f"HTTP fetch error: {e}")
            _last_http_error_time = now

def maybe_scrobble(previous_track_data, max_position, duration):
    if not previous_track_data or not previous_track_data.get("track"):
        return False
    if duration <= 0:
        return max_position >= MIN_SECONDS_TO_SCROBBLE
    progress = max_position / duration
    return progress >= SCROBBLE_THRESHOLD or max_position >= MIN_SECONDS_TO_SCROBBLE

def background_poller():
    global current_track_data
    last_title = None
    network = get_lastfm_network()
    while True:
        meta = get_playerctl_metadata()
        if meta:
            title = meta["track"]
            track_changed = (title != last_title and last_title is not None)

            with session["lock"]:
                # --- Handle track change or first track ---
                if track_changed or last_title is None:
                    # Scrobble previous track if it exists and meets threshold
                    if last_title is not None and session["last_track_data"]:
                        if maybe_scrobble(session["last_track_data"], session["max_position"], session["last_track_data"]["duration_sec"]):
                            ld = session["last_track_data"]
                            playlist_name = ld.get("playlist_name")
                            add_scrobble(ld["track"], ld["artist"], ld["album"], ld["art_url"], ld["duration_sec"],
                                         ld["quality"], ld["bit_depth"], ld["sample_rate"], ld["codec"], playlist_name)
                    # --- Initialise new track (always runs on change or first track) ---
                    # Allow TIDAL API a moment to update after a track change
                    if track_changed:
                        eventlet.sleep(0.2)
                        fetch_http_details()
                    track_start_timestamp = int(time.time())
                    session["track_start_timestamp"] = track_start_timestamp
                    # Get playing_from from the refreshed data
                    playing_from = current_track_data.get("playing_from", "").replace("Playing from: ", "")
                    album_name = meta["album"]   # use playerctl album for reliable comparison
                    # Normalise and compare
                    if playing_from.strip().lower() == album_name.strip().lower():
                        playlist_name_at_start = None
                    else:
                        playlist_name_at_start = playing_from
                    # Apply ignored playlists
                    if playlist_name_at_start and playlist_name_at_start in IGNORED_PLAYLISTS:
                        playlist_name_at_start = None
                    # Ignore "Unknown"
                    if playlist_name_at_start and playlist_name_at_start.strip().lower() == "unknown":
                        playlist_name_at_start = None

                    session["last_track_data"] = {
                        "track": title,
                        "artist": meta["artist"],
                        "album": meta["album"],
                        "playlist_name": playlist_name_at_start,
                        "art_url": current_track_data.get("art", ""),
                        "duration_sec": meta["duration_sec"],
                        "quality": current_track_data.get("quality", "low"),
                        "bit_depth": current_track_data.get("bitDepth", 0),
                        "sample_rate": current_track_data.get("sampleRate", 0),
                        "codec": current_track_data.get("codec", ""),
                        "start_timestamp": track_start_timestamp
                    }
                    session["track_start_time"] = time.time()
                    session["max_position"] = 0
                    last_title = title
                    if network:
                        update_now_playing(network, meta["artist"], title, meta["album"])

                # --- Update max position for current track ---
                pos = meta["position"]
                if pos > session["max_position"]:
                    session["max_position"] = pos

            # --- Update UI display data (outside lock) ---
            current_track_data["track"] = title
            current_track_data["artist"] = meta["artist"]
            current_track_data["album"] = meta["album"]
            current_track_data["duration_sec"] = meta["duration_sec"]
            mins = int(meta["duration_sec"] // 60)
            secs = int(meta["duration_sec"] % 60)
            current_track_data["duration"] = f"{mins}:{secs:02d}"
            pos = meta["position"]
            dur = current_track_data["duration_sec"]
            progress = (pos / dur * 100) if dur > 0 else 0
            mins = int(pos // 60)
            secs = int(pos % 60)
            current_track_data["current"] = f"{mins}:{secs:02d}"
            current_track_data["progress"] = round(progress, 1)

        fetch_http_details()   # always refresh album art, quality, etc.
        with session["lock"]:
            if session["last_track_data"]:
                # Update metadata for the currently tracked track (art, quality, etc.)
                session["last_track_data"]["art_url"] = current_track_data.get("art", session["last_track_data"]["art_url"])
                session["last_track_data"]["quality"] = current_track_data.get("quality", session["last_track_data"]["quality"])
                session["last_track_data"]["bit_depth"] = current_track_data.get("bitDepth", session["last_track_data"]["bit_depth"])
                session["last_track_data"]["sample_rate"] = current_track_data.get("sampleRate", session["last_track_data"]["sample_rate"])
                session["last_track_data"]["codec"] = current_track_data.get("codec", session["last_track_data"]["codec"])

        socketio.emit('update', current_track_data)
        eventlet.sleep(POLL_INTERVAL)

# ------------------------- FLASK ROUTES -------------------------
@app.route('/backup/now')
def backup_now():
    path = backup_database()
    cleanup_old_backups()
    if path:
        return send_file(path, as_attachment=True, download_name=os.path.basename(path))
    else:
        return "Backup failed", 500


@app.route('/favicon.ico')
def favicon():
    return '', 204   # returns no content

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/scrobbles')
def scrobbles_page():
    return render_template_string(SCROBBLES_TEMPLATE)

@app.route('/monthly')
def monthly_page():
    return render_template_string(MONTHLY_TEMPLATE)

@app.route('/api/scrobbles')
def api_scrobbles():
    limit = request.args.get('limit', 100, type=int)
    offset = request.args.get('offset', 0, type=int)
    scrobbles = get_all_scrobbles(limit, offset)
    total = count_scrobbles()
    return jsonify({"scrobbles": scrobbles, "total": total})

@app.route('/api/favourite/check')
def api_favourite_check():
    artist = request.args.get('artist', '')
    track = request.args.get('track', '')
    if not artist or not track:
        return jsonify({"favourite": False})
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT 1 FROM favourites WHERE artist=? AND track=?", (artist, track))
    row = c.fetchone()
    conn.close()
    return jsonify({"favourite": row is not None})

@app.route('/api/favourite_album/check')
def api_favourite_album_check():
    artist = request.args.get('artist', '')
    album = request.args.get('album', '')
    if not artist or not album:
        return jsonify({"favourite": False})
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT 1 FROM favourite_albums WHERE artist=? AND album=?", (artist, album))
    row = c.fetchone()
    conn.close()
    return jsonify({"favourite": row is not None})

@app.route('/api/favourite/toggle', methods=['POST'])
def api_favourite_toggle():
    data = request.get_json()
    artist = data.get('artist')
    track = data.get('track')
    album = data.get('album', '')
    art_url = data.get('art_url', '')
    if not artist or not track:
        return jsonify({"error": "Missing artist or track"}), 400

    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT 1 FROM favourites WHERE artist=? AND track=?", (artist, track))
    exists = c.fetchone()
    if exists:
        c.execute("DELETE FROM favourites WHERE artist=? AND track=?", (artist, track))
        conn.commit()
        conn.close()
        return jsonify({"status": "removed"})
    else:
        c.execute("INSERT INTO favourites (artist, track, album, art_url, added_at) VALUES (?,?,?,?,?)",
                  (artist, track, album, art_url, int(time.time())))
        conn.commit()
        conn.close()
        return jsonify({"status": "added"})

@app.route('/api/favourite_album/toggle', methods=['POST'])
def api_favourite_album_toggle():
    data = request.get_json()
    artist = data.get('artist')
    album = data.get('album')
    art_url = data.get('art_url', '')
    if not artist or not album:
        return jsonify({"error": "Missing artist or album"}), 400

    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT 1 FROM favourite_albums WHERE artist=? AND album=?", (artist, album))
    exists = c.fetchone()
    if exists:
        c.execute("DELETE FROM favourite_albums WHERE artist=? AND album=?", (artist, album))
        conn.commit()
        conn.close()
        return jsonify({"status": "removed"})
    else:
        c.execute("INSERT INTO favourite_albums (artist, album, art_url, added_at) VALUES (?,?,?,?)",
                  (artist, album, art_url, int(time.time())))
        conn.commit()
        conn.close()
        return jsonify({"status": "added"})

@app.route('/api/favourites')
def api_favourites():
    limit = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM favourites ORDER BY added_at DESC LIMIT ? OFFSET ?", (limit, offset))
    rows = c.fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])

@app.route('/api/tidal/favorite/toggle', methods=['POST'])
def tidal_favorite_toggle():
    try:
        resp = requests.post(
            'http://127.0.0.1:47836/player/favorite/toggle',
            headers={'accept': 'text/plain'},
            timeout=2
        )
        return '', resp.status_code
    except Exception:
        return '', 502   # bad gateway – TIDAL probably not running

@app.route('/api/favourite_albums')
def api_favourite_albums():
    limit = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM favourite_albums ORDER BY added_at DESC LIMIT ? OFFSET ?", (limit, offset))
    rows = c.fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])

@app.route('/api/now')
def api_now():
    return jsonify({
        "track": current_track_data.get("track", ""),
        "artist": current_track_data.get("artist", ""),
        "album": current_track_data.get("album", ""),
        "art": current_track_data.get("art", ""),
        "duration": current_track_data.get("duration", "0:00"),
        "current": current_track_data.get("current", "0:00"),
        "progress": current_track_data.get("progress", 0)
    })

@app.route('/api/stats')
def api_stats():
    top_artists = get_top_artists_with_art(50)
    top_albums = get_top_albums_with_art(50)
    top_tracks = get_top_tracks_with_art(50)
    total_scrobbles = count_scrobbles()
    return jsonify({
        "top_artists": top_artists,
        "top_albums": top_albums,
        "top_tracks": top_tracks,
        "total_scrobbles": total_scrobbles
    })

@app.route('/api/listening_time')
def api_listening_time():
    return jsonify(get_listening_time())

@app.route('/api/listening_clock')
def api_listening_clock():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("""
        SELECT 
            strftime('%H', datetime(timestamp, 'unixepoch')) as hour,
            COUNT(*) as count
        FROM scrobbles
        GROUP BY hour
        ORDER BY hour
    """)
    rows = c.fetchall()
    conn.close()
    hour_counts = [0] * 24
    for row in rows:
        hour_counts[int(row[0])] = row[1]
    max_count = max(hour_counts) if hour_counts else 0
    busiest_hour = hour_counts.index(max_count) if max_count > 0 else 0
    return jsonify({
        "hour_counts": hour_counts,
        "busiest_hour": busiest_hour,
        "busiest_hour_count": max_count
    })

@app.route('/api/scrobbles_by_weekday')
def api_scrobbles_by_weekday():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("""
        SELECT strftime('%w', datetime(timestamp, 'unixepoch')) as wd, COUNT(*)
        FROM scrobbles
        GROUP BY wd
        ORDER BY wd
    """)
    rows = c.fetchall()
    conn.close()
    day_names = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    result = [{"day": day_names[int(r[0])], "count": r[1]} for r in rows]
    return jsonify(result)

@app.route('/api/top_artists_by_time')
def api_top_artists_by_time():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("""
        SELECT artist, SUM(duration_sec) as total_seconds
        FROM scrobbles
        WHERE duration_sec > 0
        GROUP BY artist
        ORDER BY total_seconds DESC
        LIMIT 50
    """)
    rows = c.fetchall()
    conn.close()
    result = [{"artist": row[0], "hours": round(row[1] / 3600, 1)} for row in rows]
    return jsonify(result)

@app.route('/api/longest_listening_day')
def api_longest_listening_day():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("""
        SELECT DATE(datetime(timestamp, 'unixepoch')) as day, COUNT(*) as scrobble_count
        FROM scrobbles
        GROUP BY day
        ORDER BY scrobble_count DESC
        LIMIT 10
    """)
    rows = c.fetchall()
    conn.close()
    return jsonify([{"date": row[0], "scrobbles": row[1]} for row in rows])

@app.route('/api/top_playlists')
def api_top_playlists():
    top = get_top_playlists(50)
    return jsonify(top)

@app.route('/api/top_genres')
def api_top_genres():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("""
        SELECT genre, COUNT(*) as count
        FROM scrobbles
        WHERE genre IS NOT NULL AND genre != ''
        GROUP BY genre
        ORDER BY count DESC
        LIMIT 100
    """)
    rows = c.fetchall()
    conn.close()
    return jsonify([{"genre": row[0], "count": row[1]} for row in rows])

@app.route('/api/monthly_report')
def api_monthly_report():
    year = request.args.get('year', type=int)
    month = request.args.get('month', type=int)
    if not year or not month:
        return jsonify({"error": "Missing year or month"}), 400

    start_date = datetime(year, month, 1)
    if month == 12:
        end_date = datetime(year + 1, 1, 1)
    else:
        end_date = datetime(year, month + 1, 1)
    start_ts = int(start_date.timestamp())
    end_ts = int(end_date.timestamp())

    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM scrobbles WHERE timestamp >= ? AND timestamp < ?", (start_ts, end_ts))
    total_scrobbles = c.fetchone()[0]

    c.execute("SELECT SUM(duration_sec) FROM scrobbles WHERE timestamp >= ? AND timestamp < ? AND duration_sec > 0", (start_ts, end_ts))
    total_sec = c.fetchone()[0] or 0
    total_hours = round(total_sec / 3600, 1)

    c.execute("""
        SELECT artist, COUNT(*) as count
        FROM scrobbles
        WHERE timestamp >= ? AND timestamp < ?
        GROUP BY artist
        ORDER BY count DESC
        LIMIT 25
    """, (start_ts, end_ts))
    top_artists = [{"artist": row[0], "count": row[1]} for row in c.fetchall()]

    c.execute("""
        SELECT artist, album, COUNT(*) as count
        FROM scrobbles
        WHERE timestamp >= ? AND timestamp < ? AND album IS NOT NULL AND album != ''
        GROUP BY artist, album
        ORDER BY count DESC
        LIMIT 25
    """, (start_ts, end_ts))
    top_albums = [{"artist": row[0], "album": row[1], "count": row[2]} for row in c.fetchall()]

    c.execute("""
        SELECT artist, track, COUNT(*) as count
        FROM scrobbles
        WHERE timestamp >= ? AND timestamp < ?
        GROUP BY artist, track
        ORDER BY count DESC
        LIMIT 25
    """, (start_ts, end_ts))
    top_tracks = [{"artist": row[0], "track": row[1], "count": row[2]} for row in c.fetchall()]

    c.execute("""
        SELECT strftime('%H', datetime(timestamp, 'unixepoch')) as hour, COUNT(*) as count
        FROM scrobbles
        WHERE timestamp >= ? AND timestamp < ?
        GROUP BY hour
        ORDER BY hour
    """, (start_ts, end_ts))
    rows = c.fetchall()
    hour_counts = [0] * 24
    for row in rows:
        hour_counts[int(row[0])] = row[1]
    conn.close()

    return jsonify({
        "year": year,
        "month": month,
        "total_scrobbles": total_scrobbles,
        "total_hours": total_hours,
        "top_artists": top_artists,
        "top_albums": top_albums,
        "top_tracks": top_tracks,
        "hour_counts": hour_counts
    })

@app.route('/api/genre_list')
def api_genre_list():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT DISTINCT genre FROM scrobbles WHERE genre IS NOT NULL AND genre != '' ORDER BY genre")
    rows = c.fetchall()
    conn.close()
    return jsonify([row[0] for row in rows])

@app.route('/api/genre_items')
def api_genre_items():
    genre = request.args.get('genre', '')
    if not genre:
        return jsonify({"error": "Missing genre"}), 400
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    # Artists
    c.execute("SELECT DISTINCT artist FROM scrobbles WHERE genre = ? ORDER BY artist", (genre,))
    artists = [row[0] for row in c.fetchall()]
    # Albums
    c.execute("SELECT DISTINCT artist, album FROM scrobbles WHERE genre = ? AND album IS NOT NULL AND album != '' ORDER BY artist, album", (genre,))
    albums = [{"artist": row[0], "album": row[1]} for row in c.fetchall()]
    # Tracks
    c.execute("SELECT DISTINCT artist, track FROM scrobbles WHERE genre = ? ORDER BY artist, track", (genre,))
    tracks = [{"artist": row[0], "track": row[1]} for row in c.fetchall()]
    # Total scrobbles
    c.execute("SELECT COUNT(*) FROM scrobbles WHERE genre = ?", (genre,))
    total = c.fetchone()[0]
    conn.close()
    return jsonify({
        "genre": genre,
        "total_scrobbles": total,
        "artists": artists,
        "albums": albums,
        "tracks": tracks
    })

# ----- DELETE SCROBBLE ENDPOINT -----
@app.route('/api/scrobble/<int:scrobble_id>', methods=['DELETE'])
def delete_scrobble(scrobble_id):
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("DELETE FROM scrobbles WHERE id = ?", (scrobble_id,))
        conn.commit()
        deleted = c.rowcount > 0
        conn.close()
        if deleted:
            print(f"🗑️ Deleted scrobble id {scrobble_id}")
            return jsonify({"status": "deleted"})
        else:
            return jsonify({"error": "Scrobble not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/scrobble/now', methods=['POST'])
def scrobble_now():
    with session["lock"]:
        if session["last_track_data"] and session["last_track_data"]["track"]:
            ld = session["last_track_data"]
            playing_from = current_track_data.get("playing_from", "").replace("Playing from: ", "")
            album_name = current_track_data.get("album", "")
            playlist_name = None
            if playing_from != album_name and playing_from not in IGNORED_PLAYLISTS:
                playlist_name = playing_from
            add_scrobble(ld["track"], ld["artist"], ld["album"], ld["art_url"], ld["duration_sec"],
                         ld["quality"], ld["bit_depth"], ld["sample_rate"], ld["codec"], playlist_name)
            return jsonify({"status": "scrobbled"})
    return jsonify({"status": "no track playing"}), 400

@app.route('/api/current_genre')
def api_current_genre():
    artist = request.args.get('artist', '')
    album = request.args.get('album', '')
    playlist = request.args.get('playlist', '')
    genre = None
    source = None

    # 1. Artist mapping
    if artist:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT genre FROM artist_genre_map WHERE artist = ?", (artist,))
        row = c.fetchone()
        if row:
            genre = row[0]
            source = 'artist'
        conn.close()

    # 2. Album mapping
    if not genre and album and artist:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT genre FROM album_genre_map WHERE album = ? AND artist = ?", (album, artist))
        row = c.fetchone()
        if row:
            genre = row[0]
            source = 'album'
        conn.close()

    # 3. MusicBrainz
    if not genre and artist:
        mb_genres = get_musicbrainz_genres(artist)
        if mb_genres:
            genre = mb_genres[0]
            source = 'musicbrainz'

    # 4. Playlist mapping
    if not genre and playlist:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT genre FROM playlist_genre_map WHERE playlist_name = ?", (playlist,))
        row = c.fetchone()
        if row:
            genre = row[0]
            source = 'playlist'
        conn.close()

    return jsonify({"genre": genre, "source": source})

@app.route('/api/last_scrobbled')
def api_last_scrobbled():
    artist = request.args.get('artist', '')
    track = request.args.get('track', '')
    if not artist or not track:
        return jsonify({"timestamp": None})
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT MAX(timestamp) FROM scrobbles WHERE artist=? AND track=?", (artist, track))
    row = c.fetchone()
    conn.close()
    ts = row[0] if row[0] else None
    return jsonify({"timestamp": ts})

@app.route('/api/search_scrobbles')
def api_search_scrobbles():
    track = request.args.get('track', '')
    artist = request.args.get('artist', '')
    album = request.args.get('album', '')
    playlist = request.args.get('playlist', '')
    genre = request.args.get('genre', '')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    limit = request.args.get('limit', 100, type=int)

    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    query = "SELECT * FROM scrobbles WHERE 1=1"
    params = []

    if track:
        query += " AND track LIKE ?"
        params.append(f"%{track}%")
    if artist:
        query += " AND artist LIKE ?"
        params.append(f"%{artist}%")
    if album:
        query += " AND album LIKE ?"
        params.append(f"%{album}%")
    if playlist:
        query += " AND playlist LIKE ?"
        params.append(f"%{playlist}%")
    if genre:
        query += " AND genre = ?"
        params.append(genre)
    if start_date:
        start_ts = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp())
        query += " AND timestamp >= ?"
        params.append(start_ts)
    if end_date:
        end_ts = int(datetime.strptime(end_date, "%Y-%m-%d").timestamp()) + 86400
        query += " AND timestamp < ?"
        params.append(end_ts)

    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)

    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])

@app.route('/api/track_playcount')
def api_track_playcount():
    artist = request.args.get('artist', '')
    track = request.args.get('track', '')
    if not artist or not track:
        return jsonify({"count": 0})
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM scrobbles WHERE artist=? AND track=?", (artist, track))
    count = c.fetchone()[0]
    conn.close()
    return jsonify({"count": count})

@app.route('/export')
def export_data():
    data = export_scrobbles_to_json()
    json_str = json.dumps(data, indent=2)
    return send_file(io.BytesIO(json_str.encode('utf-8')), mimetype='application/json',
                     as_attachment=True, download_name='scrobbles_export.json')

@app.route('/import', methods=['POST'])
def import_data():
    file = request.files.get('file')
    if not file:
        return jsonify({"error": "No file uploaded"}), 400
    try:
        data = json.load(file)
        if not isinstance(data, list):
            return jsonify({"error": "JSON must be a list"}), 400
        inserted = import_scrobbles_from_json(data)
        return jsonify({"status": f"Imported {inserted} new scrobbles"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route('/control/<action>', methods=['POST'])
def control(action):
    endpoints = {'playpause': 'playpause', 'next': 'next', 'previous': 'previous'}
    if action in endpoints:
        try:
            requests.post(f"{BASE_URL}/player/{endpoints[action]}", timeout=2)
        except:
            pass
    return ('', 204)

@app.route('/toggle_shuffle', methods=['POST'])
def toggle_shuffle():
    try:
        requests.post(f"{BASE_URL}/player/shuffle/toggle", headers={'accept': 'text/plain'}, data='', timeout=2)
    except:
        pass
    return ('', 204)

@app.route('/toggle_repeat', methods=['POST'])
def toggle_repeat():
    try:
        requests.post(f"{BASE_URL}/player/repeat/toggle", headers={'accept': 'text/plain'}, data='', timeout=2)
    except:
        pass
    return ('', 204)

@app.route('/seek/<int:seconds>', methods=['PUT'])
def seek(seconds):
    try:
        requests.put(f"{BASE_URL}/player/seek/absolute?seconds={seconds}", timeout=2)
    except:
        pass
    return ('', 204)

@app.route('/cache/clear', methods=['GET', 'POST'])
def clear_cache():
    global album_cache, last_cached_album
    album_cache.clear()
    last_cached_album = None
    print("Album cache cleared")
    return ('', 204)

@app.route('/cache/view')
def view_cache():
    if not album_cache:
        return "<h3>Cache is empty</h3><p><a href='/'>Back</a></p>"
    html = "<h3>Cached Albums</h3><ul>"
    for key, val in album_cache.items():
        html += f"<li><strong>{key}</strong><br>&nbsp;&nbsp;{val.get('badgeText', 'N/A')}</li>"
    html += "</ul><p><a href='/'>Back</a></p>"
    return html

@app.route('/api/monthly_trend')
def api_monthly_trend():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("""
        SELECT strftime('%Y-%m', datetime(timestamp, 'unixepoch')) as month,
               COUNT(*) as scrobbles
        FROM scrobbles
        WHERE timestamp >= strftime('%s', 'now', '-12 months')
        GROUP BY month
        ORDER BY month
    """)
    rows = c.fetchall()
    conn.close()
    return jsonify([{"month": row[0], "scrobbles": row[1]} for row in rows])

@app.route('/api/playlists')
def api_playlists():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT DISTINCT playlist FROM scrobbles WHERE playlist IS NOT NULL AND playlist != '' ORDER BY playlist")
    rows = c.fetchall()
    conn.close()
    return jsonify([row[0] for row in rows])

@app.route('/api/rename_playlist', methods=['POST'])
def api_rename_playlist():
    data = request.get_json()
    old_name = data.get('old_name')
    new_name = data.get('new_name')
    if not old_name or not new_name:
        return jsonify({"error": "Missing old_name or new_name"}), 400
    if old_name == new_name:
        return jsonify({"status": "no change"})
    try:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("UPDATE scrobbles SET playlist = ? WHERE playlist = ?", (new_name, old_name))
        conn.commit()
        updated = c.rowcount
        conn.close()
        return jsonify({"status": "ok", "updated": updated})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------- GENRE MAPPING ENDPOINTS ----------
@app.route('/api/playlist_genre_map')
def api_playlist_genre_map():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT playlist_name, genre FROM playlist_genre_map ORDER BY playlist_name")
    rows = c.fetchall()
    conn.close()
    return jsonify([{"playlist": row[0], "genre": row[1]} for row in rows])

@app.route('/api/set_playlist_genre', methods=['POST'])
def api_set_playlist_genre():
    data = request.get_json()
    playlist = data.get('playlist')
    genre = data.get('genre')
    if not playlist:
        return jsonify({"error": "Missing playlist name"}), 400
    if not genre:
        # Delete mapping
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("DELETE FROM playlist_genre_map WHERE playlist_name = ?", (playlist,))
        conn.commit()
        conn.close()
        return jsonify({"status": "removed"})
    else:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO playlist_genre_map (playlist_name, genre) VALUES (?, ?)", (playlist, genre))
        conn.commit()
        conn.close()
        return jsonify({"status": "saved"})

@app.route('/api/backfill_genres', methods=['POST'])
def api_backfill_genres():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("""
        UPDATE scrobbles
        SET genre = (
            SELECT genre FROM playlist_genre_map
            WHERE playlist_genre_map.playlist_name = scrobbles.playlist
        )
        WHERE playlist IS NOT NULL AND playlist != ''
          AND EXISTS (SELECT 1 FROM playlist_genre_map WHERE playlist_name = scrobbles.playlist)
    """)
    updated = c.rowcount
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "updated": updated})

@app.route('/api/set_artist_genre', methods=['POST'])
def api_set_artist_genre():
    data = request.get_json()
    artist = data.get('artist')
    genre = data.get('genre')
    if not artist:
        return jsonify({"error": "Missing artist name"}), 400

    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()

    if not genre:
        # Delete mapping
        c.execute("DELETE FROM artist_genre_map WHERE artist = ?", (artist,))
        c.execute("UPDATE scrobbles SET genre = NULL WHERE artist = ?", (artist,))
    else:
        c.execute("INSERT OR REPLACE INTO artist_genre_map (artist, genre) VALUES (?, ?)", (artist, genre))
        c.execute("UPDATE scrobbles SET genre = ? WHERE artist = ?", (genre, artist))

    conn.commit()
    conn.close()
    return jsonify({"status": "saved"})

@app.route('/api/artist_genre_map')
def api_artist_genre_map():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT artist, genre FROM artist_genre_map ORDER BY artist")
    rows = c.fetchall()
    conn.close()
    return jsonify([{"artist": row[0], "genre": row[1]} for row in rows])

@app.route('/api/search_artists', methods=['GET'])
def api_search_artists():
    query = request.args.get('q', '')
    if not query:
        return jsonify([])
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT DISTINCT artist FROM scrobbles WHERE artist LIKE ? ORDER BY artist LIMIT 50", (f'%{query}%',))
    artists = [row[0] for row in c.fetchall()]
    if not artists:
        conn.close()
        return jsonify([])
    placeholders = ','.join('?' * len(artists))
    query_sql = f"""
        WITH artist_genre_counts AS (
            SELECT artist, genre, COUNT(*) as cnt
            FROM scrobbles
            WHERE artist IN ({placeholders}) AND genre IS NOT NULL AND genre != ''
            GROUP BY artist, genre
        ),
        ranked AS (
            SELECT artist, genre, cnt,
                   ROW_NUMBER() OVER (PARTITION BY artist ORDER BY cnt DESC) as rn
            FROM artist_genre_counts
        )
        SELECT artist, genre FROM ranked WHERE rn = 1
    """
    c.execute(query_sql, artists)
    rows = c.fetchall()
    conn.close()
    suggested = {row[0]: row[1] for row in rows}
    result = [{"artist": artist, "suggestedGenre": suggested.get(artist)} for artist in artists]
    return jsonify(result)

@app.route('/api/album_genre_map')
def api_album_genre_map():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT artist, album, genre FROM album_genre_map ORDER BY artist, album")
    rows = c.fetchall()
    conn.close()
    return jsonify([{"artist": row[0], "album": row[1], "genre": row[2]} for row in rows])

@app.route('/api/search_albums', methods=['GET'])
def api_search_albums():
    query = request.args.get('q', '')
    if not query:
        return jsonify([])
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT DISTINCT artist, album FROM scrobbles WHERE album IS NOT NULL AND album != '' AND album LIKE ? ORDER BY album LIMIT 50", (f'%{query}%',))
    rows = c.fetchall()
    conn.close()
    return jsonify([{"artist": row[0], "album": row[1]} for row in rows])

@app.route('/api/set_album_genre', methods=['POST'])
def api_set_album_genre():
    data = request.get_json()
    album = data.get('album')
    artist = data.get('artist')
    genre = data.get('genre')
    if not album or not artist:
        return jsonify({"error": "Missing album or artist"}), 400
    if not genre:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("DELETE FROM album_genre_map WHERE album = ? AND artist = ?", (album, artist))
        conn.commit()
        conn.close()
        return jsonify({"status": "removed"})
    else:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO album_genre_map (album, artist, genre) VALUES (?, ?, ?)", (album, artist, genre))
        conn.commit()
        conn.close()
        return jsonify({"status": "saved"})

@app.route('/api/backfill_artist_genres', methods=['POST'])
def api_backfill_artist_genres():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("""
        UPDATE scrobbles
        SET genre = (
            SELECT genre FROM artist_genre_map
            WHERE artist_genre_map.artist = scrobbles.artist
        )
        WHERE EXISTS (SELECT 1 FROM artist_genre_map WHERE artist = scrobbles.artist)
    """)
    updated = c.rowcount
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "updated": updated})

@app.route('/api/backfill_album_genres', methods=['POST'])
def api_backfill_album_genres():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("""
        UPDATE scrobbles
        SET genre = (
            SELECT genre FROM album_genre_map
            WHERE album_genre_map.album = scrobbles.album
              AND album_genre_map.artist = scrobbles.artist
        )
        WHERE EXISTS (
            SELECT 1 FROM album_genre_map
            WHERE album = scrobbles.album AND artist = scrobbles.artist
        )
    """)
    updated = c.rowcount
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "updated": updated})

@app.route('/api/artists_without_genre')
def api_artists_without_genre():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("""
        SELECT artist, MAX(timestamp) as last_scrobble
        FROM scrobbles
        WHERE (genre IS NULL OR genre = '')
          AND artist NOT IN (SELECT artist FROM artist_ignore_genre)
        GROUP BY artist
        ORDER BY last_scrobble DESC
        LIMIT 10
    """)
    rows = c.fetchall()
    conn.close()
    return jsonify([row[0] for row in rows])

@app.route('/api/ignore_artist_no_genre', methods=['POST'])
def api_ignore_artist_no_genre():
    data = request.get_json()
    artist = data.get('artist', '').strip()
    if not artist:
        return jsonify({"error": "Missing artist name"}), 400
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    try:
        c.execute("INSERT OR IGNORE INTO artist_ignore_genre (artist) VALUES (?)", (artist,))
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/unignore_artist_no_genre', methods=['POST'])
def api_unignore_artist_no_genre():
    data = request.get_json()
    artist = data.get('artist', '').strip()
    if not artist:
        return jsonify({"error": "Missing artist name"}), 400
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("DELETE FROM artist_ignore_genre WHERE artist = ?", (artist,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route('/api/backfill_musicbrainz_genres', methods=['POST'])
def api_backfill_musicbrainz_genres():
    # Fetch all untagged scrobble artists (distinct, to minimise API calls)
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT DISTINCT artist FROM scrobbles WHERE genre IS NULL OR genre=''")
    artists = [row[0] for row in c.fetchall()]
    conn.close()

    total_updated = 0
    for artist in artists:
        if not artist:
            continue
        genres = get_musicbrainz_genres(artist)
        if not genres:
            continue
        top_genre = genres[0]

        # Batch update all scrobbles for this artist that still have no genre
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("UPDATE scrobbles SET genre=? WHERE artist=? AND (genre IS NULL OR genre='')",
                  (top_genre, artist))
        updated = c.rowcount
        conn.commit()
        conn.close()
        total_updated += updated

    print(f"🎵 MusicBrainz backfill: {total_updated} scrobbles updated.")
    return jsonify({"status": "ok", "updated": total_updated})

@app.route('/api/mb_retag_artist/<artist_name>')
def api_mb_retag_artist(artist_name):
    # Delete the cache entry so we fetch fresh data
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("DELETE FROM artist_musicbrainz_cache WHERE artist_lower = ?", (artist_name.lower(),))
    conn.commit()
    conn.close()

    genres = get_musicbrainz_genres(artist_name)
    if not genres:
        return jsonify({"status": "no genres found"}), 404

    top_genre = genres[0]
    # Update all scrobbles of this artist
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("UPDATE scrobbles SET genre = ? WHERE artist = ?", (top_genre, artist_name))
    updated = c.rowcount
    conn.commit()
    conn.close()

    return jsonify({"status": "ok", "updated": updated, "genre": top_genre})

# ------------------------- HTML TEMPLATES -------------------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title id="page-title">Now Playing</title>
    <link id="favicon" rel="icon" type="image/png" href="">
    <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
    <style>
        body { margin:0; font-family:-apple-system; color:white; overflow:hidden; }
        .bg { position:fixed; width:100%; height:100%; background-size:cover; filter:blur(10px) brightness(0.35); z-index:-1; transition: background-image 0.5s ease; }
        .overlay { display:flex; flex-direction:column; align-items:center; justify-content:center; height:100vh; }
        .card { display:flex; gap:40px; background:rgba(0,0,0,0.4); padding:30px; border-radius:20px; backdrop-filter:blur(20px); max-width:90vw; margin-bottom:20px; }
        .art { width:260px; border-radius:16px; }
        .info { display:flex; flex-direction:column; justify-content:center; min-width: 400px; flex: 1; }
        .track { font-size:2em; word-break:break-word; }
        .artist { color:#ccc; }
        .album { color:#999; margin-bottom:4px; }
        .stats-line { font-size:0.85em; color:#aaa; }
        .playing-from { font-size:0.9em; margin-top:5px; margin-bottom:10px; display:flex; align-items:center; gap:8px; }
        .progress-container { width:100%; height:6px; background:rgba(255,255,255,0.2); border-radius:10px; overflow:hidden; cursor:default; }
        .progress { height:100%; background:#1db954; width:0%; transition:width 0.2s linear; }
        .time { display:flex; justify-content:space-between; font-size:0.8em; color:#aaa; }
        .controls { margin-top:20px; display:flex; gap:20px; flex-wrap: wrap; }
        .btn { background:rgba(255,255,255,0.1); border:none; color:white; padding:10px 15px; border-radius:10px; cursor:pointer; font-size:1em; transition: background-color 0.2s ease; }
        .btn:hover { background:rgba(255,255,255,0.25); }
        .tag-bar {
            background: rgba(0,0,0,0.3);
            backdrop-filter: blur(10px);
            border-radius: 12px;
            padding: 6px 12px;
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            justify-content: center;
            gap: 20px;
            font-size: 0.8rem;
            margin-top: 24px;
            margin-bottom: 0;
        }
        .tag-bar strong { font-weight: 600; margin-right: 4px; }
        .tag-bar .btn-sm {
            background: rgba(255,255,255,0.15);
            border: none;
            color: white;
            padding: 4px 12px;
            border-radius: 20px;
            cursor: pointer;
            font-size: 0.7rem;
            transition: background 0.2s;
        }
        .tag-bar .btn-sm:hover { background: rgba(255,255,255,0.3); }
        .tag-item { display: inline-flex; align-items: center; gap: 8px; }
        .meta { margin-top:10px; font-size:0.85em; color:#bbb; display: flex; justify-content: flex-start; align-items: center; flex-wrap: wrap; gap: 4px; }
        .quality-badge, .genre-badge { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 12px; font-size: 0.85em; font-weight: 600; background: rgba(255,255,255,0.1); }
        .bitrate { font-size:0.95em; font-family: monospace; padding: 4px 10px; border-radius: 12px; backdrop-filter: blur(8px); display: inline-block; margin-left: auto; }
        .bitrate-max { color: #FFB347; background-color: rgba(255, 179, 71, 0.20); }
        .bitrate-high { color: #40E0D0; background-color: rgba(64, 224, 208, 0.20); }
        .bitrate-low { color: #888888; background-color: rgba(136, 136, 136, 0.20); }
        .clickable { cursor: pointer; padding: 2px 6px; border-radius: 8px; transition: background-color 0.2s ease; display: inline-block; }
        .clickable:hover { background-color: rgba(255,255,255,0.2); }
        @media (max-width: 768px) {
            .card { flex-direction: column; align-items: center; gap:20px; padding:20px; }
            .art { width:200px; }
            .track { font-size:1.5em; text-align:center; }
            .artist, .album { text-align:center; }
            .playing-from { justify-content:center; }
            .info { min-width: 280px; }
            .meta { flex-direction: column; gap: 5px; text-align: center; }
            .controls { justify-content: center; }
            .tag-bar { flex-direction: column; align-items: stretch; border-radius: 12px; padding: 8px; gap: 10px; }
            .tag-item { justify-content: space-between; }
        }
        #genreModal {
            display: flex;
        }
        #genreModal input {
            background: var(--button-bg);
            color: var(--text-primary);
            border: 1px solid var(--border-card);
        }
        #genreModal button {
            transition: opacity 0.2s;
        }
        #genreModal button:hover {
            opacity: 0.9;
        }
    </style>
</head>
<body>
    <div id="bg" class="bg"></div>
    <div class="overlay">
        <div class="card">
            <img id="art" class="art" src="" />
            <div class="info">
                <div style="display:flex; align-items:center; gap:8px;">
                    <div style="width:1.8em; display:flex; justify-content:center; flex-shrink:0;">
                        <button id="favBtn" style="background:none; border:none; font-size:1.6em; cursor:pointer; color:#ccc; padding:0; line-height:1;" title="Add to favourites">♡</button>
                    </div>
                    <div id="track" class="track"></div>
                </div>
                <div style="display:flex; align-items:center; gap:8px;">
                    <div style="width:1.8em; flex-shrink:0;"></div>
                    <div id="artist" class="artist"></div>
                </div>
                <div style="display:flex; align-items:center; gap:8px;">
                    <div style="width:1.8em; display:flex; justify-content:center; flex-shrink:0;">
                        <button id="favAlbumBtn" style="background:none; border:none; font-size:1.6em; cursor:pointer; color:grey; padding:0; line-height:1;" title="Add album to favourites">☆</button>
                    </div>
                    <div id="album" class="album"></div>
                </div>

                <!-- Last played and play count, tightly stacked -->
                <div style="margin-bottom:12px; line-height:1.4;">
                    <div id="lastScrobbled" class="stats-line"></div>
                    <div id="trackPlayCount" class="stats-line"></div>
                </div>

                <div id="playingFrom" class="playing-from"></div>
                <div class="progress-container" id="progress-container">
                    <div id="progress" class="progress"></div>
                </div>
                <div class="time"><span id="current"></span><span id="duration"></span></div>
                <div class="controls">
                    <button class="btn" onclick="control('previous')">⏮ Previous</button>
                    <button class="btn" onclick="control('playpause')">⏯ Play/Pause</button>
                    <button class="btn" onclick="control('next')">⏭ Next</button>
                </div>
                <div class="meta">
                    <span id="scrobbleCounter" style="margin-right:8px; font-size:0.9em; color:#ccc; font-weight:500; position: relative; top: 1px;">📀 <span id="totalScrobblesDisplay" style="font-weight:bold;">...</span></span>
                    <div style="display: flex; gap: 8px; flex-wrap: wrap; align-items: center;">
                        <span id="metaText"></span>
                        <span id="currentGenre" class="genre-badge" style="display: none;"></span>
                        <button id="mbIgnoreBtn" class="btn-sm" style="display:none; background:rgba(255,255,255,0.08); border:none; color:#ccc; padding:2px 6px; border-radius:8px; cursor:pointer; font-size:0.8em;" title="Ignore this MusicBrainz tag">🚫</button>
                        <button id="mbRetagBtn" class="btn-sm" style="display:none; background:rgba(255,255,255,0.08); border:none; color:#ccc; padding:2px 6px; border-radius:8px; cursor:pointer; font-size:0.8em;" title="Re‑fetch MusicBrainz genre">🔄</button>
                        <span id="qualityBadge" class="quality-badge"></span>
                    </div>
                    <span id="bitrate" class="bitrate"></span>
                </div>

                <!-- Genre tagging moved inside the card -->
                <div class="tag-bar">
                    <div class="tag-item">
                        <strong>Artist:</strong> <span id="currentArtistForTag">-</span>
                        <button id="tagArtistBtn" class="btn-sm">🏷️ Tag Genre</button>
                    </div>
                    <div class="tag-item">
                        <strong>Album:</strong> <span id="currentAlbumForTag">-</span>
                        <button id="tagAlbumBtn" class="btn-sm">🏷️ Tag Genre</button>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <div id="genreModal" style="display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); z-index: 1000; justify-content: center; align-items: center;">
        <div style="background: var(--bg-card); border-radius: 16px; padding: 1.5rem; width: 300px; max-width: 90%; box-shadow: 0 4px 12px rgba(0,0,0,0.5); color: var(--text-primary);">
            <h3 id="modalTitle" style="margin-top: 0; color: var(--accent);">Tag Genre</h3>
            <p id="modalItemName" style="margin-bottom: 1rem;"></p>
            <label for="genreInput" style="display: block; margin-bottom: 4px;">Genre:</label>
            <input list="genreList" id="genreInput" placeholder="Type or select a genre" style="width: 100%; padding: 8px; margin-bottom: 8px; border-radius: 8px; border: 1px solid var(--border-card); background: var(--button-bg); color: var(--text-primary);">
            <datalist id="genreList"></datalist>
            <div style="display: flex; gap: 10px; margin-top: 1rem;">
                <button id="modalSaveBtn" style="flex:1; background: var(--accent); border: none; padding: 8px; border-radius: 8px; color: white; cursor: pointer;">Save</button>
                <button id="modalCancelBtn" style="flex:1; background: #6c757d; border: none; padding: 8px; border-radius: 8px; color: white; cursor: pointer;">Cancel</button>
            </div>
        </div>
    </div>

<script>
const socket = io({ reconnection: true, reconnectionAttempts: Infinity, reconnectionDelay: 1000 });
let trackDurationSec = 0;
const artCache = new Map();
let currentArtist = "";
let currentAlbum = "";
let currentPlaylist = "";

function updateArt(url) {
    if (!url) return;
    if (artCache.has(url)) {
        document.getElementById('art').src = artCache.get(url);
        document.getElementById('bg').style.backgroundImage = `url('${artCache.get(url)}')`;
        return;
    }
    const img = new Image();
    img.onload = () => { artCache.set(url, img.src); document.getElementById('art').src = img.src; document.getElementById('bg').style.backgroundImage = `url('${img.src}')`; };
    img.src = url;
}

const QUALITY_DISPLAY_MAP = { 'HI_RES_LOSSLESS': 'HI_RES_LOSSLESS', 'LOSSLESS': 'LOSSLESS', 'max': 'HI_RES_LOSSLESS', 'high': 'LOSSLESS', 'low': 'Low' };
const BITRATE_DEFAULTS = { 'hi_res_lossless': '24-bit 44.1kHz', 'lossless': '16-bit 44.1kHz', 'low': '96 kbps' };

function updateBitrateColor(qualityType, bitrateText) {
    const el = document.getElementById('bitrate');
    el.classList.remove('bitrate-max', 'bitrate-high', 'bitrate-low');
    if (qualityType === 'max' || qualityType === 'hi_res_lossless') el.classList.add('bitrate-max');
    else if (qualityType === 'high' || qualityType === 'lossless') el.classList.add('bitrate-high');
    else el.classList.add('bitrate-low');
}

function getQualityDisplay(q) { return QUALITY_DISPLAY_MAP[q] || q; }
function getBitrateText(quality, bitDepth, sampleRate, badgeText) {
    if (badgeText && /(bit|kHz|kbps)/.test(badgeText)) return badgeText;
    if (bitDepth && sampleRate) return `${bitDepth}-bit ${sampleRate/1000}kHz`;
    return BITRATE_DEFAULTS[quality] || 'Unknown';
}

function toggleShuffle() { fetch('/toggle_shuffle', { method: 'POST' }); }
function toggleRepeat() { fetch('/toggle_repeat', { method: 'POST' }); }

function updateTaggingInfo(data) {
    currentArtist = data.artist;
    currentAlbum = data.album;
    currentPlaylist = data.playing_from.replace(/^Playing from: /, '');
    document.getElementById('currentArtistForTag').innerText = currentArtist || "-";
    document.getElementById('currentAlbumForTag').innerText = currentAlbum || "-";
}

function fetchCurrentGenre(artist, album, playlist) {
    const url = `/api/current_genre?artist=${encodeURIComponent(artist)}&album=${encodeURIComponent(album)}&playlist=${encodeURIComponent(playlist)}`;
    fetch(url)
        .then(r => r.json())
        .then(data => {
            const genreSpan = document.getElementById('currentGenre');
            const retagBtn = document.getElementById('mbRetagBtn');
            const ignoreBtn = document.getElementById('mbIgnoreBtn');
            if (data.genre && data.genre !== '') {
                let displayGenre = data.genre;
                if (!displayGenre.startsWith('🏷️')) {
                    displayGenre = `🏷️ ${displayGenre}`;
                }
                genreSpan.innerText = displayGenre;
                genreSpan.style.display = 'inline-flex';
                retagBtn.style.display = 'inline-block';
                if (data.source === 'musicbrainz') {
                    ignoreBtn.style.display = 'inline-block';
                } else {
                    ignoreBtn.style.display = 'none';
                }
            } else {
                genreSpan.style.display = 'none';
                retagBtn.style.display = 'none';
                ignoreBtn.style.display = 'none';
            }
        })
        .catch(e => console.error("Genre fetch error:", e));
}

function fetchTotalScrobbles() {
    fetch('/api/stats')
        .then(r => r.json())
        .then(data => {
            document.getElementById('totalScrobblesDisplay').innerText = data.total_scrobbles;
        })
        .catch(() => {});
}

function formatRelativeTimeShort(ts) {
    const diff = Math.floor((Date.now() / 1000) - ts);
    if (diff < 60) return 'just now';
    const mins = Math.floor(diff / 60);
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    if (days < 7) return `${days}d ago`;
    const weeks = Math.floor(days / 7);
    if (weeks < 4) return `${weeks}w ago`;
    return new Date(ts * 1000).toLocaleDateString();
}

function fetchTrackPlayCount(artist, track) {
    if (!artist || !track) return;
    fetch(`/api/track_playcount?artist=${encodeURIComponent(artist)}&track=${encodeURIComponent(track)}`)
        .then(r => r.json())
        .then(data => {
            const el = document.getElementById('trackPlayCount');
            if (data.count > 0) {
                const times = data.count === 1 ? 'time' : 'times';
                el.innerText = `Played ${data.count} ${times}`;
            } else {
                el.innerText = '';
            }
        })
        .catch(() => {});
}

// FAVOURITE TRACK
let isFavourite = false;

function updateFavButton() {
    const btn = document.getElementById('favBtn');
    if (isFavourite) {
        btn.innerHTML = '🤍';          // white heart
        btn.style.fontSize = '1.1em';  // slightly smaller because emoji is larger
        btn.title = 'Remove from favourites';
    } else {
        btn.innerHTML = '♡';           // hollow heart
        btn.style.fontSize = '1.6em';  // match the star size
        btn.title = 'Add to favourites';
    }
}

function checkFavourite(artist, track) {
    fetch(`/api/favourite/check?artist=${encodeURIComponent(artist)}&track=${encodeURIComponent(track)}`)
        .then(r => r.json())
        .then(data => {
            isFavourite = data.favourite;
            updateFavButton();
        })
        .catch(() => {});
}

document.getElementById('favBtn').addEventListener('click', () => {
    if (!currentArtist || !document.getElementById('track').innerText) return;
    const track = document.getElementById('track').innerText;
    const album = currentAlbum || '';
    const art = document.getElementById('art').src || '';

    // 1. Toggle local database favourite
    fetch('/api/favourite/toggle', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ artist: currentArtist, track: track, album: album, art_url: art })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'added') isFavourite = true;
        else if (data.status === 'removed') isFavourite = false;
        updateFavButton();
    })
    .catch(e => console.error(e));

    // 2. Toggle favourite in TIDAL desktop app (proxied through backend)
    fetch('/api/tidal/favorite/toggle', { method: 'POST' }).catch(() => {});
});

// FAVOURITE ALBUM
let isFavouriteAlbum = false;

function updateFavAlbumButton() {
    const btn = document.getElementById('favAlbumBtn');
    if (isFavouriteAlbum) {
        btn.innerHTML = '★';
        btn.style.color = '#FFD700';
        btn.title = 'Remove album from favourites';
    } else {
        btn.innerHTML = '☆';
        btn.style.color = 'grey';
        btn.title = 'Add album to favourites';
    }
}

function checkFavouriteAlbum(artist, album) {
    if (!artist || !album) return;
    fetch(`/api/favourite_album/check?artist=${encodeURIComponent(artist)}&album=${encodeURIComponent(album)}`)
        .then(r => r.json())
        .then(data => {
            isFavouriteAlbum = data.favourite;
            updateFavAlbumButton();
        })
        .catch(() => {});
}

document.getElementById('favAlbumBtn').addEventListener('click', () => {
    if (!currentArtist || !currentAlbum) return;
    const art = document.getElementById('art').src || '';
    fetch('/api/favourite_album/toggle', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ artist: currentArtist, album: currentAlbum, art_url: art })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'added') isFavouriteAlbum = true;
        else if (data.status === 'removed') isFavouriteAlbum = false;
        updateFavAlbumButton();
    })
    .catch(e => console.error(e));
});

// RETAG & IGNORE BUTTONS
function setupRetagButton() {
    const btn = document.getElementById('mbRetagBtn');
    if (btn) {
        btn.addEventListener('click', () => {
            if (!currentArtist) { alert('No artist playing'); return; }
            btn.disabled = true;
            btn.innerText = '⏳';
            fetch(`/api/mb_retag_artist/${encodeURIComponent(currentArtist)}`)
                .then(r => r.json())
                .then(data => {
                    if (data.status === 'ok') {
                        alert(`Genre re‑tagged to "${data.genre}" for ${data.updated} scrobbles.`);
                    } else {
                        alert('MusicBrainz returned no genres for this artist.');
                    }
                    fetch(`/api/mb_retag_artist/${encodeURIComponent(currentArtist)}`).catch(() => {});
                    fetchCurrentGenre(currentArtist, currentAlbum, currentPlaylist);
                })
                .catch(e => alert('Retag failed: ' + e))
                .finally(() => {
                    btn.disabled = false;
                    btn.innerText = '🔄';
                });
        });

        document.getElementById('mbIgnoreBtn').addEventListener('click', () => {
            if (!currentArtist) return;
            const genreText = document.getElementById('currentGenre').innerText.replace('🏷️ ', '').trim();
            if (!genreText) return;
            if (!confirm(`Ignore MusicBrainz tag "${genreText}"?`)) return;

            const ignoreBtn = document.getElementById('mbIgnoreBtn');
            ignoreBtn.disabled = true;
            ignoreBtn.innerText = '⏳';

            fetch('/api/mb_blacklist/add', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ tag: genreText })
            })
            .then(r => r.json())
            .then(data => {
                if (data.status !== 'ok') {
                    alert('Error: ' + (data.error || 'unknown'));
                    ignoreBtn.disabled = false;
                    ignoreBtn.innerText = '🚫';
                    return;
                }
                return fetch(`/api/mb_retag_artist/${encodeURIComponent(currentArtist)}`);
            })
            .then(r => r && r.json())
            .then(retagData => {
                if (retagData && retagData.status === 'ok') {
                    fetchCurrentGenre(currentArtist, currentAlbum, currentPlaylist);
                } else if (retagData) {
                    alert('Cache cleared, but genre re‑fetch may have failed.');
                }
                ignoreBtn.disabled = false;
                ignoreBtn.innerText = '🚫';
            })
            .catch(e => {
                alert('Request failed: ' + e);
                ignoreBtn.disabled = false;
                ignoreBtn.innerText = '🚫';
            });
        });
    }
}

function updateUI(data) {
    document.getElementById('track').innerText = data.track;
    checkFavourite(data.artist, data.track);
    document.getElementById('artist').innerText = data.artist;
    document.getElementById('album').innerText = data.album;
    checkFavouriteAlbum(data.artist, data.album);
    document.getElementById('playingFrom').innerHTML = data.playing_from;
    document.getElementById('current').innerText = data.current;
    document.getElementById('duration').innerText = data.duration;
    document.getElementById('progress').style.width = data.progress + '%';
    document.getElementById('metaText').innerHTML = `💿 Volume: ${data.volume}% | 🔀 <span class="clickable" onclick="toggleShuffle()">Shuffle: ${data.shuffle}</span> | 🔁 <span class="clickable" onclick="toggleRepeat()">Repeat: ${data.repeat}</span>`;

    const badge = document.getElementById('qualityBadge');
    if (data.quality_raw) { badge.innerText = getQualityDisplay(data.quality_raw); badge.style.display = 'inline-flex'; }
    else { badge.style.display = 'none'; }

    const bitrateText = getBitrateText(data.quality, data.bitDepth, data.sampleRate, data.badgeText);
    document.getElementById('bitrate').innerText = bitrateText;
    updateBitrateColor(data.quality, bitrateText);
    updateArt(data.art);
    document.getElementById('page-title').innerText = `${data.artist} - ${data.track}`;
    document.getElementById('favicon').href = data.art;
    trackDurationSec = data.duration_sec || trackDurationSec;
    updateTaggingInfo(data);

    let playlistName = data.playing_from.replace(/^Playing from: /, '');
    fetchCurrentGenre(data.artist, data.album, playlistName);

    fetch(`/api/last_scrobbled?artist=${encodeURIComponent(data.artist)}&track=${encodeURIComponent(data.track)}`)
        .then(r => r.json())
        .then(res => {
            const el = document.getElementById('lastScrobbled');
            if (res.timestamp) {
                el.innerText = `Last played: ${formatRelativeTimeShort(res.timestamp)}`;
            } else {
                el.innerText = '';
            }
        })
        .catch(() => {});

    fetchTrackPlayCount(data.artist, data.track);
}

socket.on('update', (data) => updateUI(data));
socket.on('connect', () => console.log('Connected'));
socket.on('disconnect', () => setTimeout(() => socket.connect(), 1000));

function control(action) { fetch(`/control/${action}`, { method: 'POST' }); }
document.getElementById('progress-container').addEventListener('click', (e) => {
    const rect = e.target.closest('.progress-container').getBoundingClientRect();
    const percent = (e.clientX - rect.left) / rect.width;
    fetch(`/seek/${Math.floor(percent * trackDurationSec)}`, { method: 'PUT' });
});

document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    if (e.code === 'Space') { e.preventDefault(); control('playpause'); }
    else if (e.code === 'ArrowLeft') { control('previous'); }
    else if (e.code === 'ArrowRight') { control('next'); }
});

// ========== Genre Modal ==========
const escapeHtml = (str) => {
    if (!str) return '';
    return str.replace(/[&<>]/g, m => m === '&' ? '&amp;' : m === '<' ? '&lt;' : '&gt;');
};

let currentTagType = null;
let currentTagName = null;

function showGenreModal(type, name) {
    currentTagType = type;
    currentTagName = name;
    document.getElementById('modalTitle').innerText = type === 'artist' ? 'Tag Artist Genre' : 'Tag Album Genre';
    document.getElementById('modalItemName').innerHTML = `<strong>${escapeHtml(name)}</strong>`;
    document.getElementById('genreInput').value = '';
    fetch('/api/genre_list')
        .then(r => r.json())
        .then(genres => {
            const datalist = document.getElementById('genreList');
            datalist.innerHTML = '';
            genres.forEach(genre => {
                const option = document.createElement('option');
                option.value = genre;
                datalist.appendChild(option);
            });
        })
        .catch(e => console.error('Error loading genres:', e));
    document.getElementById('genreModal').style.display = 'flex';
}

function closeModal() {
    document.getElementById('genreModal').style.display = 'none';
}

function saveGenre() {
    const genre = document.getElementById('genreInput').value.trim();
    if (!genre) {
        alert("Please enter or select a genre.");
        return;
    }
    if (currentTagType === 'artist') {
        fetch('/api/set_artist_genre', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ artist: currentTagName, genre: genre })
        })
        .then(r => r.json())
        .then(data => {
            if (data.error) alert('Error: ' + data.error);
            else alert(`Genre "${genre}" saved for artist "${currentTagName}".`);
            closeModal();
        })
        .catch(e => alert('Request failed: ' + e));
    } else if (currentTagType === 'album') {
        fetch('/api/set_album_genre', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ album: currentTagName, artist: currentArtist, genre: genre })
        })
        .then(r => r.json())
        .then(data => {
            if (data.error) alert('Error: ' + data.error);
            else alert(`Genre "${genre}" saved for album "${currentTagName}".`);
            closeModal();
        })
        .catch(e => alert('Request failed: ' + e));
    }
}

document.getElementById('tagArtistBtn').addEventListener('click', () => {
    if (!currentArtist) {
        alert("No artist playing.");
        return;
    }
    showGenreModal('artist', currentArtist);
});
document.getElementById('tagAlbumBtn').addEventListener('click', () => {
    if (!currentAlbum || !currentArtist) {
        alert("No album playing.");
        return;
    }
    showGenreModal('album', currentAlbum);
});
document.getElementById('modalSaveBtn').addEventListener('click', saveGenre);
document.getElementById('modalCancelBtn').addEventListener('click', closeModal);

// ========== SO & RYM buttons ==========
(function() {
    function addButtons() {
        const card = document.querySelector('.card');
        if (!card) return;

        // --- SO Button (left of RYM) ---
        if (!document.getElementById('soButton')) {
            const soBtn = document.createElement('button');
            soBtn.id = 'soButton';
            soBtn.innerHTML = '📀';
            soBtn.title = 'Open Scrobble Overview';
            Object.assign(soBtn.style, {
                position: 'absolute',
                top: '10px',
                right: '70px',
                background: 'rgba(0,0,0,0.5)',
                backdropFilter: 'blur(4px)',
                border: 'none',
                borderRadius: '20px',
                padding: '4px 10px',
                fontSize: '0.7rem',
                cursor: 'pointer',
                color: 'white',
                fontFamily: 'inherit',
                zIndex: '100',
                transition: 'background 0.2s'
            });
            soBtn.addEventListener('mouseenter', () => soBtn.style.background = 'rgba(0,0,0,0.7)');
            soBtn.addEventListener('mouseleave', () => soBtn.style.background = 'rgba(0,0,0,0.5)');
            if (getComputedStyle(card).position !== 'relative') card.style.position = 'relative';
            card.appendChild(soBtn);
            soBtn.addEventListener('click', () => window.open('/scrobbles', '_blank'));
        }

        // --- RYM Button (right edge) ---
        if (!document.getElementById('rymButton')) {
            const rymBtn = document.createElement('button');
            rymBtn.id = 'rymButton';
            rymBtn.innerHTML = '🎵 RYM';
            rymBtn.title = 'Search on RateYourMusic';
            Object.assign(rymBtn.style, {
                position: 'absolute',
                top: '10px',
                right: '10px',
                background: 'rgba(0,0,0,0.5)',
                backdropFilter: 'blur(4px)',
                border: 'none',
                borderRadius: '20px',
                padding: '4px 10px',
                fontSize: '0.7rem',
                cursor: 'pointer',
                color: 'white',
                fontFamily: 'inherit',
                zIndex: '100',
                transition: 'background 0.2s'
            });
            rymBtn.addEventListener('mouseenter', () => rymBtn.style.background = 'rgba(0,0,0,0.7)');
            rymBtn.addEventListener('mouseleave', () => rymBtn.style.background = 'rgba(0,0,0,0.5)');
            if (getComputedStyle(card).position !== 'relative') card.style.position = 'relative';
            card.appendChild(rymBtn);
            rymBtn.addEventListener('click', () => {
                const artist = document.getElementById('artist').innerText;
                const album = document.getElementById('album').innerText;
                if (!artist || artist === '-') { alert('No artist playing'); return; }
                const url = `https://rateyourmusic.com/search?searchtype=release&searchterm=${encodeURIComponent(artist)}%20-%20${encodeURIComponent(album)}`;
                window.open(url, '_blank');
            });
        }
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', addButtons);
    else addButtons();
    setTimeout(addButtons, 1000);
})();

fetchTotalScrobbles();
setInterval(fetchTotalScrobbles, 60000);
setupRetagButton();
</script>
</body>
</html>
"""

SCROBBLES_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Scrobble Overview · TIDAL</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Cdefs%3E%3CradialGradient id='grad' cx='50%25' cy='50%25' r='50%25'%3E%3Cstop offset='0%25' stop-color='%23e0e0e0'/%3E%3Cstop offset='70%25' stop-color='%23a0a0a0'/%3E%3Cstop offset='100%25' stop-color='%23404040'/%3E%3C/radialGradient%3E%3C/defs%3E%3Ccircle cx='50' cy='50' r='48' fill='url(%23grad)' stroke='%23333' stroke-width='2'/%3E%3Ccircle cx='50' cy='50' r='12' fill='%23333'/%3E%3C/svg%3E">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
        body.dark .theme-toggle {
            background: #3a3a3a;
            border-color: #666;
            color: #ffffff;
        }
        body.dark .theme-toggle:hover {
            background: #555;
            border-color: #888;
        }
        :root {
            --bg-body: #ececec;
            --bg-card: #f5f5f5;
            --bg-tools: #ffffff;
            --text-primary: #2c2c2c; 
            --text-secondary: #666;
            --text-muted: #999;
            --border-light: #e5e5e5;
            --border-card: #eee;
            --accent: #d51007;
            --accent-hover: #b00;
            --button-bg: #f5f5f5;
            --button-border: #ddd;
            --button-hover: #e9e9e9;
            --hover-row: #fef9f9;
            --shadow: rgba(0,0,0,0.05);
            --art-bg: #f0f0f0;
        }
        body.dark {
            --bg-body: #121212;
            --bg-card: #1e1e1e;
            --bg-tools: #1e1e1e;
            --text-primary: #eee;
            --text-secondary: #bbb;
            --text-muted: #888;
            --border-light: #2c2c2c;
            --border-card: #2c2c2c;
            --accent: #ff6b6b;
            --accent-hover: #ff8a8a;
            --button-bg: #2c2c2c;
            --button-border: #444;
            --button-hover: #3a3a3a;
            --hover-row: #2a2a2a;
            --shadow: rgba(0,0,0,0.3);
            --art-bg: #2c2c2c;
        }
        * { box-sizing: border-box; }
        body {
            font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
            background: var(--bg-body);
            margin: 0;
            padding: 0;
            color: var(--text-primary);
            transition: background 0.2s, color 0.2s;
        }
        .container { max-width: 1200px; margin: 0 auto; padding: 2rem 1rem; }
        .header { display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; margin-bottom: 2rem; border-bottom: 1px solid var(--border-light); padding-bottom: 1rem; }
        h1 { font-size: 1.8rem; font-weight: 500; margin: 0; color: var(--accent); letter-spacing: -0.5px; }
        .sub { color: var(--text-muted); font-size: 0.85rem; }
        .player-link { background: var(--button-bg); border: 1px solid var(--button-border); padding: 0.4rem 1rem; border-radius: 20px; text-decoration: none; color: var(--accent); font-size: 0.85rem; font-weight: 500; transition: all 0.2s; }
        .player-link:hover { background: var(--accent); color: white; border-color: var(--accent); }
        .report-link { background: var(--button-bg); border: 1px solid var(--button-border); padding: 0.4rem 1rem; border-radius: 20px; text-decoration: none; color: var(--accent); font-size: 0.85rem; font-weight: 500; transition: all 0.2s; margin-left: 10px; }
        .report-link:hover { background: var(--accent); color: white; border-color: var(--accent); }
        .now-playing { background: var(--bg-card); border-radius: 16px; padding: 1.5rem; margin-bottom: 2rem; display: flex; gap: 1.5rem; align-items: center; box-shadow: 0 2px 8px var(--shadow); border: 1px solid var(--border-card); }
        .now-art { width: 80px; height: 80px; border-radius: 12px; object-fit: cover; background: var(--art-bg); }
        .now-info { flex: 1; }
        .now-label { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 1px; color: var(--accent); font-weight: 600; }
        .now-track { font-size: 1.4rem; font-weight: 600; margin: 0.2rem 0; }
        .now-artist, .now-album { color: var(--text-secondary); font-size: 0.9rem; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1.5rem; margin-bottom: 1.5rem; }
        .stat-card { background: var(--bg-card); border-radius: 16px; padding: 1rem; box-shadow: 0 1px 4px var(--shadow); border: 1px solid var(--border-card); }
        .stat-card h3 { margin: 0 0 1rem 0; font-size: 1.2rem; font-weight: 500; color: var(--accent); border-left: 3px solid var(--accent); padding-left: 0.75rem; }
        .stat-list { list-style: none; padding: 0; margin: 0; max-height: 300px; overflow-y: auto;  overflow: hidden; padding-right: 5px; }
        .stat-list li { display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid var(--border-card); }
        .stat-list img { width: 24px; height: 24px; border-radius: 6px; object-fit: cover; background: var(--art-bg); flex-shrink: 0; }
        .stat-list li span:first-child { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .stat-count { font-weight: 600; color: var(--accent); flex-shrink: 0; margin-left: auto; }
        .stat-list::-webkit-scrollbar { width: 6px; }
        .stat-list::-webkit-scrollbar-track { background: var(--border-card); border-radius: 3px; }
        .stat-list::-webkit-scrollbar-thumb { background: #aaa; border-radius: 3px; }
        body.dark .stat-list::-webkit-scrollbar-thumb { background: #aaa; }
        .time-card { background: var(--bg-card); border-radius: 16px; padding: 0.6rem 1rem; margin-bottom: 1.5rem; text-align: center; box-shadow: 0 1px 4px var(--shadow); border: 1px solid var(--border-card); }
        .time-stats { display: flex; justify-content: space-around; flex-wrap: wrap; gap: 0.5rem; margin-top: 0; }
        .time-item { text-align: center; min-width: 70px; }
        .total-item { text-align: center; margin-top: 0.5rem; }
        .total-label { font-size: 0.65rem; text-transform: uppercase; letter-spacing: 1px; color: var(--text-muted); }
        .total-value { font-size: 1.2rem; font-weight: 600; color: var(--accent); }
        .time-label { font-size: 0.65rem; text-transform: uppercase; letter-spacing: 1px; color: var(--text-muted); }
        .time-value { font-size: 1.2rem; font-weight: 600; color: var(--accent); }
        .tools { background: var(--bg-tools); border-radius: 12px; padding: 1rem; margin-bottom: 2rem; display: flex; flex-wrap: wrap; gap: 12px; align-items: center; box-shadow: 0 1px 3px var(--shadow); border: 1px solid var(--border-card); }
        .tools button, .tools label { background: var(--button-bg); border: 1px solid var(--button-border); padding: 0.5rem 1rem; border-radius: 30px; font-size: 0.8rem; cursor: pointer; font-family: inherit; transition: all 0.2s; color: var(--text-primary); }
        .tools button:hover, .tools label:hover { background: var(--button-hover); border-color: var(--text-muted); }
        .theme-toggle { background: var(--button-bg); border: 1px solid var(--button-border); border-radius: 30px; padding: 0.5rem 1rem; font-size: 0.8rem; cursor: pointer; display: inline-flex; align-items: center; gap: 6px; }
        .scrobble-list { background: var(--bg-card); border-radius: 16px; box-shadow: 0 1px 4px var(--shadow); overflow: hidden; border: 1px solid var(--border-card); margin-top: 1rem; }
        .scrobble-item { display: flex; align-items: center; gap: 1rem; padding: 1rem; border-bottom: 1px solid var(--border-card); transition: background 0.15s; }
        .scrobble-item:hover {
            background: #f0f0f0 !important;
        }
        body.dark .scrobble-item:hover {
            background: #2a2a2a !important;
        }
        .album-art { flex-shrink: 0; width: 56px; height: 56px; border-radius: 8px; object-fit: cover; background: var(--art-bg); box-shadow: 0 1px 2px var(--shadow); }
        .track-info { flex: 1; min-width: 0; }
        .track-name { font-weight: 600; font-size: 1rem; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .artist-name { font-size: 0.85rem; color: var(--text-secondary); margin-top: 2px; }
        .album-name { font-size: 0.75rem; color: var(--text-muted); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .scrobble-date { flex-shrink: 0; font-size: 0.75rem; color: var(--text-muted); text-align: right; min-width: 120px; }
        .delete-scrobble {
            background: none;
            border: none;
            font-size: 1.2rem;
            cursor: pointer;
            opacity: 0.6;
            transition: opacity 0.2s;
            color: var(--text-secondary);
            padding: 0 4px;
        }
        .delete-scrobble:hover {
            opacity: 1;
            color: var(--accent);
        }
        .pagination { display: flex; justify-content: center; gap: 1rem; margin-top: 2rem; align-items: center; }
        .pagination button { background: var(--button-bg); border: 1px solid var(--button-border); padding: 0.5rem 1rem; border-radius: 30px; cursor: pointer; font-size: 0.8rem; transition: all 0.2s; color: var(--text-primary); }
        .pagination button:hover:not(:disabled) { background: var(--button-hover); border-color: var(--text-muted); }
        .pagination button:disabled { opacity: 0.4; cursor: default; }
        .pagination span { font-size: 0.85rem; color: var(--text-secondary); }
        .empty-message { padding: 3rem; text-align: center; color: var(--text-muted); font-size: 0.9rem; }
        footer { margin-top: 3rem; text-align: center; font-size: 0.7rem; color: var(--text-muted); }
        @media (max-width: 700px) {
            .scrobble-item { flex-wrap: wrap; }
            .scrobble-date { margin-left: 64px; text-align: left; width: 100%; }
            .now-playing { flex-direction: column; text-align: center; }
            .time-stats { flex-direction: column; align-items: center; }
            .delete-scrobble { margin-left: auto; }
        }
        canvas { max-height: 250px; width: auto; margin: 0 auto; display: block; }
        .new-stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1.5rem; margin-bottom: 1.5rem; }
        .highlight-text { text-align: center; font-size: 1.1rem; margin: 0.5rem 0; }
        #clockPieChart { margin-bottom: 10px; }
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <div>
            <h1>📀 Scrobble Overview</h1>
            <div class="sub">your listening history, top charts & now playing</div>
        </div>
        <div style="display: flex; gap: 10px;">
            <button class="theme-toggle" id="themeToggleBtn" onclick="toggleTheme()">🌓 Dark/Light</button>
            <a href="/" class="player-link" target="_blank">◀ Now Playing (full)</a>
            <a href="/monthly" class="report-link" target="_blank">📊 Reports & Tools</a>
        </div>
    </div>
    <div class="now-playing" id="nowPlaying">
        <img id="nowArt" class="now-art" src="" alt="album art">
        <div class="now-info">
            <div class="now-label">🎧 SCROBBLING NOW</div>
            <div class="now-track" id="nowTrack">-</div>
            <div class="now-artist" id="nowArtist">-</div>
            <div class="now-album" id="nowAlbum">-</div>
        </div>
    </div>

    <div class="stats-grid" id="statsGrid">
        <div class="stat-card"><h3>🎤 Top Artists</h3><ul class="stat-list" id="topArtistsList"><li>Loading...</li></ul></div>
        <div class="stat-card"><h3>💿 Top Albums</h3><ul class="stat-list" id="topAlbumsList"><li>Loading...</li></ul></div>
        <div class="stat-card"><h3>🎵 Top Tracks</h3><ul class="stat-list" id="topTracksList"><li>Loading...</li></ul></div>
    </div>

    <div class="time-card">
        <div class="time-stats" id="listeningTime">
            <div class="time-item"><div class="time-label">Today</div><div class="time-value" id="timeToday">-</div></div>
            <div class="time-item"><div class="time-label">This Week</div><div class="time-value" id="timeWeek">-</div></div>
            <div class="time-item"><div class="time-label">This Month</div><div class="time-value" id="timeMonth">-</div></div>
            <div class="time-item"><div class="time-label">This Year</div><div class="time-value" id="timeYear">-</div></div>
        </div>
        <div class="total-item">
            <div class="total-label">Total scrobbles</div>
            <div class="total-value" id="totalScrobbles">0</div>
        </div>
    </div>

    <div class="new-stats-grid">
        <div class="stat-card">
            <h3>🕒 Listening Clock (Busiest Hours)</h3>
            <canvas id="clockPieChart" width="300" height="200"></canvas>
            <div id="busiestHourInfo" style="text-align: center; margin-top: 15px; font-weight: bold;"></div>
        </div>
        <div class="stat-card">
            <h3>📅 Scrobbles by Day of Week</h3>
            <canvas id="weekdayChart" width="300" height="200"></canvas>
        </div>
        <div class="stat-card">
            <h3>⏱️ Top Artists by Listening Time (hours)</h3>
            <ul class="stat-list" id="topArtistsTimeList"><li>Loading...</li></ul>
        </div>
        <div class="stat-card">
            <h3>🏆 Top 10 Listening Days</h3>
            <ul class="stat-list" id="topDaysList">
                <li>Loading...</li>
            </ul>
        </div>
        <div class="stat-card">
            <h3>📀 Top Playlists</h3>
            <ul class="stat-list" id="topPlaylistsList"><li>Loading...</li></ul>
        </div>
        <div class="stat-card">
            <h3>🏷️ Top Genres</h3>
            <ul class="stat-list" id="topGenresList"><li>Loading...</li></ul>
        </div>
        <!-- New Genre Distribution pie chart -->
        <div class="stat-card">
            <h3>🥧 Genre Distribution</h3>
            <canvas id="genrePieChart" width="300" height="200"></canvas>
        </div>
        <div class="stat-card">
            <h3>⭐ Favourite Albums</h3>
            <ul class="stat-list" id="favouriteAlbumsList">
                <li>Loading...</li>
            </ul>
        </div>
        <div class="stat-card">
            <h3>🤍 Favourite Tracks</h3>
            <ul class="stat-list" id="favouritesList">
                <li>Loading...</li>
            </ul>
        </div>
    </div>

    <div class="tools">
        <button onclick="exportData()">⬇️ Export JSON</button>
        <label style="display:inline-flex; align-items:center; gap:6px;">📂 Import JSON
            <input type="file" id="importFile" style="display:none" accept=".json" onchange="importData(this.files[0])">
        </label>
        <button onclick="location.reload()">🔄 Refresh</button>
    </div>

    <h3 style="margin: 1rem 0 0.5rem 0;">Recent Scrobbles</h3>
    <div id="scrobbleList" class="scrobble-list">
        <div class="empty-message">Loading your scrobbles...</div>
    </div>

    <div class="pagination" id="pagination"></div>
    <footer>scrobbles stored in scrobbles.db · auto‑scrobbled after 50% or 4 minutes · synced with Last.fm</footer>
</div>

<script>
    let genreChart = null;  // for the pie chart

    function setTheme(theme) {
        if (theme === 'dark') { document.body.classList.add('dark'); } else { document.body.classList.remove('dark'); }
        localStorage.setItem('scrobbleTheme', theme);
    }
    function toggleTheme() { const isDark = document.body.classList.contains('dark'); setTheme(isDark ? 'light' : 'dark'); }
    const savedTheme = localStorage.getItem('scrobbleTheme');
    if (savedTheme === 'dark') document.body.classList.add('dark');

    function fetchNowPlaying() {
        fetch('/api/now').then(r => r.json()).then(data => {
            document.getElementById('nowTrack').innerText = data.track || 'Nothing playing';
            document.getElementById('nowArtist').innerText = data.artist || '-';
            document.getElementById('nowAlbum').innerText = data.album || '-';
            const artEl = document.getElementById('nowArt');
            artEl.src = (data.art && data.art !== '') ? data.art : 'https://via.placeholder.com/80?text=🎵';
        }).catch(e => console.error('Now playing error:', e));
    }

function fetchListeningTime() {
    fetch('/api/listening_time').then(r => r.json()).then(data => {
        document.getElementById('timeToday').innerText = data.today + 'h';
        document.getElementById('timeWeek').innerText = data.week + 'h';
        document.getElementById('timeMonth').innerText = data.month + 'h';
        // Convert year to days + hours if it exceeds 24 hours
        const yearHours = data.year;
        if (yearHours >= 24) {
            const days = Math.floor(yearHours / 24);
            const remainingHours = (yearHours % 24).toFixed(1);
            document.getElementById('timeYear').innerText = `${days}d ${remainingHours}h`;
        } else {
            document.getElementById('timeYear').innerText = yearHours + 'h';
        }
    }).catch(e => console.error('Listening time error:', e));
}

    function fetchStats() {
        fetch('/api/stats').then(r => r.json()).then(data => {
            document.getElementById('totalScrobbles').innerText = data.total_scrobbles;
            const artistsList = document.getElementById('topArtistsList');
            artistsList.innerHTML = data.top_artists.length ? data.top_artists.map(a => `
                <li>
                    <img src="${escapeHtml(a.art_url || 'https://via.placeholder.com/24?text=🎵')}" onerror="this.src='https://via.placeholder.com/24?text=🎵'">
                    <span>${escapeHtml(a.artist)}</span>
                    <span class="stat-count">${a.playcount}</span>
                </li>
            `).join('') : '<li>No scrobbles yet</li>';
            
            const albumsList = document.getElementById('topAlbumsList');
            albumsList.innerHTML = data.top_albums.length ? data.top_albums.map(a => `
                <li>
                    <img src="${escapeHtml(a.art_url || 'https://via.placeholder.com/24?text=🎵')}" onerror="this.src='https://via.placeholder.com/24?text=🎵'">
                    <span>${escapeHtml(a.artist)} – ${escapeHtml(a.album)}</span>
                    <span class="stat-count">${a.playcount}</span>
                </li>
            `).join('') : '<li>No albums yet</li>';
            
            const tracksList = document.getElementById('topTracksList');
            tracksList.innerHTML = data.top_tracks.length ? data.top_tracks.map(t => `
                <li>
                    <img src="${escapeHtml(t.art_url || 'https://via.placeholder.com/24?text=🎵')}" onerror="this.src='https://via.placeholder.com/24?text=🎵'">
                    <span>${escapeHtml(t.artist)} – ${escapeHtml(t.track)}</span>
                    <span class="stat-count">${t.playcount}</span>
                </li>
            `).join('') : '<li>No tracks yet</li>';
        }).catch(e => console.error('Stats error:', e));
    }

    function fetchListeningClock() {
        fetch('/api/listening_clock').then(r => r.json()).then(data => {
            const labels = [], counts = [];
            for (let i = 0; i < data.hour_counts.length; i++) {
                if (data.hour_counts[i] > 0) { labels.push(`${i}:00`); counts.push(data.hour_counts[i]); }
            }
            const backgroundColors = labels.map((label, idx) => parseInt(label.split(':')[0]) === data.busiest_hour ? '#ff6384' : '#36a2eb');
            new Chart(document.getElementById('clockPieChart').getContext('2d'), {
                type: 'pie', data: { labels: labels, datasets: [{ data: counts, backgroundColor: backgroundColors, borderWidth: 1 }] },
                options: { responsive: true, plugins: { legend: { display: false }, tooltip: { callbacks: { label: (t) => `${t.label}: ${t.raw} scrobbles` } } } }
            });
            document.getElementById('busiestHourInfo').innerHTML = `🚀 Busiest hour: <strong>${data.busiest_hour}:00</strong> with <strong>${data.busiest_hour_count}</strong> scrobbles`;
        }).catch(e => console.error('Listening clock error:', e));
    }

    function fetchWeekdayStats() {
        fetch('/api/scrobbles_by_weekday').then(r => r.json()).then(data => {
            const dayOrder = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
            const orderedData = dayOrder.map(day => { const found = data.find(d => d.day === day); return found ? found.count : 0; });
            new Chart(document.getElementById('weekdayChart').getContext('2d'), {
                type: 'bar', 
                data: { 
                    labels: dayOrder, 
                    datasets: [{ 
                        label: 'Scrobbles', 
                        data: orderedData, 
                        backgroundColor: '#36a2eb', 
                        borderRadius: 4 
                    }] 
                },
                options: { 
                    responsive: true, 
                    scales: { y: { beginAtZero: true } },
                    plugins: { legend: { display: false } }
                }
            });
        }).catch(e => console.error('Weekday stats error:', e));
    }

    function fetchTopArtistsByTime() {
        fetch('/api/top_artists_by_time').then(r => r.json()).then(data => {
            const container = document.getElementById('topArtistsTimeList');
            container.innerHTML = data.length ? data.map(a => `<li><span>${escapeHtml(a.artist)}</span><span class="stat-count">${a.hours}h</span></li>`).join('') : '<li>No data yet</li>';
        }).catch(e => console.error('Top artists by time error:', e));
    }
    
    function fetchTopDays() {
        fetch('/api/longest_listening_day')
            .then(r => r.json())
            .then(data => {
                const container = document.getElementById('topDaysList');
                if (data.length === 0) {
                    container.innerHTML = '<li>No scrobbles yet</li>';
                } else {
                    container.innerHTML = data.map(d => `
                        <li>
                            <span>📅 ${d.date}</span>
                            <span class="stat-count">${d.scrobbles} scrobbles</span>
                        </li>
                    `).join('');
                }
            })
            .catch(e => console.error('Top days error:', e));
    }
    
    function fetchTopPlaylists() {
        fetch('/api/top_playlists').then(r => r.json()).then(data => {
            const container = document.getElementById('topPlaylistsList');
            container.innerHTML = data.length ? data.map(p => `<li><span>${escapeHtml(p.playlist)}</span><span class="stat-count">${p.count}</span></li>`).join('') : '<li>No playlist data yet</li>';
        }).catch(e => console.error('Top playlists error:', e));
    }

    function fetchTopGenres() {
        fetch('/api/top_genres')
            .then(r => r.json())
            .then(data => {
                const container = document.getElementById('topGenresList');
                if (data.length === 0) {
                    container.innerHTML = '<li>No genre data yet. Assign genres on the Monthly Reports page.</li>';
                } else {
                    container.innerHTML = data.map(g => `<li><span>${escapeHtml(g.genre)}</span><span class="stat-count">${g.count}</span></li>`).join('');
                }
            })
            .catch(e => console.error('Top genres error:', e));
    }

function fetchFavourites() {
    fetch('/api/favourites?limit=10')
        .then(r => r.json())
        .then(data => {
            const list = document.getElementById('favouritesList');
            if (data.length === 0) {
                list.innerHTML = '<li>No favourites yet</li>';
                return;
            }
            list.innerHTML = data.map(f => `
                <li style="display: flex; align-items: center; gap: 8px;">
                    <img src="${escapeHtml(f.art_url || 'https://via.placeholder.com/24?text=🎵')}" onerror="this.src='https://via.placeholder.com/24?text=🎵'">
                    <span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${escapeHtml(f.artist)} – ${escapeHtml(f.track)}</span>
                    <button class="delete-fav-btn" data-artist="${escapeHtml(f.artist)}" data-track="${escapeHtml(f.track)}" style="background:none; border:none; color:var(--text-muted); cursor:pointer; font-size:0.8rem;" title="Remove from favourites">❌</button>
                </li>
            `).join('');
            attachFavDeleteHandlers();
        })
        .catch(e => console.error('Favourites error:', e));
}

function fetchFavouriteAlbums() {
    fetch('/api/favourite_albums?limit=10')
        .then(r => r.json())
        .then(data => {
            const list = document.getElementById('favouriteAlbumsList');
            if (data.length === 0) {
                list.innerHTML = '<li>No favourite albums yet</li>';
                return;
            }
            list.innerHTML = data.map(f => `
                <li style="display: flex; align-items: center; gap: 8px;">
                    <img src="${escapeHtml(f.art_url || 'https://via.placeholder.com/24?text=🎵')}" onerror="this.src='https://via.placeholder.com/24?text=🎵'">
                    <span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${escapeHtml(f.artist)} – ${escapeHtml(f.album)}</span>
                    <button class="delete-fav-album-btn" data-artist="${escapeHtml(f.artist)}" data-album="${escapeHtml(f.album)}" style="background:none; border:none; color:var(--text-muted); cursor:pointer; font-size:0.8rem;" title="Remove from favourites">❌</button>
                </li>
            `).join('');
            attachFavAlbumDeleteHandlers();
        })
        .catch(e => console.error('Favourite albums error:', e));
}

// Call it along with other fetch functions
fetchFavouriteAlbums();

    // Call it along with other fetch functions
    fetchFavourites();

    // New function: fetch genre distribution for pie chart
    function fetchGenreDistribution() {
        fetch('/api/top_genres')
            .then(r => r.json())
            .then(data => {
                const ctx = document.getElementById('genrePieChart').getContext('2d');
                if (genreChart) genreChart.destroy();
                if (data.length === 0) {
                    // Hide chart and show message
                    ctx.canvas.parentElement.innerHTML = '<div class="empty-message">No genre data yet</div>';
                    return;
                }
                // Prepare data: top 9, rest as "Other"
                let topData = data.slice(0, 9);
                let otherCount = data.slice(9).reduce((sum, g) => sum + g.count, 0);
                let labels = topData.map(g => g.genre);
                let counts = topData.map(g => g.count);
                if (otherCount > 0) {
                    labels.push('Other');
                    counts.push(otherCount);
                }
                genreChart = new Chart(ctx, {
                    type: 'pie',
                    data: {
                        labels: labels,
                        datasets: [{
                            data: counts,
                            backgroundColor: [
                                '#ff6384', '#36a2eb', '#ffce56', '#4bc0c0', '#9966ff',
                                '#ff9f40', '#c9cbcf', '#7c4dff', '#b0bec5', '#ff7043'
                            ],
                            borderWidth: 1
                        }]
                    },
                    options: {
                        responsive: true,
                        plugins: {
                            legend: { position: 'right' },
                            tooltip: { callbacks: { label: (ctx) => `${ctx.label}: ${ctx.raw} scrobbles` } }
                        }
                    }
                });
            })
            .catch(e => console.error('Genre distribution error:', e));
    }

    let currentOffset = 0; const limit = 25;
    function loadScrobbles(offset) {
        const container = document.getElementById('scrobbleList');
        container.innerHTML = '<div class="empty-message">Loading...</div>';
        fetch(`/api/scrobbles?limit=${limit}&offset=${offset}`).then(r => r.json()).then(data => {
            if (!data.scrobbles.length) { container.innerHTML = '<div class="empty-message">✨ No scrobbles yet. Start listening!</div>'; return; }
            renderScrobbles(data.scrobbles);
            renderPagination(data.total, offset);
        }).catch(e => { console.error(e); container.innerHTML = '<div class="empty-message">Error loading scrobbles.</div>'; });
    }

    function renderScrobbles(scrobbles) {
        const container = document.getElementById('scrobbleList');
        let html = '';
        for (let s of scrobbles) {
            const artUrl = s.art_url || 'https://via.placeholder.com/56?text=🎵';
            const dateStr = formatRelativeTime(s.timestamp);
            html += `
                <div class="scrobble-item" data-id="${s.id}">
                    <img class="album-art" src="${escapeHtml(artUrl)}" onerror="this.src='https://via.placeholder.com/56?text=🎵'">
                    <div class="track-info">
                        <div class="track-name">${escapeHtml(s.track)}</div>
                        <div class="artist-name">${escapeHtml(s.artist)}</div>
                        <div class="album-name">${escapeHtml(s.album || '')}</div>
                    </div>
                    <div class="scrobble-date">${dateStr}</div>
                    <button class="delete-scrobble" data-id="${s.id}" title="Delete scrobble">🗑️</button>
                </div>
            `;
        }
        container.innerHTML = html;
        attachDeleteHandlers();
    }

    function attachDeleteHandlers() {
        document.querySelectorAll('.delete-scrobble').forEach(btn => {
            btn.removeEventListener('click', btn._listener);
            const listener = (e) => {
                e.stopPropagation();
                const row = btn.closest('.scrobble-item');
                deleteScrobble(btn.dataset.id, row);
            };
            btn.addEventListener('click', listener);
            btn._listener = listener;
        });
    }

function attachFavAlbumDeleteHandlers() {
    document.querySelectorAll('.delete-fav-album-btn').forEach(btn => {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            const artist = this.dataset.artist;
            const album = this.dataset.album;
            if (!confirm(`Remove "${artist} – ${album}" from favourite albums?`)) return;
            fetch('/api/favourite_album/toggle', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ artist, album })
            })
            .then(r => r.json())
            .then(data => {
                if (data.status === 'removed') {
                    fetchFavouriteAlbums();   // refresh the list
                }
            })
            .catch(e => console.error('Error removing favourite album:', e));
        });
    });
}

function attachFavDeleteHandlers() {
    document.querySelectorAll('.delete-fav-btn').forEach(btn => {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            const artist = this.dataset.artist;
            const track = this.dataset.track;
            if (!confirm(`Remove "${artist} – ${track}" from favourites?`)) return;
            fetch('/api/favourite/toggle', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ artist, track })
            })
            .then(r => r.json())
            .then(data => {
                if (data.status === 'removed') {
                    fetchFavourites();   // refresh the list
                }
            })
            .catch(e => console.error('Error removing favourite:', e));
        });
    });
}

    async function deleteScrobble(scrobbleId, rowElement) {
        if (!confirm("Are you sure you want to delete this scrobble? This cannot be undone.")) return;
        try {
            const response = await fetch(`/api/scrobble/${scrobbleId}`, { method: 'DELETE' });
            const data = await response.json();
            if (response.ok) {
                rowElement.remove();
                fetchStats();
                fetchListeningTime();
                fetchListeningClock();
                fetchWeekdayStats();
                fetchTopArtistsByTime();
                fetchTopDays();
                fetchTopPlaylists();
                fetchTopGenres();
                fetchGenreDistribution();  // refresh pie chart
            } else {
                alert(`Error: ${data.error}`);
            }
        } catch (err) {
            console.error("Delete failed:", err);
            alert("Failed to delete scrobble");
        }
    }

    function formatRelativeTime(timestamp) {
        const seconds = Math.floor((Date.now() / 1000) - timestamp);
        if (seconds < 60) return 'just now';
        const minutes = Math.floor(seconds / 60);
        if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'} ago`;
        const hours = Math.floor(minutes / 60);
        if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`;
        const days = Math.floor(hours / 24);
        if (days < 7) return `${days} day${days === 1 ? '' : 's'} ago`;
        return new Date(timestamp * 1000).toLocaleDateString();
    }

    function escapeHtml(str) { if (!str) return ''; return str.replace(/[&<>]/g, m => m === '&' ? '&amp;' : m === '<' ? '&lt;' : '&gt;'); }
    function renderPagination(total, offset) {
        const pagDiv = document.getElementById('pagination');
        if (total <= limit) { pagDiv.innerHTML = ''; return; }
        const currentPage = Math.floor(offset / limit) + 1, totalPages = Math.ceil(total / limit);
        pagDiv.innerHTML = `<button onclick="changePage(-1)" ${offset === 0 ? 'disabled' : ''}>◀ Previous</button><span>Page ${currentPage} of ${totalPages}</span><button onclick="changePage(1)" ${offset + limit >= total ? 'disabled' : ''}>Next ▶</button>`;
    }
    function changePage(delta) { let newOffset = currentOffset + delta * limit; if (newOffset < 0) newOffset = 0; currentOffset = newOffset; loadScrobbles(currentOffset); }
    function exportData() { window.location.href = '/export'; }
    function importData(file) { if (!file) return; const formData = new FormData(); formData.append('file', file); fetch('/import', { method: 'POST', body: formData }).then(r => r.json()).then(data => { alert(data.status || data.error); loadScrobbles(currentOffset); fetchStats(); fetchListeningTime(); fetchListeningClock(); fetchWeekdayStats(); fetchTopArtistsByTime(); fetchTopDays(); fetchTopPlaylists(); fetchTopGenres(); fetchGenreDistribution(); }).catch(e => alert('Import failed: ' + e)); }

    fetchNowPlaying();
    fetchStats();
    fetchListeningTime();
    fetchListeningClock();
    fetchWeekdayStats();
    fetchTopArtistsByTime();
    fetchTopDays();
    fetchTopPlaylists();
    fetchTopGenres();
    fetchGenreDistribution();  // initial load
    loadScrobbles(0);
    setInterval(fetchNowPlaying, 5000);

// RateYourMusic button for Scrobble Overview page
(function() {
    function addButtons() {
        const container = document.querySelector('.now-playing');
        if (!container) return;

        // --- RYM Button (top‑right) ---
        if (!document.getElementById('rymButtonOverview')) {
            const rymBtn = document.createElement('button');
            rymBtn.id = 'rymButtonOverview';
            rymBtn.innerHTML = '🎵 RYM';
            rymBtn.title = 'Search on RateYourMusic';
            Object.assign(rymBtn.style, {
                position: 'absolute',
                top: '10px',
                right: '10px',
                background: 'rgba(0,0,0,0.5)',
                backdropFilter: 'blur(4px)',
                border: 'none',
                borderRadius: '20px',
                padding: '4px 10px',
                fontSize: '0.7rem',
                cursor: 'pointer',
                color: 'white',
                fontFamily: 'inherit',
                zIndex: '100',
                transition: 'background 0.2s'
            });
            rymBtn.addEventListener('mouseenter', () => rymBtn.style.background = 'rgba(0,0,0,0.7)');
            rymBtn.addEventListener('mouseleave', () => rymBtn.style.background = 'rgba(0,0,0,0.5)');
            if (getComputedStyle(container).position !== 'relative') container.style.position = 'relative';
            container.appendChild(rymBtn);
            rymBtn.addEventListener('click', () => {
                const artist = document.getElementById('nowArtist').innerText;
                const album = document.getElementById('nowAlbum').innerText;
                if (!artist || artist === '-') { alert('No artist playing'); return; }
                const url = `https://rateyourmusic.com/search?searchtype=release&searchterm=${encodeURIComponent(artist)}%20-%20${encodeURIComponent(album)}`;
                window.open(url, '_blank');
            });
        }
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', addButtons);
    else addButtons();
    setTimeout(addButtons, 1000);
})();

// Make scrollable lists require a click before scrolling
document.querySelectorAll('.stat-list, .scrobble-list').forEach(list => {
    list.addEventListener('click', function(e) {
        // Toggle scrollability
        if (list.style.overflowY === 'auto' || list.style.overflowY === 'scroll') {
            list.style.overflowY = 'hidden';   // disable scrolling again
        } else {
            list.style.overflowY = 'auto';     // enable scrolling
        }
        // Optionally, stop propagation so the click doesn't trigger on child items
        e.stopPropagation();
    });
});

// Optional: clicking outside the list re‑locks scrolling
document.addEventListener('click', function(e) {
    document.querySelectorAll('.stat-list, .scrobble-list').forEach(list => {
        if (!list.contains(e.target)) {
            list.style.overflowY = 'hidden';
        }
    });
});
    
</script>
</body>
</html>
"""

MONTHLY_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Reports and Tools · TIDAL</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Cdefs%3E%3CradialGradient id='grad' cx='50%25' cy='50%25' r='50%25'%3E%3Cstop offset='0%25' stop-color='%23e0e0e0'/%3E%3Cstop offset='70%25' stop-color='%23a0a0a0'/%3E%3Cstop offset='100%25' stop-color='%23404040'/%3E%3C/radialGradient%3E%3C/defs%3E%3Ccircle cx='50' cy='50' r='48' fill='url(%23grad)' stroke='%23333' stroke-width='2'/%3E%3Ccircle cx='50' cy='50' r='12' fill='%23333'/%3E%3C/svg%3E">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
        :root {
            --bg-body: #e8e8e8;
            --bg-card: #ffffff;
            --bg-tools: #ffffff;
            --text-primary: #222;
            --text-secondary: #666;
            --text-muted: #999;
            --border-light: #e5e5e5;
            --border-card: #eee;
            --accent: #d51007;
            --accent-hover: #b00;
            --button-bg: #f5f5f5;
            --button-border: #ddd;
            --button-hover: #e9e9e9;
            --shadow: rgba(0,0,0,0.05);
        }
        body.dark {
            --bg-body: #121212;
            --bg-card: #1e1e1e;
            --bg-tools: #1e1e1e;
            --text-primary: #eee;
            --text-secondary: #bbb;
            --text-muted: #888;
            --border-light: #2c2c2c;
            --border-card: #2c2c2c;
            --accent: #ff6b6b;
            --accent-hover: #ff8a8a;
            --button-bg: #2c2c2c;
            --button-border: #444;
            --button-hover: #3a3a3a;
            --shadow: rgba(0,0,0,0.3);
        }
        * { box-sizing: border-box; }
        body {
            font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
            background: var(--bg-body);
            margin: 0;
            padding: 0;
            color: var(--text-primary);
            transition: background 0.2s, color 0.2s;
        }
        .container { max-width: 1000px; margin: 0 auto; padding: 2rem 1rem; }
        .header {
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            flex-wrap: wrap;
            margin-bottom: 2rem;
            border-bottom: 1px solid var(--border-light);
            padding-bottom: 1rem;
        }
        h1 { font-size: 1.8rem; font-weight: 500; margin: 0; color: var(--accent); letter-spacing: -0.5px; }
        .sub { color: var(--text-muted); font-size: 0.85rem; }
        .back-link { background: var(--button-bg); border: 1px solid var(--button-border); padding: 0.4rem 1rem; border-radius: 20px; text-decoration: none; color: var(--accent); font-size: 0.85rem; font-weight: 500; transition: all 0.2s; }
        .back-link:hover { background: var(--accent); color: white; border-color: var(--accent); }
        .controls { margin-bottom: 1.5rem; }
        .month-picker-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 1.5rem; }
        select, button {
            padding: 8px 12px;
            border-radius: 8px;
            border: 1px solid var(--border-card);
            background: var(--button-bg);
            color: var(--text-primary);
            font-size: 0.9rem;
            cursor: pointer;
        }
        button { background: var(--accent); border: none; color: white; font-weight: bold; }
        .stats-trend-row {
            display: flex;
            flex-wrap: wrap;
            gap: 1.5rem;
            margin-bottom: 2rem;
            align-items: stretch;
        }
        .stat-badge {
            flex: 1;
            min-width: 150px;
            text-align: center;
            background: var(--bg-card);
            padding: 1rem;
            border-radius: 16px;
            box-shadow: 0 1px 4px var(--shadow);
            border: 1px solid var(--border-card);
            display: flex;
            flex-direction: column;
            justify-content: center;
        }
        .stat-badge .label { font-size: 0.8rem; color: var(--text-muted); }
        .stat-badge .value { font-size: 2rem; font-weight: 600; color: var(--accent); }
        .trend-card {
            flex: 2;
            background: var(--bg-card);
            border-radius: 16px;
            padding: 0.75rem 1rem;
            box-shadow: 0 1px 4px var(--shadow);
            border: 1px solid var(--border-card);
        }
        .trend-card h4 { margin: 0 0 0.5rem 0; font-size: 0.9rem; color: var(--accent); }
        .trend-card canvas { width: 100%; max-height: 150px; margin-top: 0; }
        .table-card { background: var(--bg-card); border-radius: 16px; padding: 1rem; border: 1px solid var(--border-card); margin-top: 2rem; }
        .table-card h3 { margin: 0 0 1rem 0; font-size: 1.2rem; color: var(--accent); border-left: 3px solid var(--accent); padding-left: 0.75rem; }
        .stat-list { list-style: none; padding: 0 5px 0 0; margin: 0; max-height: 300px; overflow-y: auto; }
        .stat-list li { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid var(--border-card); }
        .stat-list li span:first-child { flex: 1; white-space: nowrap; overflow: hidden !important; ; text-overflow: ellipsis; padding-right: 1rem; }
        .stat-count { font-weight: 600; color: var(--accent); margin-left: auto; flex-shrink: 0; }
        .stat-list::-webkit-scrollbar { width: 6px; }
        .stat-list::-webkit-scrollbar-track { background: var(--border-card); border-radius: 3px; }
        .stat-list::-webkit-scrollbar-thumb { background: #aaa; border-radius: 3px; }
        body.dark .stat-list::-webkit-scrollbar-thumb { background: #aaa; }
        .playlist-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 8px 0;
            border-bottom: 1px solid var(--border-card);
        }
        .playlist-name { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding-right: 1rem; }
        .edit-playlist-btn, .save-genre-btn, .save-artist-genre-btn, .save-album-genre-btn {
            background: var(--button-bg);
            border: 1px solid var(--button-border);
            border-radius: 6px;
            padding: 4px 10px;
            cursor: pointer;
            font-size: 0.75rem;
            transition: all 0.2s;
            color: var(--text-primary);
        }
        .edit-playlist-btn:hover, .save-genre-btn:hover, .save-artist-genre-btn:hover, .save-album-genre-btn:hover { background: var(--button-hover); }
        .save-genre-btn, .save-artist-genre-btn, .save-album-genre-btn { background: var(--accent); color: white; }
        .genre-input, .artist-genre-input, .album-genre-input {
            background: var(--button-bg);
            border: 1px solid var(--button-border);
            border-radius: 6px;
            padding: 4px 8px;
            margin: 0 10px;
            width: 150px;
            color: var(--text-primary);
        }
        .backfill-btn {
            background: #6c757d;
            border: none;
            border-radius: 6px;
            padding: 8px 16px;
            cursor: pointer;
            color: white;
            font-size: 0.8rem;
            margin-top: 1rem;
        }
        canvas { max-height: 300px; margin-top: 1rem; }
        footer { margin-top: 3rem; text-align: center; font-size: 0.7rem; color: var(--text-muted); }
        @media (max-width: 700px) { .stats-trend-row { flex-direction: column; } }
        .scrobble-item { display: flex; align-items: center; gap: 1rem; padding: 1rem; border-bottom: 1px solid var(--border-card); transition: background 0.15s; }
        .scrobble-item:hover { background: #f0f0f0 !important; }
        body.dark .scrobble-item:hover { background: #2a2a2a !important; }
        .album-art { flex-shrink: 0; width: 56px; height: 56px; border-radius: 8px; object-fit: cover; background: #f0f0f0; }
        body.dark .album-art { background: #2c2c2c; }
        .track-info { flex: 1; min-width: 0; }
        .track-name { font-weight: 600; font-size: 1rem; }
        .artist-name { font-size: 0.85rem; color: var(--text-secondary); margin-top: 2px; }
        .album-name { font-size: 0.75rem; color: var(--text-muted); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .scrobble-date { flex-shrink: 0; font-size: 0.75rem; color: var(--text-muted); text-align: right; min-width: 120px; }
        .delete-scrobble {
            background: none;
            border: none;
            font-size: 1.2rem;
            cursor: pointer;
            opacity: 0.6;
            transition: opacity 0.2s;
            color: var(--text-secondary);
            padding: 0 4px;
        }
        .delete-scrobble:hover { opacity: 1; color: var(--accent); }
        .empty-message { padding: 2rem; text-align: center; color: var(--text-muted); }
    </style>
</head>
<body>
<div class="container">
<div class="header">
    <div>
        <h1>📊 Reports and Tools</h1>
        <div class="sub">playlist rename, genre tagging & monthly reports</div>
    </div>
    <div style="display: flex; flex-direction: column; align-items: flex-end; gap: 8px;">
        <a href="/scrobbles" class="back-link">◀ Back to Overview</a>
    </div>
</div>

    <div class="month-picker-row">
        <select id="yearSelect"></select>
        <select id="monthSelect"></select>
        <button id="loadReportBtn">Generate Report</button>
        <a href="/backup/now" class="back-link" download style="margin-left: auto;">💾 Backup Database</a>
    </div>    

    <div id="reportContent" style="display: none;">
        <div class="stats-trend-row">
            <div class="stat-badge"><div class="label">Total Scrobbles</div><div class="value" id="totalScrobbles">-</div></div>
            <div class="stat-badge"><div class="label">Listening Time</div><div class="value" id="totalHours">-</div><div class="label">hours</div></div>
            <div class="trend-card">
                <h4>📈 Monthly Trend (last 12 months)</h4>
                <canvas id="trendChart" width="100%" height="120"></canvas>
            </div>
        </div>

        <div class="table-card" style="margin-top: 0;">
            <h3>🕒 Listening Clock (Hourly Distribution)</h3>
            <canvas id="clockChart" width="100%" height="250"></canvas>
        </div>
    </div>

    <div id="loadingMsg" style="text-align: center; padding: 2rem;">Select a month and click Generate Report.</div>

    <!-- Card 1: Tag Artist by Genre (search + datalist) -->
    <div class="table-card">
        <h3>🏷️ Tag Artist by Genre</h3>
        <p class="sub" style="margin-bottom: 1rem;">Search for an artist, then assign a genre (artist genre overrides playlist/album genres). You can type a new genre or select from existing ones.</p>
        <div style="display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 1rem;">
            <input type="text" id="artistSearchInput" placeholder="Type artist name..." style="flex: 2; min-width: 200px; padding: 8px 12px; border-radius: 8px; border: 1px solid var(--border-card); background: var(--button-bg); color: var(--text-primary);">
            <button id="searchArtistBtn" style="background: var(--accent); border: none; padding: 8px 16px; border-radius: 8px; cursor: pointer; color: white;">Search</button>
            <button id="clearArtistSearchBtn" style="background: #6c757d; border: none; padding: 8px 16px; border-radius: 8px; cursor: pointer; color: white;">Clear</button>
        </div>
        <div id="artistSearchResults" style="max-height: 300px; overflow-y: auto;">
            <div class="empty-message">Search for an artist to assign a genre.</div>
        </div>
    </div>

    <!-- Card 2: Tag Artist via Genre Dropdown (artists without genre) -->
    <div class="table-card">
        <h3>🏷️ Tag Artist via Genre Dropdown</h3>
        <p class="sub" style="margin-bottom: 1rem;">Assign a genre to an artist using the dropdown of existing genres. Also shows artists with no genre for quick tagging.</p>
        <div style="display: flex; gap: 10px; flex-wrap: wrap; align-items: flex-end; margin-bottom: 1rem;">
            <div style="flex: 2; min-width: 200px;">
                <label>Artist name</label>
                <input type="text" id="artistNameInput" placeholder="Type artist name..." style="width:100%; padding: 8px 12px; border-radius: 8px; border: 1px solid var(--border-card); background: var(--button-bg); color: var(--text-primary);">
            </div>
            <div style="flex: 1; min-width: 150px;">
                <label>Genre</label>
                <select id="genreSelectDropdown" style="width:100%; padding: 8px 12px; border-radius: 8px; border: 1px solid var(--border-card); background: var(--button-bg); color: var(--text-primary);">
                    <option value="">-- Select a genre --</option>
                </select>
            </div>
            <div>
                <button id="saveArtistGenreDropdown" class="backfill-btn" style="background: var(--accent);">Save Genre</button>
                <button id="clearArtistDropdownBtn" class="backfill-btn" style="background: #6c757d; margin-left: 5px;">Clear</button>
            </div>
        </div>
        <div>
            <p class="sub">Artists with no genre (last 10 scrobbles):</p>
            <div id="noGenreArtistsList" style="max-height: 200px; overflow-y: auto;">
                <div class="empty-message">Loading...</div>
            </div>
        </div>
    </div>

    <!-- Card 3: Tag Album by Genre (search + datalist) -->
    <div class="table-card">
        <h3>🏷️ Tag Album by Genre</h3>
        <p class="sub" style="margin-bottom: 1rem;">Search for an album, then assign a genre (album genre overrides playlist genre, but is overridden by artist genre). You can type a new genre or select from existing ones.</p>
        <div style="display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 1rem;">
            <input type="text" id="albumSearchInput" placeholder="Type album name..." style="flex: 2; min-width: 200px; padding: 8px 12px; border-radius: 8px; border: 1px solid var(--border-card); background: var(--button-bg); color: var(--text-primary);">
            <button id="searchAlbumBtn" style="background: var(--accent); border: none; padding: 8px 16px; border-radius: 8px; cursor: pointer; color: white;">Search</button>
            <button id="clearAlbumSearchBtn" style="background: #6c757d; border: none; padding: 8px 16px; border-radius: 8px; cursor: pointer; color: white;">Clear</button>
        </div>
        <div id="albumSearchResults" style="max-height: 300px; overflow-y: auto;">
            <div class="empty-message">Search for an album to assign a genre.</div>
        </div>
    </div>

    <!-- Card 4: Browse by Genre -->
    <div class="table-card">
        <h3>🔍 Browse by Genre</h3>
        <p class="sub" style="margin-bottom: 1rem;">Select a genre to see all artists, albums, and tracks tagged with it.</p>
        <div style="display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 1rem;">
            <select id="genreSelect" style="flex: 2; min-width: 200px; padding: 8px 12px; border-radius: 8px; border: 1px solid var(--border-card); background: var(--button-bg); color: var(--text-primary);"><option value="">-- Select a genre --</option></select>
            <button id="loadGenreBtn" style="background: var(--accent); border: none; padding: 8px 16px; border-radius: 8px; cursor: pointer; color: white;">Browse</button>
        </div>
        <div id="genreResults" style="display: none;">
            <div class="stats-trend-row" style="margin-bottom: 1rem;">
                <div class="stat-badge"><div class="label">Total Scrobbles</div><div class="value" id="genreTotal">0</div></div>
            </div>
            <div class="tables-grid">
                <div class="table-card"><h4>🎤 Artists</h4><ul class="stat-list" id="genreArtistsList" style="max-height: 200px;"></ul></div>
                <div class="table-card"><h4>💿 Albums</h4><ul class="stat-list" id="genreAlbumsList" style="max-height: 200px;"></ul></div>
                <div class="table-card"><h4>🎵 Tracks</h4><ul class="stat-list" id="genreTracksList" style="max-height: 200px;"></ul></div>
            </div>
        </div>
        <div id="genreNoData" style="display: none; text-align: center; padding: 1rem;">Select a genre and click Browse.</div>
    </div>

    <!-- Card 5: Search Scrobbles -->
    <div class="table-card">
        <h3>🔍 Search Scrobbles</h3>
        <div style="display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end;">
            <div style="flex: 1; min-width: 150px;"><label>Track</label><input type="text" id="searchTrack" placeholder="Track name" style="width:100%;"></div>
            <div style="flex: 1; min-width: 150px;"><label>Artist</label><input type="text" id="searchArtist" placeholder="Artist name" style="width:100%;"></div>
            <div style="flex: 1; min-width: 150px;"><label>Album</label><input type="text" id="searchAlbum" placeholder="Album name" style="width:100%;"></div>
            <div style="flex: 1; min-width: 120px;"><label>Genre</label><select id="searchGenre" style="width:100%;"><option value="">All</option></select></div>
            <div style="flex: 1; min-width: 120px;"><label>Playlist</label><select id="searchPlaylist" style="width:100%;"><option value="">All</option></select></div>
            <div style="flex: 1; min-width: 130px;"><label>Start Date</label><input type="date" id="searchStartDate" style="width:100%;"></div>
            <div style="flex: 1; min-width: 130px;"><label>End Date</label><input type="date" id="searchEndDate" style="width:100%;"></div>
            <div><button id="searchScrobblesBtn" class="backfill-btn" style="background: var(--accent);">Search</button><button id="clearSearchBtn" class="backfill-btn" style="background: #6c757d; margin-left:5px;">Clear</button></div>
        </div>
        <div id="searchResults" style="margin-top: 1rem;"><div class="empty-message">Enter search criteria and click Search.</div></div>
    </div>

    <!-- Remaining tools (Rename Playlists, Genre Tagging by Playlist) -->
    <div class="table-card">
        <h3>📀 Rename Playlists</h3>
        <p class="sub" style="margin-bottom: 1rem;">Edit any playlist name – all past scrobbles will be updated.</p>
        <div id="playlistList" style="max-height: 300px; overflow-y: auto;"><div class="empty-message">Loading playlists...</div></div>
    </div>

    <div class="table-card">
        <h3>🏷️ Genre Tagging (by Playlist)</h3>
        <p class="sub" style="margin-bottom: 1rem;">Assign a genre to each playlist – all scrobbles from that playlist will be tagged.</p>
        <div id="genreMappingList" style="max-height: 400px; overflow-y: auto;"><div class="empty-message">Loading playlists...</div></div>
        <button id="backfillGenresBtn" class="backfill-btn">Backfill Genres for Past Scrobbles</button>
        <div style="display: flex; gap: 10px; margin-top: 1rem; flex-wrap: wrap;">
            <button id="backfillArtistGenresBtn" class="backfill-btn">Backfill Artist Genres</button>
            <button id="backfillAlbumGenresBtn" class="backfill-btn">Backfill Album Genres</button>
            <button id="backfillMBGenresBtn" class="backfill-btn" style="background: #6c757d;">🧠 Backfill MusicBrainz Genres</button>
        </div>
    </div>

    <footer>scrobbles stored in scrobbles.db · auto‑scrobbled after 50% or 4 minutes · synced with Last.fm</footer>
</div>

<script>
    // ========== DARK MODE – APPLY IMMEDIATELY ==========
    (function() {
        const theme = localStorage.getItem('scrobbleTheme');
        if (theme === 'dark') {
            document.body.classList.add('dark');
        }
    })();

    let clockChart = null;
    let trendChart = null;

    // Populate year and month dropdowns
    function populateDateSelects() {
        const yearSelect = document.getElementById('yearSelect');
        const monthSelect = document.getElementById('monthSelect');
        if (!yearSelect || !monthSelect) return;
        const currentYear = new Date().getFullYear();
        for (let y = 2020; y <= currentYear + 1; y++) {
            const option = document.createElement('option');
            option.value = y;
            option.textContent = y;
            if (y === currentYear) option.selected = true;
            yearSelect.appendChild(option);
        }
        const monthNames = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
        for (let m = 0; m < monthNames.length; m++) {
            const option = document.createElement('option');
            option.value = m + 1;
            option.textContent = monthNames[m];
            if (m === new Date().getMonth()) option.selected = true;
            monthSelect.appendChild(option);
        }
    }

    async function loadReport() {
        const year = document.getElementById('yearSelect').value;
        const month = document.getElementById('monthSelect').value;
        if (!year || !month) return;
        const contentDiv = document.getElementById('reportContent');
        const loadingMsg = document.getElementById('loadingMsg');
        loadingMsg.style.display = 'block';
        loadingMsg.innerText = 'Loading report...';
        contentDiv.style.display = 'none';
        try {
            const response = await fetch(`/api/monthly_report?year=${year}&month=${month}`);
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = await response.json();
            if (data.error) throw new Error(data.error);
            document.getElementById('totalScrobbles').innerText = data.total_scrobbles;
            document.getElementById('totalHours').innerText = data.total_hours;
            const ctx = document.getElementById('clockChart').getContext('2d');
            if (clockChart) clockChart.destroy();
            clockChart = new Chart(ctx, {
                type: 'bar',
                data: { labels: Array.from({length:24},(_,i)=>`${i}:00`), datasets: [{ label: 'Scrobbles', data: data.hour_counts, backgroundColor: '#36a2eb', borderRadius: 4 }] },
                options: { responsive: true, scales: { y: { beginAtZero: true } } }
            });
            loadingMsg.style.display = 'none';
            contentDiv.style.display = 'block';
        } catch(err) {
            console.error(err);
            loadingMsg.innerText = `Error: ${err.message}`;
        }
    }

    function loadTrendChart() {
        fetch('/api/monthly_trend').then(r=>r.json()).then(data=>{
            const ctx = document.getElementById('trendChart').getContext('2d');
            if(trendChart) trendChart.destroy();
            trendChart = new Chart(ctx, {
                type: 'bar', data: { labels: data.map(d=>d.month), datasets: [{ label: 'Scrobbles', data: data.map(d=>d.scrobbles), backgroundColor: '#36a2eb', borderRadius: 4 }] },
                options: { responsive: true, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true } } }
            });
        }).catch(e=>console.error(e));
    }

    function escapeHtml(str) { if (!str) return ''; return str.replace(/[&<>]/g, m => m==='&'?'&amp;':m==='<'?'&lt;':'&gt;'); }
    function formatRelativeTime(timestamp) {
        const seconds = Math.floor((Date.now()/1000)-timestamp);
        if(seconds<60) return 'just now';
        const minutes = Math.floor(seconds/60);
        if(minutes<60) return `${minutes} minute${minutes===1?'':'s'} ago`;
        const hours = Math.floor(minutes/60);
        if(hours<24) return `${hours} hour${hours===1?'':'s'} ago`;
        const days = Math.floor(hours/24);
        if(days<7) return `${days} day${days===1?'':'s'} ago`;
        return new Date(timestamp*1000).toLocaleDateString();
    }

    // ========== PLAYLIST RENAME & GENRE MAPPINGS ==========
    function loadPlaylists() {
        const container = document.getElementById('playlistList');
        container.innerHTML = '<div class="empty-message">Loading playlists...</div>';
        fetch('/api/playlists').then(r=>r.json()).then(data=>{
            if(data.length===0) { container.innerHTML = '<div class="empty-message">No playlists found.</div>'; return; }
            container.innerHTML = data.map(p => `<div class="playlist-item"><span class="playlist-name">${escapeHtml(p)}</span><button class="edit-playlist-btn" onclick="renamePlaylist('${escapeHtml(p).replace(/'/g, "\\'")}')">✏️ Rename</button></div>`).join('');
        }).catch(e=>{ container.innerHTML = '<div class="empty-message">Error loading playlists.</div>'; console.error(e); });
    }
    function renamePlaylist(oldName) {
        const newName = prompt('Enter new name for playlist:', oldName);
        if(!newName || newName===oldName) return;
        fetch('/api/rename_playlist', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ old_name: oldName, new_name: newName }) })
            .then(r=>r.json()).then(data=>{ if(data.error) alert('Error: '+data.error); else alert(`Renamed to "${newName}" (${data.updated} updates).`); loadPlaylists(); loadGenreMappings(); loadTrendChart(); }).catch(e=>alert('Request failed: '+e));
    }
    function loadGenreMappings() {
        const container = document.getElementById('genreMappingList');
        container.innerHTML = '<div class="empty-message">Loading...</div>';
        fetch('/api/playlist_genre_map').then(r=>r.json()).then(maps=>{
            fetch('/api/playlists').then(r=>r.json()).then(playlists=>{
                if(playlists.length===0) { container.innerHTML = '<div class="empty-message">No playlists found.</div>'; return; }
                container.innerHTML = playlists.map(p => {
                    const existing = maps.find(m=>m.playlist===p);
                    const genre = existing ? existing.genre : '';
                    return `<div class="playlist-item"><span class="playlist-name">${escapeHtml(p)}</span><input type="text" class="genre-input" value="${escapeHtml(genre)}" placeholder="Genre"><button class="save-genre-btn" data-playlist="${escapeHtml(p)}">Save</button></div>`;
                }).join('');
                document.querySelectorAll('.save-genre-btn').forEach(btn => btn.addEventListener('click', (e)=>{
                    const playlist = btn.getAttribute('data-playlist');
                    const input = btn.parentElement.querySelector('.genre-input');
                    saveGenreMapping(playlist, input.value.trim());
                }));
            });
        }).catch(e=>{ container.innerHTML = '<div class="empty-message">Error loading genre mappings.</div>'; console.error(e); });
    }
    function saveGenreMapping(playlist, genre) {
        fetch('/api/set_playlist_genre', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ playlist, genre }) })
            .then(r=>r.json()).then(data=>{ if(data.error) alert('Error: '+data.error); else alert(`Genre saved.`); loadGenreMappings(); }).catch(e=>alert('Request failed: '+e));
    }
    function backfillGenres() { if(!confirm("Update all past scrobbles with playlist genre mappings?")) return; fetch('/api/backfill_genres',{method:'POST'}).then(r=>r.json()).then(data=>alert(`Backfill complete. ${data.updated} scrobbles updated.`)).catch(e=>alert('Error: '+e)); }
    function backfillArtistGenres() { if(!confirm("Update all past scrobbles with artist genre mappings?")) return; fetch('/api/backfill_artist_genres',{method:'POST'}).then(r=>r.json()).then(data=>alert(`Artist backfill complete. ${data.updated} scrobbles updated.`)).catch(e=>alert('Error: '+e)); }
    function backfillAlbumGenres() { if(!confirm("Update all past scrobbles with album genre mappings?")) return; fetch('/api/backfill_album_genres',{method:'POST'}).then(r=>r.json()).then(data=>alert(`Album backfill complete. ${data.updated} scrobbles updated.`)).catch(e=>alert('Error: '+e)); }

    // ========== ARTIST GENRE TAGGING (search + datalist) ==========
    const artistDatalist = document.createElement('datalist');
    artistDatalist.id = 'artistGenreDatalist';
    document.body.appendChild(artistDatalist);
    const albumDatalist = document.createElement('datalist');
    albumDatalist.id = 'albumGenreDatalist';
    document.body.appendChild(albumDatalist);

    function refreshGenreDatalists() {
        fetch('/api/genre_list')
            .then(r => r.json())
            .then(genres => {
                const artistDL = document.getElementById('artistGenreDatalist');
                const albumDL = document.getElementById('albumGenreDatalist');
                if (artistDL) artistDL.innerHTML = genres.map(g => `<option value="${escapeHtml(g)}">`).join('');
                if (albumDL) albumDL.innerHTML = genres.map(g => `<option value="${escapeHtml(g)}">`).join('');
            })
            .catch(e => console.error('Error refreshing datalists:', e));
    }
    refreshGenreDatalists();

    function clearArtistSearch() {
        document.getElementById('artistSearchInput').value = '';
        document.getElementById('artistSearchResults').innerHTML = '<div class="empty-message">Search for an artist to assign a genre.</div>';
    }

    function searchArtists() {
        const query = document.getElementById('artistSearchInput').value.trim();
        const resultsDiv = document.getElementById('artistSearchResults');
        if (!query) {
            resultsDiv.innerHTML = '<div class="empty-message">Enter an artist name to search.</div>';
            return;
        }
        resultsDiv.innerHTML = '<div class="empty-message">Searching...</div>';
        fetch(`/api/search_artists?q=${encodeURIComponent(query)}`)
            .then(response => response.json())
            .then(artistsData => {
                if (!artistsData.length) {
                    resultsDiv.innerHTML = '<div class="empty-message">No artists found.</div>';
                    return;
                }
                fetch('/api/artist_genre_map')
                    .then(r => r.json())
                    .then(mappings => {
                        let html = '';
                        artistsData.forEach(item => {
                            const artist = item.artist;
                            const suggested = item.suggestedGenre || '';
                            const existing = mappings.find(m => m.artist.toLowerCase() === artist.toLowerCase());
                            const genre = existing ? existing.genre : suggested;
                            html += `
                                <div class="playlist-item" data-artist="${escapeHtml(artist)}">
                                    <span class="playlist-name" style="flex:2;">${escapeHtml(artist)}</span>
                                    <input list="artistGenreDatalist" class="artist-genre-input" value="${escapeHtml(genre)}" placeholder="Type or select genre" style="width: 150px; margin: 0 10px; padding: 4px; border-radius: 6px; border: 1px solid var(--border-card); background: var(--button-bg); color: var(--text-primary);">
                                    <button class="save-artist-genre-btn" data-artist="${escapeHtml(artist)}">Save</button>
                                </div>
                            `;
                        });
                        resultsDiv.innerHTML = html;
                        document.querySelectorAll('.save-artist-genre-btn').forEach(btn => {
                            btn.removeEventListener('click', btn._listener);
                            const listener = () => {
                                const artist = btn.getAttribute('data-artist');
                                const input = btn.parentElement.querySelector('.artist-genre-input');
                                const genreVal = input.value.trim();
                                saveArtistGenre(artist, genreVal);
                            };
                            btn.addEventListener('click', listener);
                            btn._listener = listener;
                        });
                    })
                    .catch(err => {
                        console.error("Error fetching artist genre map:", err);
                        resultsDiv.innerHTML = `<div class="empty-message">Error loading genre map: ${err.message}</div>`;
                    });
            })
            .catch(err => {
                console.error("Artist search error:", err);
                resultsDiv.innerHTML = `<div class="empty-message">Search error: ${err.message}</div>`;
            });
    }

    function saveArtistGenre(artist, genre) {
        fetch('/api/set_artist_genre', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ artist, genre })
        })
        .then(r => r.json())
        .then(data => {
            if (data.error) alert('Error: ' + data.error);
            else alert(`Genre for "${artist}" saved.`);
            searchArtists(); // refresh
            refreshGenreDatalists();
        })
        .catch(err => alert('Request failed: ' + err.message));
    }

function loadNoGenreArtists() {
    const container = document.getElementById('noGenreArtistsList');
    container.innerHTML = '<div class="empty-message">Loading...</div>';
    fetch('/api/artists_without_genre')
        .then(r => r.json())
        .then(artists => {
            if (artists.length === 0) {
                container.innerHTML = '<div class="empty-message">No artists without genre.</div>';
                return;
            }
            let html = '<ul class="stat-list" style="max-height: 200px;">';
            artists.forEach(artist => {
                html += `<li class="artist-row" data-artist="${escapeHtml(artist)}" style="cursor:pointer; padding: 4px 0; display: flex; justify-content: space-between; align-items: center;">
                           <span>${escapeHtml(artist)}</span>
                           <button class="ignore-artist-btn" data-artist="${escapeHtml(artist)}" style="background:none; border:none; color:var(--text-muted); cursor:pointer; font-size:0.8rem;" title="Ignore this artist">🚫</button>
                         </li>`;
            });
            html += '</ul>';
            container.innerHTML = html;

            // Attach click handlers
            document.querySelectorAll('.artist-row').forEach(row => {
                row.addEventListener('click', function(e) {
                    const artist = this.dataset.artist;
                    document.getElementById('artistNameInput').value = artist;
                });
            });

            document.querySelectorAll('.ignore-artist-btn').forEach(btn => {
                btn.addEventListener('click', function(e) {
                    e.stopPropagation(); // prevent filling the input
                    const artist = this.dataset.artist;
                    ignoreArtistNoGenre(artist);
                });
            });
        })
        .catch(e => {
            console.error(e);
            container.innerHTML = '<div class="empty-message">Error loading artists.</div>';
        });
}

    function ignoreArtistNoGenre(artist) {
        fetch('/api/ignore_artist_no_genre', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ artist: artist })
        })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'ok') {
                // Reload the list – the ignored artist will be gone
                loadNoGenreArtists();
            } else {
                alert('Error: ' + (data.error || 'unknown'));
            }
        })
        .catch(e => alert('Request failed: ' + e));
    }

    function populateGenreDropdown() {
        fetch('/api/genre_list')
            .then(r => r.json())
            .then(genres => {
                const select = document.getElementById('genreSelectDropdown');
                select.innerHTML = '<option value="">-- Select a genre --</option>';
                genres.forEach(genre => {
                    const option = document.createElement('option');
                    option.value = genre;
                    option.textContent = genre;
                    select.appendChild(option);
                });
            })
            .catch(e => console.error('Error loading genres:', e));
    }

    function saveArtistFromDropdown() {
        const artist = document.getElementById('artistNameInput').value.trim();
        const genre = document.getElementById('genreSelectDropdown').value;
        if (!artist) { alert("Please enter an artist name."); return; }
        if (!genre) { alert("Please select a genre."); return; }
        fetch('/api/set_artist_genre', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ artist, genre })
        })
        .then(r => r.json())
        .then(data => {
            if (data.error) alert('Error: ' + data.error);
            else {
                alert(`Genre "${genre}" saved for artist "${artist}".`);
                // Remove the artist from the ignore list (if present)
                fetch('/api/unignore_artist_no_genre', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ artist: artist })
                }).catch(() => {});
                // Refresh the “no genre” list and reset the form
                loadNoGenreArtists();
                document.getElementById('artistNameInput').value = '';
                document.getElementById('genreSelectDropdown').value = '';
                refreshGenreDatalists();
            }
        })
        .catch(err => alert('Request failed: ' + err.message));
    }

    function clearArtistDropdown() {
        document.getElementById('artistNameInput').value = '';
        document.getElementById('genreSelectDropdown').value = '';
    }

    // ========== ALBUM GENRE TAGGING (search + datalist) ==========
    function clearAlbumSearch() {
        document.getElementById('albumSearchInput').value = '';
        document.getElementById('albumSearchResults').innerHTML = '<div class="empty-message">Search for an album to assign a genre.</div>';
    }

    function searchAlbums() {
        const query = document.getElementById('albumSearchInput').value.trim();
        const resultsDiv = document.getElementById('albumSearchResults');
        if (!query) {
            resultsDiv.innerHTML = '<div class="empty-message">Enter an album name to search.</div>';
            return;
        }
        resultsDiv.innerHTML = '<div class="empty-message">Searching...</div>';
        fetch(`/api/search_albums?q=${encodeURIComponent(query)}`)
            .then(response => {
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                return response.json();
            })
            .then(albums => {
                if (!albums.length) {
                    resultsDiv.innerHTML = '<div class="empty-message">No albums found.</div>';
                    return;
                }
                Promise.all([
                    fetch('/api/album_genre_map').then(r => r.json()),
                    fetch('/api/genre_list').then(r => r.json())
                ])
                .then(([mappings, genres]) => {
                    let html = '';
                    albums.forEach(album => {
                        const existing = mappings.find(m => m.artist === album.artist && m.album === album.album);
                        const genre = existing ? existing.genre : '';
                        html += `
                            <div class="playlist-item" data-artist="${escapeHtml(album.artist)}" data-album="${escapeHtml(album.album)}">
                                <span class="playlist-name" style="flex:2;">${escapeHtml(album.artist)} – ${escapeHtml(album.album)}</span>
                                <input list="albumGenreDatalist" class="album-genre-input" value="${escapeHtml(genre)}" placeholder="Type or select genre" style="width: 150px; margin: 0 10px; padding: 4px; border-radius: 6px; border: 1px solid var(--border-card); background: var(--button-bg); color: var(--text-primary);">
                                <button class="save-album-genre-btn" data-artist="${escapeHtml(album.artist)}" data-album="${escapeHtml(album.album)}">Save</button>
                            </div>
                        `;
                    });
                    resultsDiv.innerHTML = html;
                    document.querySelectorAll('.save-album-genre-btn').forEach(btn => {
                        btn.removeEventListener('click', btn._listener);
                        const listener = () => {
                            const artist = btn.getAttribute('data-artist');
                            const albumName = btn.getAttribute('data-album');
                            const input = btn.parentElement.querySelector('.album-genre-input');
                            const genreVal = input.value.trim();
                            saveAlbumGenre(artist, albumName, genreVal);
                        };
                        btn.addEventListener('click', listener);
                        btn._listener = listener;
                    });
                })
                .catch(err => {
                    console.error("Error loading data:", err);
                    resultsDiv.innerHTML = `<div class="empty-message">Error loading genre data: ${err.message}</div>`;
                });
            })
            .catch(err => {
                console.error("Album search error:", err);
                resultsDiv.innerHTML = `<div class="empty-message">Search error: ${err.message}</div>`;
            });
    }

    function saveAlbumGenre(artist, album, genre) {
        fetch('/api/set_album_genre', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ artist, album, genre })
        })
        .then(r => r.json())
        .then(data => {
            if (data.error) alert('Error: ' + data.error);
            else alert(`Genre for "${album}" saved.`);
            searchAlbums(); // refresh
            refreshGenreDatalists();
        })
        .catch(e => alert('Request failed: ' + e.message));
    }

    // ========== BROWSE BY GENRE ==========
    function loadGenreList() {
        fetch('/api/genre_list').then(r=>r.json()).then(genres=>{
            const select = document.getElementById('genreSelect');
            select.innerHTML = '<option value="">-- Select a genre --</option>';
            genres.forEach(g => { const opt = document.createElement('option'); opt.value = g; opt.textContent = g; select.appendChild(opt); });
        }).catch(e=>console.error(e));
    }
    function browseGenre() {
        const genre = document.getElementById('genreSelect').value;
        const resultsDiv = document.getElementById('genreResults');
        const noDataDiv = document.getElementById('genreNoData');
        if(!genre) { resultsDiv.style.display = 'none'; noDataDiv.style.display = 'block'; noDataDiv.innerHTML = 'Select a genre and click Browse.'; return; }
        resultsDiv.style.display = 'none'; noDataDiv.style.display = 'block'; noDataDiv.innerHTML = 'Loading...';
        fetch(`/api/genre_items?genre=${encodeURIComponent(genre)}`).then(r=>r.json()).then(data=>{
            if(data.error) { noDataDiv.innerHTML = `Error: ${data.error}`; return; }
            document.getElementById('genreTotal').innerText = data.total_scrobbles;
            document.getElementById('genreArtistsList').innerHTML = data.artists.length ? data.artists.map(a=>`<li><span>${escapeHtml(a)}</span></li>`).join('') : '<li>No artists</li>';
            document.getElementById('genreAlbumsList').innerHTML = data.albums.length ? data.albums.map(a=>`<li><span>${escapeHtml(a.artist)} – ${escapeHtml(a.album)}</span></li>`).join('') : '<li>No albums</li>';
            document.getElementById('genreTracksList').innerHTML = data.tracks.length ? data.tracks.map(t=>`<li><span>${escapeHtml(t.artist)} – ${escapeHtml(t.track)}</span></li>`).join('') : '<li>No tracks</li>';
            resultsDiv.style.display = 'block'; noDataDiv.style.display = 'none';
        }).catch(e=>{ console.error(e); noDataDiv.innerHTML = `Error: ${e.message}`; });
    }

    // ========== SEARCH SCROBBLES ==========
    function loadSearchGenres() {
        fetch('/api/genre_list').then(r=>r.json()).then(genres=>{
            const select = document.getElementById('searchGenre');
            select.innerHTML = '<option value="">All</option>';
            genres.forEach(g => { const opt = document.createElement('option'); opt.value = g; opt.textContent = g; select.appendChild(opt); });
        }).catch(e=>console.error(e));
    }
    function loadPlaylistDropdown() {
        fetch('/api/playlists').then(r=>r.json()).then(playlists=>{
            const select = document.getElementById('searchPlaylist');
            select.innerHTML = '<option value="">All</option>';
            playlists.forEach(playlist => {
                const option = document.createElement('option');
                option.value = playlist;
                option.textContent = playlist;
                select.appendChild(option);
            });
        }).catch(e => console.error('Error loading playlists:', e));
    }
    function searchScrobbles() {
        const params = new URLSearchParams();
        const track = document.getElementById('searchTrack').value.trim(); if(track) params.append('track', track);
        const artist = document.getElementById('searchArtist').value.trim(); if(artist) params.append('artist', artist);
        const album = document.getElementById('searchAlbum').value.trim(); if(album) params.append('album', album);
        const genre = document.getElementById('searchGenre').value; if(genre) params.append('genre', genre);
        const playlist = document.getElementById('searchPlaylist').value; if(playlist) params.append('playlist', playlist);
        const startDate = document.getElementById('searchStartDate').value; if(startDate) params.append('start_date', startDate);
        const endDate = document.getElementById('searchEndDate').value; if(endDate) params.append('end_date', endDate);
        params.append('limit', 100);
        const resultsDiv = document.getElementById('searchResults');
        resultsDiv.innerHTML = '<div class="empty-message">Searching...</div>';
        fetch(`/api/search_scrobbles?${params.toString()}`).then(r=>r.json()).then(data=>{
            if(data.length===0) { resultsDiv.innerHTML = '<div class="empty-message">No scrobbles found.</div>'; return; }
            let html = '<div class="scrobble-list">';
            data.forEach(s => {
                const artUrl = s.art_url || 'https://via.placeholder.com/56?text=🎵';
                const dateStr = formatRelativeTime(s.timestamp);
                html += `<div class="scrobble-item" data-id="${s.id}">
                    <img class="album-art" src="${escapeHtml(artUrl)}" onerror="this.src='https://via.placeholder.com/56?text=🎵'">
                    <div class="track-info">
                        <div class="track-name">${escapeHtml(s.track)}</div>
                        <div class="artist-name">${escapeHtml(s.artist)}</div>
                        <div class="album-name">${escapeHtml(s.album || '')}</div>
                    </div>
                    <div class="scrobble-date">${dateStr}</div>
                    <button class="delete-scrobble" data-id="${s.id}" title="Delete scrobble">🗑️</button>
                </div>`;
            });
            html += '</div>';
            resultsDiv.innerHTML = html;
            attachDeleteHandlersToSearch();
        }).catch(e=>{ console.error(e); resultsDiv.innerHTML = '<div class="empty-message">Error searching scrobbles.</div>'; });
    }
    function attachDeleteHandlersToSearch() {
        document.querySelectorAll('#searchResults .delete-scrobble').forEach(btn => {
            btn.removeEventListener('click', btn._listener);
            const listener = async (e) => {
                e.stopPropagation();
                const row = btn.closest('.scrobble-item');
                const scrobbleId = btn.dataset.id;
                if(!confirm("Delete this scrobble?")) return;
                try {
                    const resp = await fetch(`/api/scrobble/${scrobbleId}`, { method: 'DELETE' });
                    const data = await resp.json();
                    if(resp.ok) {
                        row.remove();
                        if(document.querySelectorAll('#searchResults .scrobble-item').length === 0) {
                            document.getElementById('searchResults').innerHTML = '<div class="empty-message">No scrobbles found.</div>';
                        }
                    } else alert(`Error: ${data.error}`);
                } catch(err) { alert("Delete failed: "+err); }
            };
            btn.addEventListener('click', listener);
            btn._listener = listener;
        });
    }
    function clearSearch() {
        document.getElementById('searchTrack').value = '';
        document.getElementById('searchArtist').value = '';
        document.getElementById('searchAlbum').value = '';
        document.getElementById('searchStartDate').value = '';
        document.getElementById('searchEndDate').value = '';
        document.getElementById('searchGenre').value = '';
        document.getElementById('searchPlaylist').value = '';
        document.getElementById('searchResults').innerHTML = '<div class="empty-message">Enter search criteria and click Search.</div>';
    }

    // ========== EVENT LISTENERS ==========
    document.getElementById('loadReportBtn').addEventListener('click', loadReport);
    document.getElementById('backfillGenresBtn').addEventListener('click', backfillGenres);
    document.getElementById('backfillArtistGenresBtn').addEventListener('click', backfillArtistGenres);
    document.getElementById('backfillAlbumGenresBtn').addEventListener('click', backfillAlbumGenres);
    document.getElementById('searchArtistBtn').addEventListener('click', searchArtists);
    document.getElementById('clearArtistSearchBtn').addEventListener('click', clearArtistSearch);
    document.getElementById('artistSearchInput').addEventListener('keypress', e => { if(e.key==='Enter') searchArtists(); });
    document.getElementById('searchAlbumBtn').addEventListener('click', searchAlbums);
    document.getElementById('clearAlbumSearchBtn').addEventListener('click', clearAlbumSearch);
    document.getElementById('albumSearchInput').addEventListener('keypress', e => { if(e.key==='Enter') searchAlbums(); });
    document.getElementById('loadGenreBtn').addEventListener('click', browseGenre);
    document.getElementById('genreSelect').addEventListener('change', browseGenre);
    document.getElementById('searchScrobblesBtn').addEventListener('click', searchScrobbles);
    document.getElementById('clearSearchBtn').addEventListener('click', clearSearch);
    document.getElementById('saveArtistGenreDropdown').addEventListener('click', saveArtistFromDropdown);
    document.getElementById('clearArtistDropdownBtn').addEventListener('click', clearArtistDropdown);
    
    document.getElementById('backfillMBGenresBtn').addEventListener('click', () => {
        if (!confirm("Update all scrobbles without a manual genre using MusicBrainz suggestions? This may take a while.")) return;
        fetch('/api/backfill_musicbrainz_genres', { method: 'POST' })
            .then(r => r.json())
            .then(data => alert(`MusicBrainz backfill complete. ${data.updated} scrobbles updated.`))
            .catch(e => alert('Error: ' + e));
    });    

    // Initialise everything
    populateDateSelects();
    loadReport();
    loadPlaylists();
    loadGenreMappings();
    loadTrendChart();
    loadGenreList();
    loadSearchGenres();
    loadPlaylistDropdown();
    populateGenreDropdown();
    loadNoGenreArtists();

// Click-to-enable scrolling for all scrollable lists on the monthly page
document.querySelectorAll('.stat-list, .scrobble-list, [style*="max-height"]').forEach(list => {
    list.style.overflow = 'hidden';   // ensure initial state
    list.addEventListener('click', function(e) {
        // Toggle scrolling
        if (list.style.overflowY === 'auto' || list.style.overflowY === 'scroll') {
            list.style.overflow = 'hidden';
        } else {
            list.style.overflowY = 'auto';
        }
        e.stopPropagation();
    });
});

// Clicking outside any list locks scrolling again
document.addEventListener('click', function(e) {
    document.querySelectorAll('.stat-list, .scrobble-list, [style*="max-height"]').forEach(list => {
        if (!list.contains(e.target)) {
            list.style.overflow = 'hidden';
        }
    });
});
</script>
</body>
</html>
"""

# ------------------------- MAIN -------------------------

def signal_handler(sig, frame):
    print("\n👋 Goodbye!")

    if SKIP_SYNC_ON_EXIT:
        print("🛑 Skipping Google Drive sync (--no-sync flag set).")
        sys.exit(0)

    def _sync_and_exit():
        """Perform WAL checkpoint, check remote timestamp, and sync if safe."""
        try:
            # Force WAL checkpoint so all data is in the main database file
            conn = sqlite3.connect(DATABASE)
            conn.execute("PRAGMA wal_checkpoint(FULL)")
            conn.close()

            print("⏳ Syncing to Google Drive...")

            # ------------------------------------------------------------
            # SAFETY CHECK: prevent overwriting a newer remote database
            # ------------------------------------------------------------
            local_max = None
            conn = sqlite3.connect(DATABASE)
            cur = conn.cursor()
            cur.execute("SELECT MAX(timestamp) FROM scrobbles")
            row = cur.fetchone()
            if row and row[0] is not None:
                local_max = row[0]
            conn.close()

            remote_max = None
            tmp_meta = f"/tmp/{SYNC_META_FILE}"
            try:
                subprocess.run(
                    ["rclone", "copyto",
                     f"gdrive-scrobbler:ScrobblerBackup/{SYNC_META_FILE}",
                     tmp_meta],
                    check=True,
                    timeout=60
                )
                with open(tmp_meta, "r") as f:
                    meta = json.load(f)
                remote_max = meta.get("max_timestamp")
            except Exception:
                if local_max is not None:
                    print("   No remote metadata found – assuming first sync.")
                else:
                    print("⚠️ Cannot verify remote state; aborting sync to be safe.")
                    sys.exit(1)

            if remote_max is not None and local_max is not None and local_max < remote_max:
                print("❌ Local database is OLDER than the Google Drive backup!")
                print("   → Please run pull_db.sh first to update this machine.")
                sys.exit(1)

            # ------------------------------------------------------------
            # UPLOAD DATABASE
            # ------------------------------------------------------------
            subprocess.run(
                ["rclone", "copy", DATABASE, "gdrive-scrobbler:ScrobblerBackup/"],
                check=True,
                timeout=300
            )
            for suffix in ["-wal", "-shm"]:
                extra = DATABASE + suffix
                if os.path.exists(extra):
                    subprocess.run(
                        ["rclone", "copy", extra, "gdrive-scrobbler:ScrobblerBackup/"],
                        check=True,
                        timeout=300
                    )

            # ------------------------------------------------------------
            # UPDATE METADATA FILE
            # ------------------------------------------------------------
            if local_max is not None:
                meta = {
                    "max_timestamp": local_max,
                    "host": os.uname().nodename,
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")
                }
                local_meta_path = f"/tmp/{SYNC_META_FILE}"
                with open(local_meta_path, "w") as f:
                    json.dump(meta, f)
                subprocess.run(
                    ["rclone", "copy", local_meta_path, "gdrive-scrobbler:ScrobblerBackup/"],
                    check=True,
                    timeout=60
                )

            print("💾 Database synced to Google Drive.")
            if os.path.exists(DIRTY_FLAG_FILE):
                os.remove(DIRTY_FLAG_FILE)            
        except subprocess.TimeoutExpired:
            print("⚠️ Sync timed out – the file may still have been uploaded.")
        except Exception as e:
            print(f"⚠️ Failed to sync to Google Drive: {e}")
        finally:
            sys.exit(0)

    # Spawn the sync/exit routine as a greenlet – Eventlet will run it immediately
    eventlet.spawn(_sync_and_exit)


if __name__ == '__main__':
    import fcntl

    # --- Prevent duplicate instances ---
    LOCK_FILE = os.path.join(SCRIPT_DIR, "scrobbler.lock")
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
    except IOError:
        print("❌ Another instance is already running. Exiting.")
        sys.exit(1)
    print("🔒 Lock acquired – only one instance running.")
    # --- End duplicate check ---

        # --- Startup sync check: warn if remote database is newer ---
    if not SKIP_SYNC_ON_EXIT:
        try:
            local_check_conn = sqlite3.connect(DATABASE)
            cur = local_check_conn.cursor()
            cur.execute("SELECT MAX(timestamp) FROM scrobbles")
            local_row = cur.fetchone()
            local_max = local_row[0] if local_row and local_row[0] is not None else 0
            local_check_conn.close()

            print("⏳ Checking remote metadata...")

            tmp_meta = "/tmp/startup_meta_check.json"
            download_ok = False
            for attempt in range(2):
                try:
                    subprocess.run(
                        ["rclone", "copyto",
                         f"gdrive-scrobbler:ScrobblerBackup/{SYNC_META_FILE}",
                         tmp_meta],
                        check=True,
                        timeout=20
                    )
                    download_ok = True
                    break
                except subprocess.TimeoutExpired:
                    if attempt == 0:
                        print("   Retrying metadata download...")
                    else:
                        raise
                except Exception:
                    raise

            if not download_ok:
                raise Exception("Metadata download timed out after retry.")

            with open(tmp_meta, "r") as f:
                remote_meta = json.load(f)
            remote_max = remote_meta.get("max_timestamp", 0)

            if remote_max > local_max:
                RED = "\033[91m"
                RESET = "\033[0m"
                print(f"{RED}⚠️  WARNING: The Google Drive backup is NEWER than this local database!{RESET}")
                print(f"{RED}   → Run pull_db.sh FIRST to update this machine, or you may lose scrobbles.{RESET}")
            else:
                print("   ✓ Local database is up‑to‑date.")
        except Exception as e:
            YELLOW = "\033[93m"
            RESET = "\033[0m"
            print(f"{YELLOW}⚠️  Could not verify remote metadata: {e}{RESET}")
            print(f"{YELLOW}   → If you haven't synced recently, run pull_db.sh manually.{RESET}")
    else:
        print("⏭️  Skipping remote metadata check (‑‑no‑sync).")
    # --- End startup sync check ---

    # --- Dirty flag check (unsynced local changes) ---
    if not SKIP_SYNC_ON_EXIT and os.path.exists(DIRTY_FLAG_FILE):
        YELLOW = "\033[93m"
        RESET = "\033[0m"
        print(f"{YELLOW}⚠️  WARNING: You previously used --no-sync on this PC and new scrobbles were recorded!{RESET}")
        print(f"{YELLOW}   → The local database may contain unsynced changes.{RESET}")
        print(f"{YELLOW}   → Before switching to another PC, run this command on THIS machine to push the changes:{RESET}")
        print(f"{YELLOW}     rclone copy {DATABASE} gdrive-scrobbler:ScrobblerBackup/{RESET}")
        print(f"{YELLOW}   → Then delete the flag file: rm {DIRTY_FLAG_FILE}{RESET}")

    init_db()
    poller_thread = threading.Thread(target=background_poller, daemon=True)
    poller_thread.start()

    # Start the backup scheduler as a cooperative greenlet
    eventlet.spawn(backup_scheduler)

    print(f"✅ TIDAL HIFI FULL SCROBBLER (with genre tagging)")
    print(f"📀 Database: {DATABASE}")
    print("🌐 Player: http://127.0.0.1:5000")
    print("📊 Overview: http://127.0.0.1:5000/scrobbles")
    print("📅 Monthly Reports (with playlist rename & genre tagging): http://127.0.0.1:5000/monthly")

    signal.signal(signal.SIGINT, signal_handler)

    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
