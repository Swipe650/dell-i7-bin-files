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
            color: #FFB347;  /* Light orange */
            background-color: rgba(255, 179, 71, 0.20);
            box-shadow: 0 0 5px rgba(255, 179, 71, 0.2);
        }
        .bitrate-high {
            color: #40E0D0;  /* Turquoise */
            background-color: rgba(64, 224, 208, 0.20);
            box-shadow: 0 0 5px rgba(64, 224, 208, 0.2);
        }
        .bitrate-low {
            color: #888888;  /* Gray */
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
        
        /* Responsive adjustments */
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
const socket = io();
let trackDurationSec = 0;

function updateBitrateColor(qualityType, bitrateText) {
    const bitrateElement = document.getElementById('bitrate');
    // Remove existing classes
    bitrateElement.classList.remove('bitrate-max', 'bitrate-high', 'bitrate-low');
    
    // Add class based on quality type
    if (qualityType === 'max' || qualityType === 'hi_res_lossless') {
        bitrateElement.classList.add('bitrate-max');    // Orange
    } else if (qualityType === 'high' || qualityType === 'lossless') {
        bitrateElement.classList.add('bitrate-high');   // Turquoise
    } else if (qualityType === 'low') {
        bitrateElement.classList.add('bitrate-low');    // Gray
    } else {
        bitrateElement.classList.add('bitrate-low');    // Default to gray
    }
}

function getQualityDisplay(qualityRaw) {
    // Return the exact API value if it's HI_RES_LOSSLESS or LOSSLESS
    if (qualityRaw === 'HI_RES_LOSSLESS') {
        return 'HI_RES_LOSSLESS';
    }
    if (qualityRaw === 'LOSSLESS') {
        return 'LOSSLESS';
    }
    
    // Otherwise use friendly names
    const qualityMap = {
        //'max': 'Max',
        //'high': 'High',
        'max': 'HI_RES_LOSSLESS',
        'high': 'LOSSLESS',
        'low': 'Low'
    };
    return qualityMap[qualityRaw] || qualityRaw;
}

function getBitrateText(quality, bitDepth, sampleRate, badgeText) {
    // Check if badgeText contains actual bitrate info (bit/kHz/kbps)
    const hasBitrateInfo = badgeText && (badgeText.includes('bit') || badgeText.includes('kHz') || badgeText.includes('kbps'));
    
    if (hasBitrateInfo) {
        // Use the badgeText from API (e.g., "24-bit 96kHz", "96 kbps")
        return badgeText;
    }
    
    // No detailed bitrate info, use defaults based on quality
    if (quality === 'max' || quality === 'hi_res_lossless') {
        return '24-bit 44.1kHz';
    } else if (quality === 'high' || quality === 'lossless') {
        return '16-bit 44.1kHz';
    } else if (quality === 'low') {
        return '96 kbps';
    }
    
    return 'Unknown Quality';
}

function toggleShuffle() {
    fetch('/toggle_shuffle', { method: 'POST' })
        .then(response => {
            if (response.ok) {
                console.log('Shuffle toggled');
                // Refresh the current track data after a short delay
                setTimeout(() => {
                    location.reload();
                }, 100);
            }
        })
        .catch(error => console.error('Error toggling shuffle:', error));
}

function toggleRepeat() {
    fetch('/toggle_repeat', { method: 'POST' })
        .then(response => {
            if (response.ok) {
                console.log('Repeat toggled');
                // Refresh the current track data after a short delay
                setTimeout(() => {
                    location.reload();
                }, 100);
            }
        })
        .catch(error => console.error('Error toggling repeat:', error));
}

function updateUI(data) {
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
        const qualityDisplay = getQualityDisplay(data.quality_raw);
        qualityBadge.innerText = qualityDisplay;
        qualityBadge.style.display = 'inline-flex';
    } else {
        qualityBadge.style.display = 'none';
    }
    
    // Update bitrate display with colored text
    const bitrateText = getBitrateText(data.quality, data.bitDepth, data.sampleRate, data.badgeText);
    document.getElementById('bitrate').innerText = bitrateText;
    updateBitrateColor(data.quality, bitrateText);
    
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
        
        # Extract audio quality information (handles both formats)
        audio_quality = data.get("audioQuality", {})
        
        # Get the raw quality value from API
        quality_raw = audio_quality.get("quality", "")
        
        # Map to internal quality types for color coding and bitrate defaults
        quality_lower = quality_raw.lower()
        if quality_lower in ["hi_res_lossless", "max"]:
            quality = "hi_res_lossless"  # MAX quality
        elif quality_lower in ["lossless", "high"]:
            quality = "lossless"  # HIGH quality (16-bit)
        elif quality_lower == "low":
            quality = "low"  # LOW quality
        else:
            quality = "low"  # Default
        
        # Get detailed info if available
        bit_depth = audio_quality.get("bitDepth", 0)
        sample_rate = audio_quality.get("sampleRate", 0)
        codec = audio_quality.get("codec", "")
        
        # Get badgeText if available
        badge_text = audio_quality.get("badgeText", "")
        
        # Convert shuffle boolean to "on"/"off"
        shuffle_raw = data.get("player", {}).get("shuffle", False)
        shuffle_display = "on" if shuffle_raw else "off"
        
        # Get repeat status
        repeat = data.get("player", {}).get("repeat", "OFF")
        
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
            "repeat": repeat,
            "playing_from": playing_from,
            "quality_raw": quality_raw,  # Pass the original API value
            "quality": quality,  # Internal mapping for color coding
            "bitDepth": bit_depth,
            "sampleRate": sample_rate,
            "codec": codec,
            "badgeText": badge_text
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

@app.route('/toggle_shuffle', methods=['POST'])
def toggle_shuffle():
    try:
        url = f"{BASE_URL}/player/shuffle/toggle"
        response = requests.post(url, headers={'accept': 'text/plain'}, data='', timeout=2)
        print(f"Shuffle toggled, response: {response.status_code}")
        return ('', 204)
    except Exception as e:
        print("SHUFFLE ERROR:", e)
        return ('', 500)

@app.route('/toggle_repeat', methods=['POST'])
def toggle_repeat():
    try:
        url = f"{BASE_URL}/player/repeat/toggle"
        response = requests.post(url, headers={'accept': 'text/plain'}, data='', timeout=2)
        print(f"Repeat toggled, response: {response.status_code}")
        return ('', 204)
    except Exception as e:
        print("REPEAT ERROR:", e)
        return ('', 500)

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
