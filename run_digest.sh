#!/bin/bash
# Only run the digest if it's currently 8am Eastern Time
HOUR_ET=$(TZ="America/New_York" date +"%H")
if [ "$HOUR_ET" = "08" ]; then
    /opt/homebrew/bin/python3 /Users/dillandevram/Desktop/claude-projects/lightwork-digest/post_call_digest.py
fi
