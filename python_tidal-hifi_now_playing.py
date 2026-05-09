import eventlet
eventlet.monkey_patch()  # Must be the very first thing

import time
import requests
import re
from flask import Flask, render_template_string, request, jsonify
from flask_socketio import SocketIO

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

BASE_URL = "http://127.0.0.1:47836"
CURRENT_URL = f"{BASE_URL}/current"
POLL_INTERVAL = 1

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
            min-width: 400px;  /* Set minimum width to match previous fixed size */
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
            #color:#1db954;
            margin-top:5px;
            margin-bottom:10px;
            display:flex;
            align-items:center;
            gap:8px;
        }
        .progress-container { 
            width:100%;  /* Dynamic width - fills the container */
            height:6px; 
            background:rgba(255,255,255,0.2); 
            border-radius:10px; 
            overflow:hidden; 
            cursor:pointer;
            min-width: 300px;  /* Ensure progress bar doesn't get too small */
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
        .quality-badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 0.85em;
            font-weight: 600;
        }
        .quality-HI_RES_LOSSLESS {
            background: linear-gradient(135deg, #FFB347, #FF8C00);
            color: white;
            text-shadow: 0 0 2px rgba(0,0,0,0.3);
        }
        .quality-LOSSLESS {
            background: linear-gradient(135deg, #40E0D0, #00B4D8);
            color: white;
        }
        .quality-HIGH {
            background: #666;
            color: #eee;
        }
        .quality-LOW {
            background: #444;
            color: #999;
        }
        .bitrate-16bit {
            color: #40E0D0;  /* Turquoise */
            background-color: rgba(64, 224, 208, 0.20);
            box-shadow: 0 0 5px rgba(64, 224, 208, 0.2);
        }
        .bitrate-24bit {
            color: #FFB347;  /* Light orange */
            background-color: rgba(255, 179, 71, 0.20);
            box-shadow: 0 0 5px rgba(255, 179, 71, 0.2);
        }
        .bitrate-gray {
            color: #888888;  /* Gray for low quality or kbps */
            background-color: rgba(136, 136, 136, 0.20);
            box-shadow: 0 0 5px rgba(136, 136, 136, 0.1);
        }
        .codec-badge {
            font-size: 0.75em;
            padding: 2px 6px;
            border-radius: 8px;
            background: rgba(255,255,255,0.1);
            font-family: monospace;
        }
        
        /* Responsive adjustments */
        @media (max-width: 768px) {
            .card { flex-direction: column; align-items: center; gap:20px; padding:20px; }
            .art { width:200px; }
            .track { font-size:1.5em; text-align:center; }
            .artist, .album { text-align:center; }
            .playing-from { justify-content:center; }
            .info { min-width: 280px; }  /* Slightly smaller minimum on mobile */
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
                        <span id="codecBadge" class="codec-badge"></span>
                    </div>
                    <span id="bitrate" class="bitrate"></span>
                </div>
            </div>
        </div>
    </div>
<script>
const socket = io();
let trackDurationSec = 0;

function updateBitrateColor(bitDepth, sampleRate) {
    const bitrateElement = document.getElementById('bitrate');
    // Remove existing classes
    bitrateElement.classList.remove('bitrate-16bit', 'bitrate-24bit', 'bitrate-gray');
    
    // Update based on bit depth
    if (bitDepth >= 24) {
        bitrateElement.classList.add('bitrate-24bit');
    }
    else if (bitDepth >= 16) {
        bitrateElement.classList.add('bitrate-16bit');
    }
    else if (bitDepth > 0 && bitDepth < 16) {
        bitrateElement.classList.add('bitrate-gray');
    }
    // Default to gray for missing data
    else {
        bitrateElement.classList.add('bitrate-gray');
    }
}

function getQualityBadgeText(quality) {
    const qualityMap = {
        'HI_RES_LOSSLESS': 'MAX',
        'LOSSLESS': 'HiFi',
        'HIGH': 'HIGH',
        'LOW': 'LOW'
    };
    return qualityMap[quality] || quality;
}

function updateUI(data) {
    document.getElementById('track').innerText = data.track;
    document.getElementById('artist').innerText = data.artist;
    document.getElementById('album').innerText = data.album;
    document.getElementById('playingFrom').innerHTML = data.playing_from;
    document.getElementById('current').innerText = data.current;
    document.getElementById('duration').innerText = data.duration;
    document.getElementById('progress').style.width = data.progress + '%';
    document.getElementById('metaText').innerHTML = `💿 Volume: ${data.volume}% | 🔀 Shuffle: ${data.shuffle} | 🔁 Repeat: ${data.repeat}`;
    
    // Update bitrate display with new structured data
    const bitrateText = data.badgeText || `${data.bitDepth}-bit ${data.sampleRate/1000}kHz`;
    document.getElementById('bitrate').innerText = bitrateText;
    updateBitrateColor(data.bitDepth, data.sampleRate);
    
    // Update quality badge
    const qualityBadge = document.getElementById('qualityBadge');
    if (data.quality && data.quality !== 'UNKNOWN') {
        qualityBadge.innerText = getQualityBadgeText(data.quality);
        qualityBadge.className = `quality-badge quality-${data.quality}`;
        qualityBadge.style.display = 'inline-flex';
    } else {
        qualityBadge.style.display = 'none';
    }
    
    // Update codec badge
    const codecBadge = document.getElementById('codecBadge');
    if (data.codec) {
        codecBadge.innerText = data.codec;
        codecBadge.style.display = 'inline-block';
    } else {
        codecBadge.style.display = 'none';
    }
    
    document.getElementById('art').src = data.art;
    document.getElementById('bg').style.backgroundImage = `url('${data.art}')`;
    document.getElementById('page-title').innerText = `${data.artist} - ${data.track}`;
    document.getElementById('favicon').href = data.art;

    trackDurationSec = data.duration_sec || trackDurationSec;
}

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

def get_current_track():
    try:
        data = requests.get(CURRENT_URL).json()
        current_sec = data.get("currentInSeconds", 0)
        duration_sec = data.get("durationInSeconds", 1)
        progress = (current_sec / duration_sec) * 100 if duration_sec else 0
        
        # Get playing from directly from the API response
        playing_from_raw = data.get("playingFrom", "Unknown Source")
        playing_from = f"Playing from: {playing_from_raw}"
        
        # Extract audio quality information
        audio_quality = data.get("audioQuality", {})
        
        # Handle both old and new API formats
        if audio_quality:
            # New format with structured audio quality
            quality = audio_quality.get("quality", "UNKNOWN")
            badge_text = audio_quality.get("badgeText", "")
            bit_depth = audio_quality.get("bitDepth", 0)
            sample_rate = audio_quality.get("sampleRate", 0)
            codec = audio_quality.get("codec", "")
            
            # Format badge text if missing
            if not badge_text and bit_depth and sample_rate:
                badge_text = f"{bit_depth}-bit {sample_rate//1000}kHz"
            elif not badge_text and quality == "HI_RES_LOSSLESS":
                badge_text = "24-bit 96kHz"  # Default assumption
            elif not badge_text and quality == "LOSSLESS":
                badge_text = "16-bit 44.1kHz"
        else:
            # Fallback to old format or default
            quality = "UNKNOWN"
            badge_text = data.get("bitrate", "16-bit 44.1kHz")
            bit_depth = 16 if "16-bit" in badge_text else (24 if "24-bit" in badge_text else 0)
            sample_rate = 44100 if "44.1" in badge_text else (96000 if "96" in badge_text else 0)
            codec = "FLAC" if "FLAC" in badge_text or "lossless" in str(data).lower() else "AAC"
        
        # Convert shuffle boolean to "On"/"Off"
        shuffle_raw = data.get("player", {}).get("shuffle", False)
        shuffle_display = "on" if shuffle_raw else "off"
        
        return {
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
            "repeat": str(data.get("player", {}).get("repeat", "OFF")),
            "playing_from": playing_from,
            # New audio quality fields
            "quality": quality,
            "badgeText": badge_text,
            "bitDepth": bit_depth,
            "sampleRate": sample_rate,
            "codec": codec,
            # Keep for backward compatibility
            "bitrate": badge_text
        }
    except Exception as e:
        print("ERROR:", e)
        return {}

def background_task():
    while True:
        track_data = get_current_track()
        if track_data:
            socketio.emit('update', track_data)
        socketio.sleep(POLL_INTERVAL)

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/control/<action>', methods=['POST'])
def control(action):
    try:
        url = None
        if action == 'playpause': url = f"{BASE_URL}/player/playpause"
        elif action == 'next': url = f"{BASE_URL}/player/next"
        elif action == 'previous': url = f"{BASE_URL}/player/previous"
        if url:
            try:
                requests.post(url, timeout=2)
            except Exception:
                try:
                    requests.get(url, timeout=2)
                except Exception as e:
                    print("CONTROL ERROR:", e)
    except Exception as e:
        print("CONTROL ROUTE ERROR:", e)
    return ('', 204)

@app.route('/seek/<int:seconds>', methods=['PUT'])
def seek(seconds):
    try:
        url = f"{BASE_URL}/player/seek/absolute?seconds={seconds}"
        try:
            requests.put(url, timeout=2)
        except Exception:
            try:
                requests.get(url, timeout=2)
            except Exception as e:
                print("SEEK ERROR:", e)
    except Exception as e:
        print("SEEK ROUTE ERROR:", e)
    return ('', 204)

@socketio.on('connect')
def connect():
    print("Client connected")

if __name__ == '__main__':
    socketio.start_background_task(background_task)
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
