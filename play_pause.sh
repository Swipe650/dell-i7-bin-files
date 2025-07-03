#!/bin/bash

# Get list of players
players=$(playerctl -l 2>/dev/null)

# Check if 'wiimplay' is in the list
if echo "$players" | grep -q "wiimplay"; then
    playerctl --player=wiimplay play-pause
else
    playerctl -p "$(playerctl -l | awk 'NR==3')" play-pause
fi
