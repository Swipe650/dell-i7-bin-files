#!/bin/bash

google_home="Kitchen home"
#google_home="Bedroom mini"

CAST_CMD="/home/swipe/bin/cast-linux-amd64 --name \"$google_home\""

# Retry helper: runs a command up to 3 times if it fails
run_with_retry() {
    local cmd="$1"
    local max_retries=3
    local retry_delay=2
    local attempt=1

    while [ $attempt -le $max_retries ]; do
        eval $cmd
        if [ $? -eq 0 ]; then
            return 0
        fi
        echo "Command failed (attempt $attempt/$max_retries): $cmd" >&2
        sleep $retry_delay
        ((attempt++))
    done

    echo "Command failed after $max_retries attempts: $cmd" >&2
    #return 1
    exit 1
}

mute () { run_with_retry "$CAST_CMD mute"; }
unmute () { run_with_retry "$CAST_CMD unmute"; }

get_mute_duration() {
    # Force decimal interpretation to avoid octal errors (08, 09)
    hour=$((10#$(date +%H)))
    minute=$((10#$(date +%M)))
    dow=$(date +%u)                     # 1=Monday ... 7=Sunday

    # ----- SPECIAL LBC AFTERNOON RULE (weekdays, 4 PM to 7 PM) -----
    if [[ $hour -ge 16 && $hour -lt 19 ]] && \
       [ -f "$HOME/.lbc" ] && \
       [[ $dow -ge 1 && $dow -le 5 ]]; then
        echo 220
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
    if [[ $hour -ge 6 && $hour -lt 19 ]]; then
        echo 170
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

duration=$(get_mute_duration)
mute
sleep "$duration"
unmute
