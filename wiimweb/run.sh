#!/bin/bash
export PATH="$HOME/bin:$PATH"
# WiiM Web Controller startup script
# Starts Flask web app and wiimplay (for MPRIS/system tray)

set -e

cd "$(dirname "$0")"

# Detect Wayland
if [ "$XDG_SESSION_TYPE" = "wayland" ] || [ -n "$WAYLAND_DISPLAY" ]; then
    echo "Wayland detected - setting GDK_BACKEND=x11 for wiimplay"
    export GDK_BACKEND=x11
else
    echo "X11 detected - no special backend needed"
fi

# Function to clean up
cleanup() {
    echo "Stopping background processes..."
    kill $FLASK_PID $WIIMPLAY_PID 2>/dev/null
    wait $FLASK_PID $WIIMPLAY_PID 2>/dev/null
    echo "Done."
}
trap cleanup INT TERM EXIT

# Start Flask web app
echo "Starting wiimweb Flask app..."
python wiimweb_app.py &
FLASK_PID=$!
echo "Flask PID: $FLASK_PID"

# Locate wiimplay binary
WIIMPLAY_BIN=""
if command -v wiimplay &>/dev/null; then
    WIIMPLAY_BIN=$(command -v wiimplay)
    echo "Found wiimplay in PATH: $WIIMPLAY_BIN"
elif [ -f "wiimplay" ]; then
    WIIMPLAY_BIN="wiimplay"
    echo "Found wiimplay in current directory"
else
    echo "wiimplay not found in PATH or current directory. Skipping MPRIS support."
fi

if [ -n "$WIIMPLAY_BIN" ]; then
    echo "Starting wiimplay from $WIIMPLAY_BIN"
    $WIIMPLAY_BIN &
    WIIMPLAY_PID=$!
    echo "wiimplay PID: $WIIMPLAY_PID"
else
    WIIMPLAY_PID=""
fi

echo "Both services running. Press Ctrl+C to stop."
wait $FLASK_PID
