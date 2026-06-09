#!/bin/bash
# Runs the Mac-side LinkedIn newsfeed scout.
# Launched hourly by ~/Library/LaunchAgents/com.sovereignsignal.scout.plist
# Mirrors run_listener.sh: sets PYTHONPATH and uses the python@3.14 interpreter.
export PYTHONPATH=/opt/homebrew/lib/python3.14/site-packages
/opt/homebrew/opt/python@3.14/bin/python3.14 /Users/andrewboss/sovereign-signal/scripts/scout_mac.py
