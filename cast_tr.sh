#!/bin/bash

google_home="Kitchen home"
#google_home="Bedroom mini"

touch ~/.tr

if [[ -f ~/.lbc ]]; then rm ~/.lbc & touch ~/.tr
fi

vol=$(/home/swipe/bin/cast-linux-amd64 --name "$google_home" status | awk -F 'Volume:' '{print $2}' | cut -c3-5)

/home/swipe/bin/cast-linux-amd64 --name "$google_home" media play https://talk.live.stream.broadcasting.news/stream?

/home/swipe/bin/cast-linux-amd64 --name "$google_home" volume $vol

/home/swipe/bin/cast-linux-amd64 --name "$google_home" unmute
