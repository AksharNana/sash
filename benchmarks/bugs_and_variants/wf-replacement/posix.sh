#!/bin/sh


cp file file.tmp
cmd < file.tmp_typo > file
rm file.tmp
