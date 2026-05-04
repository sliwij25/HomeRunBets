"""
Returns minutes until today's first MLB game.
Negative = game already started. 9999 = no games today.
Used by auto_picks.sh to decide whether to run picks.
"""
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo


def minutes_to_first_game(game_date: str | None = None) -> int:
    today = game_date or datetime.now(ZoneInfo("America/Chicago")).date().isoformat()
    try:
        resp = requests.get(
            f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={today}",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return 9999  # safe fallback: don't run picks on API error

    dates = data.get("dates", [])
    if not dates:
        return 9999
    games = dates[0].get("games", [])
    if not games:
        return 9999

    first_start = None
    for game in games:
        gt = game.get("gameDate")
        if gt:
            t = datetime.fromisoformat(gt.replace("Z", "+00:00"))
            if first_start is None or t < first_start:
                first_start = t

    if first_start is None:
        return 9999

    now = datetime.now(timezone.utc)
    return int((first_start - now).total_seconds() / 60)


if __name__ == "__main__":
    print(minutes_to_first_game())
