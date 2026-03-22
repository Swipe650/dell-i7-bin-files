#!/bin/bash
sleep 5
killall -9 conky 
sleep 1
conky -c ~/.conky/Gotham/Gotham && conky -c ~/.conky/conky-rtng && conky -c ~/.conky/conkytidal/conkytidal-hifi
