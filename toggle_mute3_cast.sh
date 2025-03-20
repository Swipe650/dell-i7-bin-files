#!/bin/bash

google_home="Kitchen home"
#google_home="Bedroom mini"

get_vol=$("$HOME/bin/cast-linux-amd64" --name "$google_home" status | awk -F 'Volume:' '{print $2}' | cut -c2-5)

vol=$get_vol

mute () { "$HOME/bin/cast-linux-amd64" --name "$google_home" volume 0; }


unmute () { "$HOME/bin/cast-linux-amd64" --name "$google_home" volume $vol ; }

set_default_mute_time()
{
    mute
    
    sleep 205
     
    unmute
}

set_lbc_mute_time()
{
    mute
    
    sleep 220
     
    unmute
}




check_top_of_the_hour()
{  
# Get current time in minutes
    currenttime=$(date +%M)

# check time for Talkradio
    st=$(test -f .tr && echo "TalkRadio")
    talkradio='TalkRadio'
    case "$st" in 
    "$talkradio" )
    
    if [ "$currenttime" -eq "58" ] || [ "$currenttime" -eq "59" ]  || [ "$currenttime" -eq "00" ] || [ "$currenttime" -eq "01" ] || [ "$currenttime" -eq "02" ] || [ "$currenttime" -eq "03" ]  || [ "$currenttime" -eq "04" ] ; then
    
    mute
    
    sleep 200
     
    unmute
    
        
    elif [ "$currenttime" -eq "32" ] || [ "$currenttime" -eq "33" ] || [ "$currenttime" -eq "34" ]  || [ "$currenttime" -eq "35" ] || [ "$currenttime" -eq "36" ]  || [ "$currenttime" -eq "37" ] || [ "$currenttime" -eq "05" ] || [ "$currenttime" -eq "06" ]  || [ "$currenttime" -eq "07" ]; then
    
    mute
    
    sleep 180
     
    unmute
    
    else
    
    mute
    
    set_default_mute_time
    
    #sleep 210
     
    unmute
    
    fi
    esac
    
    # check time for lbc
    st=$(test -f .lbc && echo "LBC UK")
    lbc='LBC UK'
    case "$st" in 
    "$lbc" )

    if [ "$currenttime" -gt "00" ] && [ "$currenttime" -lt "07" ]; then
# 
    mute
    
    sleep 30
     
    unmute
    
    else
    
    set_lbc_mute_time
    
    fi
    
    esac
}

check_top_of_the_hour 
