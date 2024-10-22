#!/bin/bash
sleep 3
killall -9 conky && conky -c ~/.conky/Gotham/Gotham && conky -c ~/.conky/conkytidal/conkytidal
