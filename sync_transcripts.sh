#!/bin/bash
# Drip-sync Granola MCP transcripts in small batches to avoid rate limits.
# Designed to run via launchd every 30 minutes until all meetings are synced.
# Once caught up, it exits quickly (skips already-cached transcripts).

cd "$(dirname "$0")"
export GRANOLA_MCP_ENABLE=1
/opt/homebrew/bin/python3 post_call_digest.py --sync-transcripts --batch-size 10
