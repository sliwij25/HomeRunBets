#!/usr/bin/env python3
"""
Audit what happened for a given date.
Usage:
  python scripts/check_picks.py           # today
  python scripts/check_picks.py 2026-05-03  # specific date
"""
import sys, sqlite3, json
from pathlib import Path
from datetime import date

# Find the actual project root (DingersHotline/) by looking for data/bets.db
def find_project_root():
    p = Path(__file__).parent.parent
    while p != p.parent:  # Until we reach root
        if (p / "data" / "bets.db").exists():
            return p
        p = p.parent
    return Path(__file__).parent.parent  # Fallback

BASE = find_project_root()
TARGET_DATE = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()


def hr_symbol(homered):
    if homered == 1:
        return "✓ HR"
    if homered == 0:
        return "  --"
    return "  ? "


print(f"\n{'='*60}")
print(f"  PICKS AUDIT — {TARGET_DATE}")
print(f"{'='*60}")

# ── Picks file ────────────────────────────────────────────────────────────────
picks_file = BASE / "picks" / f"picks_{TARGET_DATE}.txt"
if picks_file.exists():
    print(f"\n✓ Picks file: picks_{TARGET_DATE}.txt")
else:
    print(f"\n✗ No picks file for {TARGET_DATE}")

# ── pick_factors snapshot ─────────────────────────────────────────────────────
try:
    conn = sqlite3.connect(BASE / "data" / "bets.db")
    rows = conn.execute(
        """SELECT rank, player, score, lineup_confirmed, homered
           FROM pick_factors WHERE bet_date=? ORDER BY rank""",
        (TARGET_DATE,),
    ).fetchall()
    conn.close()
except Exception as e:
    print(f"\n✗ DB error: {e}")
    rows = []

if rows:
    hits = sum(1 for _, _, _, _, h in rows if h == 1)
    unlabeled = sum(1 for _, _, _, _, h in rows if h is None)
    print(f"\n✓ pick_factors: {len(rows)} players | {hits} HR | {unlabeled} unlabeled\n")
    print(f"  {'#':<4} {'Player':<30} {'Score':<7} {'Confirmed':<10} Result")
    print(f"  {'-'*58}")
    for rank, player, score, confirmed, homered in rows:
        conf = "YES" if confirmed else "NO "
        score_str = f"{score:.1f}" if score is not None else "  N/A"
        print(f"  #{rank:<3} {player:<30} {score_str:<7} {conf:<10} {hr_symbol(homered)}")
else:
    print(f"\n✗ No pick_factors rows for {TARGET_DATE}")

# ── Scratch history ───────────────────────────────────────────────────────────
try:
    scratched_data = json.loads((BASE / "cache" / "scratched.json").read_text())
    if scratched_data.get("date") == TARGET_DATE and scratched_data.get("players"):
        print(f"\n⚠ Scratched players recorded: {', '.join(scratched_data['players'])}")
    else:
        print(f"\n✓ No scratches recorded for {TARGET_DATE}")
except Exception:
    print(f"\n✓ No scratch data for {TARGET_DATE}")

# ── Run log ───────────────────────────────────────────────────────────────────
log_file = BASE / "logs" / f"picks_{TARGET_DATE}.log"
if log_file.exists():
    print(f"\n--- Run Log ---")
    for line in log_file.read_text().splitlines():
        if any(kw in line for kw in ["fired", "picks", "Scratch", "ERROR", "window", "complete", "game"]):
            print(f"  {line}")
else:
    print(f"\n✗ No log file: picks_{TARGET_DATE}.log")

print()
