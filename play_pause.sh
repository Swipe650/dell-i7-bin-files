#!/bin/bash

# Get list of players
players=$(playerctl -l 2>/dev/null)

# Check if 'wiimplay' is in the list
if echo "$players" | grep -q "wiimplay"; then
    playerctl --player=wiimplay play-pause
else
    playerctl play-pause
fi
