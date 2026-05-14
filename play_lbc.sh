#!/bin/bash

touch ~/.lbc
rm -f ~/.gbn  # -f avoids "file not found" errors

if [[ -f ~/.tr ]]; then 
    rm ~/.tr
    touch ~/.lbc
fi

rename_muted() { 
    [[ -f "$HOME/.conky/muted.png" ]] && mv "$HOME/.conky/muted.png" "$HOME/.conky/xmuted.png"
}

radiotray-ng &
sleep 3  # Longer wait for DBus registration

setvol() { 
    qdbus com.github.radiotray_ng /com/github/radiotray_ng com.github.radiotray_ng.set_volume 47
}

unmute() { 
    [[ -x "$HOME/bin/mute_radiotray-ng" ]] && "$HOME/bin/mute_radiotray-ng" -u /usr/bin/radiotray-ng && rename_muted
}

setvol
qdbus com.github.radiotray_ng /com/github/radiotray_ng com.github.radiotray_ng.play_station Imported 'LBC UK'
sleep 3  
unmute
