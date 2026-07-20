#!/bin/sh



file=baktestuser.txt
for user in $(cat $file);
do
cd /usr/local/ddos
root_folder=$(mkdir "$(date +"%d-%m-%Y")")
cp baktestuser.txt $root_folder
done
