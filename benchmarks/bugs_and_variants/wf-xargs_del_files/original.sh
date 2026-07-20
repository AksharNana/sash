#!/bin/sh

find ./* -newermt $(date +%Y-%m-%d -d '7 day ago') -type f -print | xargs -I '{}' mv {} ../update_error_handled
