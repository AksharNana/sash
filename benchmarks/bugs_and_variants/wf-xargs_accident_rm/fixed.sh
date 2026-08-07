#!/bin/sh

find /home/billy/Downloads -type f -iname "*.mkv" -o -iname "*.mp4" -o -iname "*.avi" | xargs -I list mv list /home/billy/Videos/
