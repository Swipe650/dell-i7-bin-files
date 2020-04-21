#!/bin/bash

sudo ufw reset
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 1714:1764/udp
sudo ufw allow 1714:1764/tcp
sudo ufw allow 22/tcp
sudo ufw enable

if [ -e /home/swipe/.conky/fwon.png ] 
then
mv /home/swipe/.conky/fwon.png /home/swipe/.conky/fwoff.png
fi
