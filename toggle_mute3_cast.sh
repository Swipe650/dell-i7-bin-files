#!/bin/bash

google_home="Kitchen home"
#google_home="Bedroom mini"

# Unmute the device
mute () { /home/swipe/bin/cast-linux-amd64 --name "$google_home" mute; }

# Mute the device if it's not already muted
unmute () { /home/swipe/bin/cast-linux-amd64 --name "$google_home" unmute; }

# Default mute/unmute cycle
set_default_mute_time() 
{
    mute
    sleep 180
    unmute
}

# LBC-specific mute/unmute cycle
set_lbc_mute_time() {
    mute
    sleep 160
    unmute
}

# Function to check the top of the hour for different cases
check_top_of_the_hour() {
    # Get the current time in minutes
    currenttime=$(date +%M)

    # Check for TalkRadio
    if [ -f .tr ]; then
        if [[ "$currenttime" =~ ^(00|01|02|03|04|58|59)$ ]]; then
            mute
            sleep 180
            unmute
        elif [[ "$currenttime" =~ ^(32|33|34|35|36|37|05|06|07)$ ]]; then
            mute
            sleep 140
            unmute
        else
            mute
            set_default_mute_time
            unmute
        fi
    fi

    # Check for LBC UK
    if [ -f .lbc ]; then
        if [ "$currenttime" -gt "00" ] && [ "$currenttime" -lt "07" ]; then
            mute
            sleep 30
            unmute
        else
            set_lbc_mute_time
        fi
    fi
}

# Run the check
check_top_of_the_hour
