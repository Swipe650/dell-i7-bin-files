#!/bin/bash
 
mute () { "$HOME/bin/mute_radiotray-ng" -m /usr/bin/radiotray-ng && rename_xmuted; }
unmute () { "$HOME/bin/mute_radiotray-ng" -u /usr/bin/radiotray-ng && rename_muted; }
rename_xmuted () { if [ -f "$HOME/.conky/xmuted.png" ]; then { mv "$HOME/.conky/xmuted.png" "$HOME/.conky/muted.png"; } fi }
rename_muted () { if [ -f "$HOME/.conky/muted.png" ]; then { mv "$HOME/.conky/muted.png" "$HOME/.conky/xmuted.png"; } fi }
talkradio () { qdbus com.github.radiotray_ng /com/github/radiotray_ng com.github.radiotray_ng.play_station Imported 'TalkRadio' ; }
setvol () { qdbus com.github.radiotray_ng /com/github/radiotray_ng com.github.radiotray_ng.set_volume 34 ; }

touch ~/.tr

rm ~/.gbn

if [[ -f ~/.lbc ]]; then rm ~/.lbc & touch ~/.tr
fi

radiotray-ng &
sleep 1

setvol
talkradio
sleep 3
unmute
rename_muted
