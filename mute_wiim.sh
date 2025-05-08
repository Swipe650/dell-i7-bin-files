#!/bin/bash

vol=$(playerctl -p wiimplay volume | cut -c3-4 | bc -l)

    if [[ "$vol" -eq "00" ]]; then
      playerctl -p wiimplay volume 0.63
      
      
    elif [[ "$vol" -gt "01" ]]; then
      playerctl -p wiimplay volume 0
    fi
