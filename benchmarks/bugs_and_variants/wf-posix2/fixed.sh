#!/bin/sh

find /Users/myname/Downloads/ -maxdepth 1 -name "*.zip" -print0 | xargs -0 mv -f -t /Users/myname/Downloads/zip/
