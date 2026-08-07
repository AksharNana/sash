#!/bin/sh


cp file file.tmp
cmd < file.tmp > file
rm file.tmp
