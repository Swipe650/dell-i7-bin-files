import eventlet
eventlet.monkey_patch()  # Must be the very first thing

import time
import requests
from flask import Flask, render_template_string, request
from flask_socketio import SocketIO
from functools import lru_cache
import hashlib

# ========== CONFIGURATION ==========
BASE_URL = "http://127.0.0.1:47836"
CURRENT_URL = f"{BASE_URL}/current"
POLL_INTERVAL = 1
REQUEST_TIMEOUT = 2
PROGRESS_THRESHOLD = 0.5  # Only send progress update every 0.5%

# Quality mappings
QUALITY_INTERNAL_MAP = {
    'hi_res_lossless': 'hi_res_lossless',
    'max': 'hi_res_lossless',
    'lossless': 'lossless',
    'high': 'lossless',
    'low': 'low'
}

# Bitrate defaults (used when API doesn't provide detailed info)
BITRATE_DEFAULTS = {
    'hi_res_lossless': '24-bit 44.1kHz',
    'lossless': '16-bit 44.1kHz',
    'low': '96 kbps'
}

# ========== FLASK APP INITIALIZATION ==========
app = Flask(__name__)
# Improvement #4: WebSocket Compression
socketio = SocketIO(
    app, 
    cors_allowed_origins="*",
    ping_timeout=60,     # Longer ping timeout
    ping_interval=25     # Less frequent pings
)

# ========== HTML TEMPLATE ==========
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
        .info { 
            display:flex; 
            flex-direction:column; 
            justify-content:center; 
            min-width: 400px;
            flex: 1;
        }
        .track { 
            font-size:2em; 
            word-break:break-word; 
            overflow-wrap:break-word;
        }
        .artist { color:#ccc; }
        .album { color:#999; margin-bottom:20px; }
        .playing-from {
            font-size:0.9em;
            margin-top:5px;
            margin-bottom:10px;
            display:flex;
            align-items:center;
            gap:8px;
        }
        .progress-container { 
            width:100%;
            height:6px; 
            background:rgba(255,255,255,0.2); 
            border-radius:10px; 
            overflow:hidden; 
            cursor:pointer;
            min-width: 300px;
        }
        .progress { height:100%; background:#1db954; width:0%; transition:width 0.2s linear; }
        .time { display:flex; justify-content:space-between; font-size:0.8em; color:#aaa; }
        .controls { margin-top:20px; display:flex; gap:20px; }
        .btn { background:rgba(255,255,255,0.1); border:none; color:white; padding:10px 15px; border-radius:10px; cursor:pointer; font-size:1em; }
        .btn:hover { background:rgba(255,255,255,0.25); }
        .meta { 
            margin-top:10px; 
            font-size:0.85em; 
            color:#bbb;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 8px;
        }
        .quality-badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 0.85em;
            font-weight: 600;
            letter-spacing: 0.5px;
            color: #ddd;
            background: rgba(255,255,255,0.1);
        }
        .bitrate {
            font-size:0.95em;
            font-family: monospace;
            letter-spacing: 0.5px;
            font-weight: bold;
            transition: all 0.3s ease;
            padding: 4px 10px;
            border-radius: 12px;
            backdrop-filter: blur(8px);
            display: inline-block;
        }
        .bitrate-max {
            color: #FFB347;
            background-color: rgba(255, 179, 71, 0.20);
            box-shadow: 0 0 5px rgba(255, 179, 71, 0.2);
        }
        .bitrate-high {
            color: #40E0D0;
            background-color: rgba(64, 224, 208, 0.20);
            box-shadow: 0 0 5px rgba(64, 224, 208, 0.2);
        }
        .bitrate-low {
            color: #888888;
            background-color: rgba(136, 136, 136, 0.20);
            box-shadow: 0 0 5px rgba(136, 136, 136, 0.1);
        }
        .clickable {
            cursor: pointer;
            padding: 2px 6px;
            border-radius: 8px;
            transition: background-color 0.2s ease;
            display: inline-block;
        }
        .clickable:hover {
            background-color: rgba(255,255,255,0.2);
        }
        
        @media (max-width: 768px) {
            .card { flex-direction: column; align-items: center; gap:20px; padding:20px; }
            .art { width:200px; }
            .track { font-size:1.5em; text-align:center; }
            .artist, .album { text-align:center; }
            .playing-from { justify-content:center; }
            .info { min-width: 280px; }
            .meta { flex-direction: column; gap: 5px; text-align: center; }
            .bitrate { margin-top: 5px; }
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
                <div class="time">
                    <span id="current"></span>
                    <span id="duration"></span>
                </div>
                <div class="controls">
                    <button class="btn" onclick="control('previous')">⏮</button>
                    <button class="btn" onclick="control('playpause')">⏯</button>
                    <button class="btn" onclick="control('next')">⏭</button>
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
const socket = io({
    reconnection: true,
    reconnectionAttempts: Infinity,
    reconnectionDelay: 1000,
    reconnectionDelayMax: 5000
});

let trackDurationSec = 0;

// Improvement #6: Browser-side Art Caching
const artCache = new Map();

function updateArt(url) {
    if (!url) return;
    
    if (artCache.has(url)) {
        const cachedUrl = artCache.get(url);
        document.getElementById('art').src = cachedUrl;
        document.getElementById('bg').style.backgroundImage = `url('${cachedUrl}')`;
        return;
    }
    
    const img = new Image();
    img.onload = () => {
        artCache.set(url, img.src);
        document.getElementById('art').src = img.src;
        document.getElementById('bg').style.backgroundImage = `url('${img.src}')`;
    };
    img.src = url;
}

// Quality display mapping
const QUALITY_DISPLAY_MAP = {
    'HI_RES_LOSSLESS': 'HI_RES_LOSSLESS',
    'LOSSLESS': 'LOSSLESS',
    'max': 'HI_RES_LOSSLESS',
    'high': 'LOSSLESS',
    'low': 'Low'
};

// Bitrate defaults
const BITRATE_DEFAULTS = {
    'hi_res_lossless': '24-bit 44.1kHz',
    'lossless': '16-bit 44.1kHz',
    'low': '96 kbps'
};

function updateBitrateColor(qualityType, bitrateText) {
    const bitrateElement = document.getElementById('bitrate');
    bitrateElement.classList.remove('bitrate-max', 'bitrate-high', 'bitrate-low');
    
    if (qualityType === 'max' || qualityType === 'hi_res_lossless') {
        bitrateElement.classList.add('bitrate-max');
    } else if (qualityType === 'high' || qualityType === 'lossless') {
        bitrateElement.classList.add('bitrate-high');
    } else if (qualityType === 'low') {
        bitrateElement.classList.add('bitrate-low');
    } else {
        bitrateElement.classList.add('bitrate-low');
    }
}

function getQualityDisplay(qualityRaw) {
    return QUALITY_DISPLAY_MAP[qualityRaw] || qualityRaw;
}

function getBitrateText(quality, bitDepth, sampleRate, badgeText) {
    // Priority 1: Use badgeText if it contains actual bitrate info
    if (badgeText && /(bit|kHz|kbps)/.test(badgeText)) {
        return badgeText;
    }
    
    // Priority 2: Use bitDepth and sampleRate if available
    if (bitDepth && sampleRate) {
        return `${bitDepth}-bit ${sampleRate/1000}kHz`;
    }
    
    // Priority 3: Use defaults based on quality
    return BITRATE_DEFAULTS[quality] || 'Unknown Quality';
}

function toggleShuffle() {
    fetch('/toggle_shuffle', { method: 'POST' })
        .then(response => {
            if (response.ok) {
                console.log('Shuffle toggled');
                setTimeout(() => location.reload(), 100);
            }
        })
        .catch(error => console.error('Error toggling shuffle:', error));
}

function toggleRepeat() {
    fetch('/toggle_repeat', { method: 'POST' })
        .then(response => {
            if (response.ok) {
                console.log('Repeat toggled');
                setTimeout(() => location.reload(), 100);
            }
        })
        .catch(error => console.error('Error toggling repeat:', error));
}

function updateUI(data) {
    // Always update all fields (simpler and more reliable)
    document.getElementById('track').innerText = data.track;
    document.getElementById('artist').innerText = data.artist;
    document.getElementById('album').innerText = data.album;
    document.getElementById('playingFrom').innerHTML = data.playing_from;
    document.getElementById('current').innerText = data.current;
    document.getElementById('duration').innerText = data.duration;
    document.getElementById('progress').style.width = data.progress + '%';
    
    // Make shuffle and repeat text clickable
    const metaTextElement = document.getElementById('metaText');
    metaTextElement.innerHTML = `💿 Volume: ${data.volume}% | 🔀 <span class="clickable" onclick="toggleShuffle()">Shuffle: ${data.shuffle}</span> | 🔁 <span class="clickable" onclick="toggleRepeat()">Repeat: ${data.repeat}</span>`;
    
    // Update quality badge
    const qualityBadge = document.getElementById('qualityBadge');
    if (data.quality_raw) {
        qualityBadge.innerText = getQualityDisplay(data.quality_raw);
        qualityBadge.style.display = 'inline-flex';
    } else {
        qualityBadge.style.display = 'none';
    }
    
    // Update bitrate display
    const bitrateText = getBitrateText(data.quality, data.bitDepth, data.sampleRate, data.badgeText);
    document.getElementById('bitrate').innerText = bitrateText;
    updateBitrateColor(data.quality, bitrateText);
    
    // Update art with caching
    updateArt(data.art);
    
    document.getElementById('page-title').innerText = `${data.artist} - ${data.track}`;
    document.getElementById('favicon').href = data.art;

    trackDurationSec = data.duration_sec || trackDurationSec;
}

// Improvement #11: Auto-reconnect with better error handling
socket.on('connect', () => {
    console.log('Connected to server');
});

socket.on('disconnect', (reason) => {
    console.log('Disconnected:', reason);
    if (reason === 'io server disconnect') {
        setTimeout(() => socket.connect(), 1000);
    }
});

socket.on('connect_error', (error) => {
    console.log('Connection error:', error);
    setTimeout(() => {
        socket.connect();
    }, 3000);
});

socket.on('update', (data) => {
    updateUI(data);
});

function control(action) {
    fetch(`/control/${action}`, { method: 'POST' });
}

const progressContainer = document.getElementById('progress-container');
progressContainer.addEventListener('click', (e) => {
    const rect = progressContainer.getBoundingClientRect();
    const clickX = e.clientX - rect.left;
    const width = rect.width;
    const percent = clickX / width;
    const seekSeconds = Math.floor(percent * trackDurationSec);

    fetch(`/seek/${seekSeconds}`, { method: 'PUT' });
});
</script>
</body>
</html>
"""

# Improvement #2: Backend Art Caching with LRU
@lru_cache(maxsize=50)
def get_art_hash(url):
    """Generate hash for art URL to track changes"""
    return hashlib.md5(url.encode()).hexdigest()

# Improvement #7: Progress Throttling (simplified)
last_progress = -1

def should_update_progress(current_progress):
    """Only return True if progress changed more than threshold"""
    global last_progress
    if abs(current_progress - last_progress) >= PROGRESS_THRESHOLD:
        last_progress = current_progress
        return True
    return False

# ========== BACKEND FUNCTIONS ==========
def get_current_track():
    """Fetch current track information from Tidal API"""
    try:
        response = requests.get(CURRENT_URL, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        
        # Calculate progress
        current_sec = data.get("currentInSeconds", 0)
        duration_sec = data.get("durationInSeconds", 1)
        progress = (current_sec / duration_sec * 100) if duration_sec else 0
        progress = round(progress, 1)  # Round to 1 decimal place
        
        # Extract audio quality
        audio_quality = data.get("audioQuality", {})
        quality_raw = audio_quality.get("quality", "")
        quality_lower = quality_raw.lower()
        
        # Map to internal quality type for color coding
        quality = QUALITY_INTERNAL_MAP.get(quality_lower, 'low')
        
        # Get player state
        player = data.get("player", {})
        shuffle_display = "on" if player.get("shuffle") else "off"
        repeat = player.get("repeat", "OFF")
        
        track_data = {
            "track": data.get("title"),
            "artist": data.get("artist"),
            "album": data.get("album"),
            "art": data.get("image"),
            "current": data.get("current"),
            "duration": data.get("duration"),
            "duration_sec": duration_sec,
            "progress": progress,
            "volume": round(data.get("volume", 0) * 100),
            "shuffle": shuffle_display,
            "repeat": repeat,
            "playing_from": f"Playing from: {data.get('playingFrom', 'Unknown Source')}",
            "quality_raw": quality_raw,
            "quality": quality,
            "bitDepth": audio_quality.get("bitDepth", 0),
            "sampleRate": audio_quality.get("sampleRate", 0),
            "codec": audio_quality.get("codec", ""),
            "badgeText": audio_quality.get("badgeText", "")
        }
        
        # Improvement #7: Only throttle progress updates
        # Store full data but we'll filter in background task
        return track_data
        
    except requests.RequestException as e:
        print(f"API request failed: {e}")
        return {}
    except Exception as e:
        print(f"Unexpected error in get_current_track: {e}")
        return {}

def background_task():
    """Background task to poll Tidal API and emit updates"""
    last_full_data = {}
    
    while True:
        track_data = get_current_track()
        if track_data:
            # Check if we should send an update
            send_update = False
            
            # Always send if track changed
            if track_data.get('track') != last_full_data.get('track'):
                send_update = True
            # Always send if artist changed
            elif track_data.get('artist') != last_full_data.get('artist'):
                send_update = True
            # Always send if album art changed
            elif track_data.get('art') != last_full_data.get('art'):
                send_update = True
            # Always send if quality changed
            elif track_data.get('quality_raw') != last_full_data.get('quality_raw'):
                send_update = True
            # Check progress with throttling
            elif should_update_progress(track_data.get('progress', 0)):
                send_update = True
            
            if send_update:
                socketio.emit('update', track_data)
                last_full_data = track_data.copy()
        
        socketio.sleep(POLL_INTERVAL)

# ========== FLASK ROUTES ==========
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/control/<action>', methods=['POST'])
def control(action):
    """Handle playback controls"""
    endpoints = {
        'playpause': f"{BASE_URL}/player/playpause",
        'next': f"{BASE_URL}/player/next",
        'previous': f"{BASE_URL}/player/previous"
    }
    
    url = endpoints.get(action)
    if not url:
        return ('', 404)
    
    try:
        # Try POST first, fallback to GET
        try:
            requests.post(url, timeout=REQUEST_TIMEOUT)
        except Exception:
            requests.get(url, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        print(f"CONTROL ERROR ({action}): {e}")
    
    return ('', 204)

@app.route('/toggle_shuffle', methods=['POST'])
def toggle_shuffle():
    """Toggle shuffle mode"""
    try:
        url = f"{BASE_URL}/player/shuffle/toggle"
        requests.post(url, headers={'accept': 'text/plain'}, data='', timeout=REQUEST_TIMEOUT)
        print("Shuffle toggled successfully")
        return ('', 204)
    except Exception as e:
        print(f"SHUFFLE ERROR: {e}")
        return ('', 500)

@app.route('/toggle_repeat', methods=['POST'])
def toggle_repeat():
    """Toggle repeat mode"""
    try:
        url = f"{BASE_URL}/player/repeat/toggle"
        requests.post(url, headers={'accept': 'text/plain'}, data='', timeout=REQUEST_TIMEOUT)
        print("Repeat toggled successfully")
        return ('', 204)
    except Exception as e:
        print(f"REPEAT ERROR: {e}")
        return ('', 500)

@app.route('/seek/<int:seconds>', methods=['PUT'])
def seek(seconds):
    """Seek to specific position in current track"""
    if seconds < 0:
        return ('Bad Request', 400)
    
    try:
        url = f"{BASE_URL}/player/seek/absolute?seconds={seconds}"
        try:
            requests.put(url, timeout=REQUEST_TIMEOUT)
        except Exception:
            requests.get(url, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        print(f"SEEK ERROR: {e}")
    
    return ('', 204)

@socketio.on('connect')
def connect():
    print("Client connected")

# ========== MAIN ENTRY POINT ==========
if __name__ == '__main__':
    socketio.start_background_task(background_task)
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
