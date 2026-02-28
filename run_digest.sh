#!/bin/bash
# Run the digest once per day. If the laptop was off at 8am ET,
# it catches up whenever the Mac wakes up (between 7am-9pm ET).
#
# Railway handles the 8am ET run with email sending.
# This local run generates the HTML preview only (no emails)
# so Jay can review drafts in the browser.

HOUR_ET=$(TZ="America/New_York" date +"%H")
TODAY=$(TZ="America/New_York" date +"%Y-%m-%d")
LOCK_FILE="/Users/dillandevram/Desktop/claude-projects/lightwork-digest/.last_run_date"

# Only run between 7am and 9pm ET
if [ "$HOUR_ET" -lt 7 ] || [ "$HOUR_ET" -gt 21 ]; then
    exit 0
fi

# Check if already ran today
if [ -f "$LOCK_FILE" ]; then
    LAST_RUN=$(cat "$LOCK_FILE")
    if [ "$LAST_RUN" = "$TODAY" ]; then
        exit 0
    fi
fi

# Run the digest locally (no emails, Railway handles that)
if /opt/homebrew/bin/python3 /Users/dillandevram/Desktop/claude-projects/lightwork-digest/post_call_digest.py --no-email; then
    echo "$TODAY" > "$LOCK_FILE"
fi
