#!/bin/bash
# Railway entrypoint: restore tokens, seed DBs to volume, then run digest

set -e

# Write Granola MCP token from base64 env var if present
if [ -n "$GRANOLA_TOKEN_B64" ]; then
    TOKEN_DIR="$HOME/.config/lightwork-digest"
    mkdir -p "$TOKEN_DIR"
    echo "$GRANOLA_TOKEN_B64" | base64 -d > "$TOKEN_DIR/granola_mcp_token.json"
    chmod 600 "$TOKEN_DIR/granola_mcp_token.json"
    echo "Granola MCP token restored from env var"
fi

# Seed databases to persistent volume on first run
if [ -n "$DB_DIR" ] && [ -d "/app/seed" ]; then
    mkdir -p "$DB_DIR"
    for db in transcripts.db drafts.db; do
        if [ ! -f "$DB_DIR/$db" ] && [ -f "/app/seed/$db" ]; then
            cp "/app/seed/$db" "$DB_DIR/$db"
            echo "Seeded $db to $DB_DIR"
        fi
    done
fi

python3 -u post_call_digest.py --no-open
