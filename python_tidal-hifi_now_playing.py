#!/usr/bin/env python3
import eventlet
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

# ------------------------- CONFIGURATION -------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(SCRIPT_DIR, "scrobbles.db")

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
        lastfm_scrobbled INTEGER DEFAULT 0
    )''')
    conn.commit()
    conn.close()
    migrate_db()

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

def add_scrobble(track, artist, album, art_url, duration_sec, quality, bit_depth, sample_rate, codec, playlist=None):
    timestamp = int(time.time())
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('''INSERT INTO scrobbles 
        (timestamp, track, artist, album, art_url, duration_sec, quality, bit_depth, sample_rate, codec, playlist, lastfm_scrobbled)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (timestamp, track, artist, album, art_url, duration_sec, quality, bit_depth, sample_rate, codec, playlist, 0))
    conn.commit()
    conn.close()
    print(f"📀 Scrobbled locally: {artist} - {track}" + (f" [playlist: {playlist}]" if playlist else ""))
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

def get_top_artists_with_art(limit=25):
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

def get_top_albums_with_art(limit=25):
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

def get_top_tracks_with_art(limit=25):
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

def get_top_playlists(limit=25):
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
                (timestamp, track, artist, album, art_url, duration_sec, quality, bit_depth, sample_rate, codec, playlist, lastfm_scrobbled)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (item['timestamp'], item['track'], item['artist'], item.get('album'),
                 item.get('art_url'), item.get('duration_sec'), item.get('quality'),
                 item.get('bit_depth'), item.get('sample_rate'), item.get('codec'), 
                 item.get('playlist'), 0))
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
                print(f"Cached quality for {album_name}: {badge_text}")

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
        print(f"HTTP fetch error: {e}")

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
                if track_changed and session["last_track_data"]:
                    if maybe_scrobble(session["last_track_data"], session["max_position"], session["last_track_data"]["duration_sec"]):
                        ld = session["last_track_data"]
                        playing_from = current_track_data.get("playing_from", "").replace("Playing from: ", "")
                        album_name = current_track_data.get("album", "")
                        playlist_name = playing_from if playing_from != album_name else None
                        add_scrobble(ld["track"], ld["artist"], ld["album"], ld["art_url"], ld["duration_sec"],
                                     ld["quality"], ld["bit_depth"], ld["sample_rate"], ld["codec"], playlist_name)
                    session["track_start_time"] = time.time()
                    session["max_position"] = 0
                    session["last_track_data"] = None
                if track_changed or not session["last_track_data"]:
                    track_start_timestamp = int(time.time())
                    session["track_start_timestamp"] = track_start_timestamp
                    session["last_track_data"] = {
                        "track": title, "artist": meta["artist"], "album": meta["album"],
                        "art_url": current_track_data.get("art", ""), "duration_sec": meta["duration_sec"],
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
                pos = meta["position"]
                if pos > session["max_position"]:
                    session["max_position"] = pos
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
        fetch_http_details()
        with session["lock"]:
            if session["last_track_data"]:
                session["last_track_data"]["art_url"] = current_track_data.get("art", session["last_track_data"]["art_url"])
                session["last_track_data"]["quality"] = current_track_data.get("quality", session["last_track_data"]["quality"])
                session["last_track_data"]["bit_depth"] = current_track_data.get("bitDepth", session["last_track_data"]["bit_depth"])
                session["last_track_data"]["sample_rate"] = current_track_data.get("sampleRate", session["last_track_data"]["sample_rate"])
                session["last_track_data"]["codec"] = current_track_data.get("codec", session["last_track_data"]["codec"])
        socketio.emit('update', current_track_data)
        eventlet.sleep(POLL_INTERVAL)

# ------------------------- FLASK ROUTES -------------------------
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
    top_artists = get_top_artists_with_art(25)
    top_albums = get_top_albums_with_art(25)
    top_tracks = get_top_tracks_with_art(25)
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
        LIMIT 25
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
        LIMIT 5
    """)
    rows = c.fetchall()
    conn.close()
    return jsonify([{"date": row[0], "scrobbles": row[1]} for row in rows])

@app.route('/api/top_playlists')
def api_top_playlists():
    top = get_top_playlists(25)
    return jsonify(top)

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
            playlist_name = playing_from if playing_from != album_name else None
            add_scrobble(ld["track"], ld["artist"], ld["album"], ld["art_url"], ld["duration_sec"],
                         ld["quality"], ld["bit_depth"], ld["sample_rate"], ld["codec"], playlist_name)
            return jsonify({"status": "scrobbled"})
    return jsonify({"status": "no track playing"}), 400

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
        .overlay { display:flex; height:100vh; align-items:center; justify-content:center; }
        .card { display:flex; gap:40px; background:rgba(0,0,0,0.4); padding:30px; border-radius:20px; backdrop-filter:blur(20px); max-width:90vw; }
        .art { width:260px; border-radius:16px; }
        .info { display:flex; flex-direction:column; justify-content:center; min-width: 400px; flex: 1; }
        .track { font-size:2em; word-break:break-word; }
        .artist { color:#ccc; }
        .album { color:#999; margin-bottom:20px; }
        .playing-from { font-size:0.9em; margin-top:5px; margin-bottom:10px; display:flex; align-items:center; gap:8px; }
        .progress-container { width:100%; height:6px; background:rgba(255,255,255,0.2); border-radius:10px; overflow:hidden; cursor:default; }
        .progress { height:100%; background:#1db954; width:0%; transition:width 0.2s linear; }
        .time { display:flex; justify-content:space-between; font-size:0.8em; color:#aaa; }
        .controls { margin-top:20px; display:flex; gap:20px; flex-wrap: wrap; }
        .btn { background:rgba(255,255,255,0.1); border:none; color:white; padding:10px 15px; border-radius:10px; cursor:pointer; font-size:1em; transition: background-color 0.2s ease; }
        .btn:hover { background:rgba(255,255,255,0.25); }
        .meta { margin-top:10px; font-size:0.85em; color:#bbb; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
        .quality-badge { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 12px; font-size: 0.85em; font-weight: 600; background: rgba(255,255,255,0.1); }
        .bitrate { font-size:0.95em; font-family: monospace; padding: 4px 10px; border-radius: 12px; backdrop-filter: blur(8px); display: inline-block; }
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
        }
    </style>
</head>
<body>
    <div id="bg" class="bg"></div>
    <div class="overlay">
        <div class="card">
            <img id="art" class="art" src="" />
            <div class="info">
                <div id="track" class="track"></div>
                <div id="artist" class="artist"></div>
                <div id="album" class="album"></div>
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
                    <div style="display: flex; gap: 8px; flex-wrap: wrap; align-items: center;">
                        <span id="metaText"></span>
                        <span id="qualityBadge" class="quality-badge"></span>
                    </div>
                    <span id="bitrate" class="bitrate"></span>
                </div>
            </div>
        </div>
    </div>
<script>
const socket = io({ reconnection: true, reconnectionAttempts: Infinity, reconnectionDelay: 1000 });
let trackDurationSec = 0;
const artCache = new Map();

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

function updateUI(data) {
    document.getElementById('track').innerText = data.track;
    document.getElementById('artist').innerText = data.artist;
    document.getElementById('album').innerText = data.album;
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
        .stat-list { list-style: none; padding: 0; margin: 0; max-height: 300px; overflow-y: auto; padding-right: 5px; }
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
        .time-label { font-size: 0.65rem; text-transform: uppercase; letter-spacing: 1px; color: var(--text-muted); }
        .time-value { font-size: 1.2rem; font-weight: 600; color: var(--accent); }
        .total-scrobbles { text-align: center; font-size: 0.8rem; margin-top: 0.3rem; color: var(--text-muted); }
        .tools { background: var(--bg-tools); border-radius: 12px; padding: 1rem; margin-bottom: 2rem; display: flex; flex-wrap: wrap; gap: 12px; align-items: center; box-shadow: 0 1px 3px var(--shadow); border: 1px solid var(--border-card); }
        .tools button, .tools label { background: var(--button-bg); border: 1px solid var(--button-border); padding: 0.5rem 1rem; border-radius: 30px; font-size: 0.8rem; cursor: pointer; font-family: inherit; transition: all 0.2s; color: var(--text-primary); }
        .tools button:hover, .tools label:hover { background: var(--button-hover); border-color: var(--text-muted); }
        .theme-toggle { background: var(--button-bg); border: 1px solid var(--button-border); border-radius: 30px; padding: 0.5rem 1rem; font-size: 0.8rem; cursor: pointer; display: inline-flex; align-items: center; gap: 6px; }
        .scrobble-list { background: var(--bg-card); border-radius: 16px; box-shadow: 0 1px 4px var(--shadow); overflow: hidden; border: 1px solid var(--border-card); margin-top: 1rem; }
        .scrobble-item { display: flex; align-items: center; gap: 1rem; padding: 1rem; border-bottom: 1px solid var(--border-card); transition: background 0.15s; }
        .scrobble-item:hover { background: var(--hover-row); }
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
            <a href="/monthly" class="report-link" target="_blank">📅 Monthly Reports</a>
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
        <div class="total-scrobbles" id="totalScrobbles"></div>
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
            <h3>🏆 Top 5 Listening Days</h3>
            <ul class="stat-list" id="topDaysList">
                <li>Loading...</li>
            </ul>
        </div>
        <div class="stat-card">
            <h3>📀 Top Playlists</h3>
            <ul class="stat-list" id="topPlaylistsList"><li>Loading...</li></ul>
        </div>
        <div class="stat-card">
            <h3>📈 Monthly Trend (last 12 months)</h3>
            <canvas id="trendChart" width="100%" height="200"></canvas>
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
            document.getElementById('timeYear').innerText = data.year + 'h';
        }).catch(e => console.error('Listening time error:', e));
    }

    function fetchStats() {
        fetch('/api/stats').then(r => r.json()).then(data => {
            document.getElementById('totalScrobbles').innerText = `Total scrobbles: ${data.total_scrobbles}`;
            
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
                type: 'bar', data: { labels: dayOrder, datasets: [{ label: 'Scrobbles', data: orderedData, backgroundColor: '#36a2eb', borderRadius: 4 }] },
                options: { responsive: true, scales: { y: { beginAtZero: true } } }
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

    function fetchMonthlyTrend() {
        fetch('/api/monthly_trend')
            .then(r => r.json())
            .then(data => {
                const ctx = document.getElementById('trendChart').getContext('2d');
                if (window.trendChart) window.trendChart.destroy();
                window.trendChart = new Chart(ctx, {
                    type: 'bar',
                    data: {
                        labels: data.map(d => d.month),
                        datasets: [{
                            label: 'Scrobbles',
                            data: data.map(d => d.scrobbles),
                            backgroundColor: '#36a2eb',
                            borderRadius: 4
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: true,
                        plugins: { legend: { position: 'top' } }
                    }
                });
            })
            .catch(e => console.error('Monthly trend error:', e));
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
                fetchMonthlyTrend();
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
    function importData(file) { if (!file) return; const formData = new FormData(); formData.append('file', file); fetch('/import', { method: 'POST', body: formData }).then(r => r.json()).then(data => { alert(data.status || data.error); loadScrobbles(currentOffset); fetchStats(); fetchListeningTime(); fetchListeningClock(); fetchWeekdayStats(); fetchTopArtistsByTime(); fetchTopDays(); fetchTopPlaylists(); fetchMonthlyTrend(); }).catch(e => alert('Import failed: ' + e)); }

    fetchNowPlaying();
    fetchStats();
    fetchListeningTime();
    fetchListeningClock();
    fetchWeekdayStats();
    fetchTopArtistsByTime();
    fetchTopDays();
    fetchTopPlaylists();
    fetchMonthlyTrend();
    loadScrobbles(0);
    setInterval(fetchNowPlaying, 5000);
</script>
</body>
</html>
"""

MONTHLY_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Monthly Listening Report · TIDAL</title>
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
        .controls { margin-bottom: 1.5rem; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
        input { padding: 8px 12px; border-radius: 8px; border: 1px solid var(--border-card); background: var(--button-bg); color: var(--text-primary); }
        button { background: var(--accent); border: none; padding: 8px 16px; border-radius: 8px; cursor: pointer; color: white; font-weight: bold; }
        .report-stats { display: flex; gap: 2rem; justify-content: center; margin-bottom: 2rem; }
        .stat-badge { text-align: center; background: var(--bg-card); padding: 1rem; border-radius: 16px; min-width: 150px; box-shadow: 0 1px 4px var(--shadow); }
        .stat-badge .label { font-size: 0.8rem; color: var(--text-muted); }
        .stat-badge .value { font-size: 2rem; font-weight: 600; color: var(--accent); }
        .tables-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1.5rem; margin-bottom: 2rem; }
        .table-card { background: var(--bg-card); border-radius: 16px; padding: 1rem; border: 1px solid var(--border-card); }
        .table-card h3 { margin: 0 0 1rem 0; font-size: 1.2rem; color: var(--accent); border-left: 3px solid var(--accent); padding-left: 0.75rem; }
        .stat-list { list-style: none; padding: 0 5px 0 0; margin: 0; max-height: 300px; overflow-y: auto; }
        .stat-list li { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid var(--border-card); }
        .stat-list li span:first-child { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding-right: 1rem; }
        .stat-count { font-weight: 600; color: var(--accent); margin-left: auto; flex-shrink: 0; }
        .stat-list::-webkit-scrollbar { width: 6px; }
        .stat-list::-webkit-scrollbar-track { background: var(--border-card); border-radius: 3px; }
        .stat-list::-webkit-scrollbar-thumb { background: #aaa; border-radius: 3px; }
        body.dark .stat-list::-webkit-scrollbar-thumb { background: #aaa; }
        canvas { max-height: 300px; margin-top: 1rem; }
        footer { margin-top: 3rem; text-align: center; font-size: 0.7rem; color: var(--text-muted); }
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <div>
            <h1>📅 Monthly Listening Report</h1>
            <div class="sub">detailed breakdown for any month</div>
        </div>
        <a href="/scrobbles" class="back-link">◀ Back to Overview</a>
    </div>

    <div class="controls">
        <input type="month" id="monthPicker" value="2025-05">
        <button id="loadReportBtn">Generate Report</button>
    </div>

    <div id="reportContent" style="display: none;">
        <div class="report-stats">
            <div class="stat-badge"><div class="label">Total Scrobbles</div><div class="value" id="totalScrobbles">-</div></div>
            <div class="stat-badge"><div class="label">Listening Time</div><div class="value" id="totalHours">-</div><div class="label">hours</div></div>
        </div>

        <div class="tables-grid">
            <div class="table-card"><h3>🎤 Top Artists</h3><ul class="stat-list" id="topArtists"></ul></div>
            <div class="table-card"><h3>💿 Top Albums</h3><ul class="stat-list" id="topAlbums"></ul></div>
            <div class="table-card"><h3>🎵 Top Tracks</h3><ul class="stat-list" id="topTracks"></ul></div>
        </div>

        <div class="table-card">
            <h3>🕒 Listening Clock (Hourly Distribution)</h3>
            <canvas id="clockChart" width="100%" height="250"></canvas>
        </div>
    </div>
    <div id="loadingMsg" style="text-align: center; padding: 2rem;">Select a month and click Generate Report.</div>
</div>

<script>
    let clockChart = null;

    async function loadReport() {
        const monthInput = document.getElementById('monthPicker').value;
        if (!monthInput) {
            console.error("No month selected");
            return;
        }
        const [year, month] = monthInput.split('-');
        const contentDiv = document.getElementById('reportContent');
        const loadingMsg = document.getElementById('loadingMsg');
        
        loadingMsg.style.display = 'block';
        loadingMsg.innerText = 'Loading report...';
        contentDiv.style.display = 'none';
        
        console.log(`Fetching monthly report for year=${year}, month=${month}`);
        
        try {
            const response = await fetch(`/api/monthly_report?year=${year}&month=${month}`);
            console.log("Response status:", response.status);
            
            if (!response.ok) {
                throw new Error(`HTTP error ${response.status}`);
            }
            
            const data = await response.json();
            console.log("Received data:", data);
            
            if (data.error) {
                loadingMsg.innerText = `Error: ${data.error}`;
                console.error("API error:", data.error);
                return;
            }
            
            document.getElementById('totalScrobbles').innerText = data.total_scrobbles;
            document.getElementById('totalHours').innerText = data.total_hours;
            
            const artistsList = document.getElementById('topArtists');
            if (data.top_artists.length === 0) {
                artistsList.innerHTML = '<li>No scrobbles in this month</li>';
            } else {
                artistsList.innerHTML = data.top_artists.map(a => `<li><span>${escapeHtml(a.artist)}</span><span class="stat-count">${a.count}</span></li>`).join('');
            }
            
            const albumsList = document.getElementById('topAlbums');
            if (data.top_albums.length === 0) {
                albumsList.innerHTML = '<li>No albums in this month</li>';
            } else {
                albumsList.innerHTML = data.top_albums.map(a => `<li><span>${escapeHtml(a.artist)} – ${escapeHtml(a.album)}</span><span class="stat-count">${a.count}</span></li>`).join('');
            }
            
            const tracksList = document.getElementById('topTracks');
            if (data.top_tracks.length === 0) {
                tracksList.innerHTML = '<li>No tracks in this month</li>';
            } else {
                tracksList.innerHTML = data.top_tracks.map(t => `<li><span>${escapeHtml(t.artist)} – ${escapeHtml(t.track)}</span><span class="stat-count">${t.count}</span></li>`).join('');
            }
            
            const ctx = document.getElementById('clockChart').getContext('2d');
            if (clockChart) clockChart.destroy();
            clockChart = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: Array.from({length: 24}, (_, i) => `${i}:00`),
                    datasets: [{
                        label: 'Scrobbles',
                        data: data.hour_counts,
                        backgroundColor: '#36a2eb',
                        borderRadius: 4
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: true,
                    scales: { y: { beginAtZero: true, title: { display: true, text: 'Scrobbles' } },
                              x: { title: { display: true, text: 'Hour of Day' } } }
                }
            });
            
            loadingMsg.style.display = 'none';
            contentDiv.style.display = 'block';
            
        } catch (err) {
            console.error("Fetch error:", err);
            loadingMsg.innerText = `Error loading report: ${err.message}. See console.`;
            loadingMsg.style.color = 'var(--accent)';
        }
    }
    
    function escapeHtml(str) {
        if (!str) return '';
        return str.replace(/[&<>]/g, m => m === '&' ? '&amp;' : m === '<' ? '&lt;' : '&gt;');
    }
    
    const now = new Date();
    document.getElementById('monthPicker').value = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
    document.getElementById('loadReportBtn').addEventListener('click', loadReport);
    
    if (localStorage.getItem('scrobbleTheme') === 'dark') document.body.classList.add('dark');
    
    loadReport();
</script>
</body>
</html>
"""

# ------------------------- MAIN -------------------------
def signal_handler(sig, frame):
    print("\n👋 Goodbye!")
    sys.exit(0)

if __name__ == '__main__':
    init_db()
    poller_thread = threading.Thread(target=background_poller, daemon=True)
    poller_thread.start()
    print(f"✅ TIDAL HIFI FULL SCROBBLER (Last.fm + Monthly Reports in new page)")
    print(f"📀 Database: {DATABASE}")
    print("🌐 Player: http://127.0.0.1:5000")
    print("📊 Overview: http://127.0.0.1:5000/scrobbles")
    print("📅 Monthly Reports: http://127.0.0.1:5000/monthly")
    
    # Set up Ctrl+C handler
    signal.signal(signal.SIGINT, signal_handler)
    
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
