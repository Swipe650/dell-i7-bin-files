#!/bin/bash

google_home="Kitchen"
#google_home="Bedroom mini"

get_vol=$("$HOME/bin/cast-linux-amd64" --name "$google_home" status | awk -F 'Volume:' '{print $2}' | cut -c2-5)

vol=$get_vol

#Mute volume
"$HOME/bin/cast-linux-amd64" --name "$google_home" volume 0

sleep 30

#echo "$vol"

#Set volume to current volume level
"$HOME/bin/cast-linux-amd64" --name "$google_home" volume $vol 
