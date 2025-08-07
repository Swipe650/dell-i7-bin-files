#!/bin/bash
sleep 5
killall -9 conky && conky -c ~/.conky/Gotham/Gotham && conky -c ~/.conky/conky-rtng
