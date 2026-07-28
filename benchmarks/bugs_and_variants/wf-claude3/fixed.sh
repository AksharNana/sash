#!/bin/sh

mkdir -p some/dir
touch some/dir/newfile.cpp
echo 'some code' > some/dir/newfile.cpp
cat some/dir/newfile.cpp | g++ -x c++ -
rm some/dir/newfile.cpp && rmdir some/dir
