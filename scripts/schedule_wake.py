"""
Schedules a pmset wake 70 minutes before today's first MLB game.
Called by auto_picks.sh when picks haven't run yet and first game is > 110 min away.
Runs as root (LaunchDaemon) so pmset needs no sudo.
"""
import subprocess
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from scripts.get_first_game_time import minutes_to_first_game

WAKE_LEAD_MINUTES = 70


def schedule_wake() -> None:
    mins = minutes_to_first_game()
    if mins == 9999:
        return  # no games today

    wake_offset = mins - WAKE_LEAD_MINUTES
    if wake_offset <= 0:
        return  # too late to schedule a useful wake

    wake_time = datetime.now(timezone.utc) + timedelta(minutes=wake_offset)
    local_wake = wake_time.astimezone(ZoneInfo("America/Chicago"))
    pmset_fmt = local_wake.strftime("%m/%d/%y %H:%M:%S")

    result = subprocess.run(["pmset", "schedule", "wake", pmset_fmt], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[schedule_wake] ERROR: pmset failed (rc={result.returncode}): {result.stderr.strip()}")
    else:
        print(f"[schedule_wake] Wake scheduled for {pmset_fmt} CT ({wake_offset}min from now)")


if __name__ == "__main__":
    schedule_wake()
