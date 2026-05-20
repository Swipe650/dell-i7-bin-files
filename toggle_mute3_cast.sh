#!/bin/bash

google_home="Kitchen home"
#google_home="Bedroom mini"

CAST_CMD="/home/swipe/bin/cast-linux-amd64 --name \"$google_home\""

mute () { eval $CAST_CMD mute; }
unmute () { eval $CAST_CMD unmute; }

get_mute_duration() {
    minute=$(date +%M)          # Current minute (00-59)
    hour=$(date +%H)            # Current hour (00-23)
    dow=$(date +%u)             # Day of week: 1=Monday ... 7=Sunday

    # ----- TOP OF THE HOUR (minutes 01 through 07) -----
    # If you run the script during these minutes and a .tr or .lbc flag file exists,
    # it will override the normal schedule.
    if [[ $minute -ge 1 && $minute -le 7 ]]; then
        if [ -f "$HOME/.tr" ]; then
            echo 50             # TalkRadio top-of-hour ad break
            return
        elif [ -f "$HOME/.lbc" ]; then
            echo 30             # LBC top-of-hour ad break
            return
        fi
        # If no flag file, fall through to the normal schedule below
    fi

    # ----- NORMAL TIME‑BASED SCHEDULE (edit these values easily) -----
    
    # TIME PERIOD 1: 06:00 AM to 07:00 PM (6 AM – 7 PM) – every day
    if [[ $hour -ge 6 && $hour -lt 19 ]]; then
        echo 121                # Mute for 121 seconds

    # TIME PERIOD 2: 07:00 PM to 10:00 PM (7 PM – 10 PM) – split by weekday/weekend
    elif [[ $hour -ge 19 && $hour -lt 22 ]]; then
        if [[ $dow -ge 1 && $dow -le 5 ]]; then
            echo 140            # Monday to Friday: 140 seconds
        else
            echo 125            # Saturday & Sunday: 125 seconds
        fi

    # TIME PERIOD 3: 10:00 PM to 06:00 AM (10 PM – 6 AM) – overnight daily
    else
        echo 120                # Mute for 120 seconds
    fi
}

# Run the mute cycle
duration=$(get_mute_duration)
mute
sleep "$duration"
unmute
