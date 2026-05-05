#!/bin/bash
# auto_picks.sh — orchestration for DingersHotline picks
# Called by launchd every 30 minutes. Decides whether to run fresh picks,
# check for scratches, or do nothing. All actions logged to logs/picks_YYYY-MM-DD.log.

set -e
BASE=/Users/joesliwinski/AIProjects/DingersHotline
cd "$BASE"

PYTHON="$BASE/.venv/bin/python"
TODAY=$($PYTHON -c "from datetime import date; print(date.today().isoformat())")
PICKS_FILE="$BASE/picks/picks_$TODAY.txt"
LOG_FILE="$BASE/logs/picks_$TODAY.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

send_telegram_alert() {
    local MSG="$1"
    local TG_TOKEN=$(grep TELEGRAM_BOT_TOKEN ~/.claude/channels/telegram/.env 2>/dev/null | cut -d= -f2)
    if [ -n "$TG_TOKEN" ]; then
        curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
            --data-urlencode "chat_id=6624347634" \
            --data-urlencode "text=$MSG" \
            > /dev/null
    fi
}

log "auto_picks.sh fired"

if [ ! -f "$PICKS_FILE" ]; then
    # ── No picks yet today — check timing window ──────────────────────────────
    MINS=$($PYTHON scripts/get_first_game_time.py 2>/dev/null || echo "9999")
    log "No picks yet. First game in ${MINS}min."

    if [ "$MINS" = "9999" ]; then
        log "No games today — nothing to do."
        exit 0
    fi

    if [ "$MINS" -le 110 ] && [ "$MINS" -ge -60 ]; then
        # Within window: run fresh picks
        log "Within window (${MINS}min to first game) — running fresh picks."
        $PYTHON scripts/daily_picks.py 2>&1 | tee -a "$LOG_FILE"
        EXIT_CODE=${PIPESTATUS[0]}
        if [ "$EXIT_CODE" -ne 0 ]; then
            log "ERROR: daily_picks.py failed with exit code $EXIT_CODE"
            send_telegram_alert "⚠️ DingersHotline picks FAILED (exit $EXIT_CODE) — $(date '+%Y-%m-%d %I:%M %p')
Check log: $LOG_FILE"
            exit "$EXIT_CODE"
        fi
        log "Fresh picks run complete."
    else
        # Too early — schedule a targeted wake
        log "Too early (${MINS}min to game). Scheduling pmset wake."
        $PYTHON scripts/schedule_wake.py 2>&1 | tee -a "$LOG_FILE"
    fi

else
    # ── Picks exist — check for scratches ────────────────────────────────────
    log "Picks file exists. Checking for scratches..."
    $PYTHON scripts/detect_scratches.py 2>&1 | tee -a "$LOG_FILE"
    SCRATCH_EXIT=$?

    if [ "$SCRATCH_EXIT" -eq 1 ]; then
        log "Scratches detected — running --use-cache re-run."
        $PYTHON scripts/daily_picks.py --use-cache 2>&1 | tee -a "$LOG_FILE"
        RERUN_EXIT=${PIPESTATUS[0]}
        if [ "$RERUN_EXIT" -ne 0 ]; then
            log "ERROR: --use-cache re-run failed with exit code $RERUN_EXIT"
            send_telegram_alert "⚠️ DingersHotline scratch re-run FAILED (exit $RERUN_EXIT) — $(date '+%Y-%m-%d %I:%M %p')"
            exit "$RERUN_EXIT"
        fi
        log "Scratch re-run complete."
    else
        log "No scratches found — nothing to do."
    fi
fi

log "auto_picks.sh done."
exit 0
