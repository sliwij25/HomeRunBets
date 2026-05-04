#!/bin/bash
# run-picks.sh — manual picks runner (for interactive use)
# For automated runs, launchd calls auto_picks.sh directly.
BASE=/Users/joesliwinski/AIProjects/DingersHotline
cd "$BASE"
.venv/bin/python scripts/daily_picks.py "$@"
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    TG_TOKEN=$(grep TELEGRAM_BOT_TOKEN ~/.claude/channels/telegram/.env 2>/dev/null | cut -d= -f2)
    if [ -n "$TG_TOKEN" ]; then
        ERR_LOG="$BASE/logs/daily_picks_error.log"
        SUMMARY=$(tail -10 "$ERR_LOG" 2>/dev/null || tail -10 "$BASE/logs/daily_picks.log" 2>/dev/null)
        curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
            --data-urlencode "chat_id=6624347634" \
            --data-urlencode "text=⚠️ DingersHotline picks FAILED (exit $EXIT_CODE) — $(date '+%Y-%m-%d %I:%M %p')
$SUMMARY" > /dev/null
    fi
fi
exit $EXIT_CODE
