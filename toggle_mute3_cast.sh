#!/bin/bash

google_home="Kitchen home"
#google_home="Bedroom mini"

CAST_CMD="/home/swipe/bin/cast-linux-amd64 --name \"$google_home\""

mute () { eval $CAST_CMD mute; }
unmute () { eval $CAST_CMD unmute; }

# Get mute duration based on time/day, or top-of-hour override
get_mute_duration() {
    minute=$(date +%M)   # 00-59
    hour=$(date +%H)     # 00-23
    dow=$(date +%u)      # 1=Monday ... 7=Sunday

    # Top of the hour (minutes 01 through 07)
    if [[ $minute -ge 1 && $minute -le 7 ]]; then
        if [ -f "$HOME/.tr" ]; then
            echo 50
            return
        elif [ -f "$HOME/.lbc" ]; then
            echo 30
            return
        fi
        # If both missing, fall through to normal schedule
    fi

    # Normal time‑based schedule
    if [[ $hour -ge 6 && $hour -lt 19 ]]; then
        echo 121
    elif [[ $hour -ge 19 && $hour -lt 22 ]]; then
        if [[ $dow -ge 1 && $dow -le 5 ]]; then
            echo 140
        else
            echo 125
        fi
    else
        echo 120
    fi
}

# Main mute cycle
duration=$(get_mute_duration)
mute
sleep "$duration"
unmute
