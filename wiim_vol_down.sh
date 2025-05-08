#!/bin/bash

vol=$(playerctl -p wiimplay volume | cut -c1-4)

incl=-0.03

voldown=$(echo $vol + $incl | bc)

/home/swipe/bin/set_vol_wiim "0$voldown"
