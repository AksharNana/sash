#!/bin/sh

# https://community.unix.com/t/move-file-in-current-date-folder-through-shell-script/384145

file=baktestuser.txt
for user in $(cat $file);
do
cd /usr/local/ddos
root_folder=$(date +"%d-%m-%Y")
mkdir -p $root_folder
cp baktestuser.txt $root_folder
done
