#!/bin/sh

touch ~/.lbc
rm ~/.gbn

if [[ -f ~/.tr ]]; then rm ~/.tr & touch ~/.lbc
fi

radiotray-ng &
sleep 1
setvol () { qdbus com.github.radiotray_ng /com/github/radiotray_ng com.github.radiotray_ng.set_volume 47 ; }
unmute () { "$HOME/bin/mute_radiotray-ng" -u /usr/bin/radiotray-ng && rename_muted; }
setvol

qdbus com.github.radiotray_ng /com/github/radiotray_ng com.github.radiotray_ng.play_station Imported 'LBC UK'  ; sleep 3  ;  ~/.conky/conkyradiotray-ng/onair
unmute
