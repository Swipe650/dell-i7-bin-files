#!/bin/bash

google_home="Kitchen home"
#google_home="Bedroom mini"

CAST_CMD="/home/swipe/bin/cast-linux-amd64 --name \"$google_home\""

mute () { eval $CAST_CMD mute; }
unmute () { eval $CAST_CMD unmute; }

get_mute_duration() {
    # Get hour and minute as decimal numbers (strip leading zeros to avoid octal issues)
    hour=$((10#$(date +%H)))      # 0-23, decimal
    minute=$((10#$(date +%M)))    # 0-59, decimal
    dow=$(date +%u)               # 1=Monday ... 7=Sunday (no leading zeros)

    # ----- SPECIAL LBC AFTERNOON RULE (weekdays, 4 PM to 7 PM) -----
    if [[ $hour -ge 16 && $hour -lt 19 ]] && \
       [ -f "$HOME/.lbc" ] && \
       [[ $dow -ge 1 && $dow -le 5 ]]; then
        echo 180
        return
    fi

    # ----- TOP OF THE HOUR (minutes 01 through 07) -----
    if [[ $minute -ge 1 && $minute -le 7 ]]; then
        if [ -f "$HOME/.tr" ]; then
            echo 50
            return
        elif [ -f "$HOME/.lbc" ]; then
            echo 30
            return
        fi
    fi

    # ----- NORMAL TIME‑BASED SCHEDULE -----
    # 06:00 – 19:00 (every day)
    if [[ $hour -ge 6 && $hour -lt 19 ]]; then
        echo 121

    # 19:00 – 22:00 (weekday/weekend split)
    elif [[ $hour -ge 19 && $hour -lt 22 ]]; then
        if [[ $dow -ge 1 && $dow -le 5 ]]; then
            echo 140
        else
            echo 125
        fi

    # 22:00 – 06:00 (overnight)
    else
        echo 120
    fi
}

duration=$(get_mute_duration)
mute
sleep "$duration"
unmute
