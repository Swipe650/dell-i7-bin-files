#!/bin/sh
ps -ef | grep conkyrt | grep -v grep | awk '{print $2}' | xargs kill &
ps -ef | grep radiotray | grep -v grep | awk '{print $2}' | xargs kill &
