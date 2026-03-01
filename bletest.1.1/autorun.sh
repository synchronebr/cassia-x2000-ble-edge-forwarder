#!/bin/sh
set -x

mkdir -p /opt/bletest/logs

echo "==== START $(date) ====" >> /opt/bletest/logs/app.log
whoami >> /opt/bletest/logs/app.log 2>&1
pwd >> /opt/bletest/logs/app.log 2>&1
ls -l /opt/bletest >> /opt/bletest/logs/app.log 2>&1

python3 -u /opt/bletest/app.py >> /opt/bletest/logs/app.log 2>&1