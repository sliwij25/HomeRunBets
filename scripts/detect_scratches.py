"""
Compares today's top-20 pick_factors players against current confirmed MLB lineups.
If any player is absent from their team's confirmed batting order, they are added
to cache/scratched.json and auto_picks.sh will trigger a --use-cache re-run.

Safety rules:
  - Only scratches if the team's lineup IS confirmed (no false positives on WAITING lineups)
  - Never scratches on MLB API error (returns [] — safe fallback)
  - Uses fuzzy matching (threshold 0.82) to handle accent/abbreviation variations

Exit codes (when run as __main__):
  0 = no new scratches
  1 = scratches found and scratched.json updated
"""
import sys, json, sqlite3, requests
from pathlib import Path
from datetime import date
from difflib import SequenceMatcher

BASE = Path(__file__).parent.parent
DB_PATH = BASE / "data" / "bets.db"
SCRATCHED_FILE = BASE / "cache" / "scratched.json"
FUZZY_THRESHOLD = 0.82


def _fuzzy(a: str, b: str) -> bool:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= FUZZY_THRESHOLD


def detect_and_update_scratches(today: str | None = None) -> list[str]:
    """
    Returns list of newly scratched player names.
    Updates SCRATCHED_FILE if non-empty.
    """
    today = today or date.today().isoformat()

    # ── 1. Get today's top-20 from pick_factors ─────────────────────────────────
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT player, team FROM pick_factors WHERE bet_date=? AND rank IS NOT NULL ORDER BY rank",
            (today,),
        ).fetchall()
        conn.close()
    except Exception as e:
        print(f"[detect_scratches] DB error: {e}")
        return []

    if not rows:
        print("[detect_scratches] No picks in DB for today — skipping")
        return []

    top20 = rows[:20]
    print(f"[detect_scratches] Checking {len(top20)} players against confirmed lineups...")

    # ── 2. Fetch current MLB lineups ─────────────────────────────────────────────
    try:
        resp = requests.get(
            f"https://statsapi.mlb.com/api/v1/schedule"
            f"?sportId=1&date={today}&hydrate=lineups(person),team",
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[detect_scratches] MLB API error — skipping scratch check: {e}")
        return []

    # ── 3. Build team → {confirmed, players} map ────────────────────────────────
    team_lineups: dict[str, dict] = {}
    for game in data.get("dates", [{}])[0].get("games", []):
        lineup_data = game.get("lineups", {})
        for side_key in ("away", "home"):
            side = game.get("teams", {}).get(side_key, {})
            team_name = side.get("team", {}).get("name", "")
            if not team_name:
                continue
            confirmed_players = lineup_data.get(f"{side_key}Players", [])
            confirmed_names = {
                p.get("fullName", "") for p in confirmed_players if p.get("fullName")
            }
            team_lineups[team_name] = {
                "confirmed": bool(confirmed_names),
                "players": confirmed_names,
            }

    # ── 4. Check each player ─────────────────────────────────────────────────────
    newly_scratched = []
    for player, team in top20:
        lineup = team_lineups.get(team)
        if not lineup:
            lineup = next(
                (v for k, v in team_lineups.items()
                 if team and (team.lower() in k.lower() or k.lower() in team.lower())),
                None,
            )

        if not lineup or not lineup["confirmed"]:
            continue

        in_lineup = any(_fuzzy(player, lp) for lp in lineup["players"])
        if not in_lineup:
            print(f"[detect_scratches] SCRATCH: {player} absent from {team} confirmed lineup")
            newly_scratched.append(player)

    if not newly_scratched:
        print("[detect_scratches] No scratches detected.")
        return []

    # ── 5. Update scratched.json ─────────────────────────────────────────────────
    try:
        existing = json.loads(SCRATCHED_FILE.read_text())
        if existing.get("date") != today:
            existing = {"date": today, "players": []}
    except Exception:
        existing = {"date": today, "players": []}

    current = existing.get("players", [])
    added = [p for p in newly_scratched if p not in current]
    if not added:
        print("[detect_scratches] All scratches already in scratched.json.")
        return []

    existing["players"] = current + added
    SCRATCHED_FILE.write_text(json.dumps(existing, indent=2))
    print(f"[detect_scratches] Updated scratched.json — added: {added}")
    return added


if __name__ == "__main__":
    new_scratches = detect_and_update_scratches()
    sys.exit(1 if new_scratches else 0)
