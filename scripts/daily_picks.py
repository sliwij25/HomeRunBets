"""
daily_picks.py
Run this every morning in Spyder to get today's HR picks and bet slips.

COST OPTIMIZATION:
  For testing/development, use --use-cache flag to skip data fetching:
    python daily_picks.py --use-cache
  
  This loads cached context from the latest debug_context_YYYY-MM-DD.json,
  avoiding ~100 API calls per run (Odds API, MLB API, Statcast, etc.).

Usage:
  python daily_picks.py              # fetch all data fresh (full output)
  python daily_picks.py --brief      # fetch fresh, print top 7 only (saves tokens)
  python daily_picks.py --use-cache  # reuse cached data from today
"""

import json
import os
import sys
import argparse
from datetime import date
from pathlib import Path
from dotenv import load_dotenv

# ── Setup ──────────────────────────────────────────────────────────────────────

# Make sure we're in the right directory so agent imports work
os.chdir(str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "ml"))
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

# Load API keys from api/.env
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "api", ".env"))

TODAY = date.today().isoformat()

# Load today's scratched players from cache/scratched.json (auto-expires on date change)
_scratched_file = Path(__file__).parent.parent / "cache" / "scratched.json"
SCRATCHED: set = set()
try:
    import json as _json
    _s = _json.loads(_scratched_file.read_text())
    if _s.get("date") == TODAY:
        SCRATCHED = set(_s.get("players", []))
        if SCRATCHED:
            print(f"  [Scratched] Excluding: {', '.join(sorted(SCRATCHED))}")
except Exception:
    pass

# Parse command-line args
parser = argparse.ArgumentParser()
parser.add_argument("--use-cache", action="store_true",
                    help="Load cached context instead of fetching fresh data")
parser.add_argument("--no-lock", action="store_true",
                    help="Bypass the morning top-20 player lock; use with --use-cache after scoring changes")
parser.add_argument("--brief", action="store_true",
                    help="Print only top 7 picks + summary; full list still saved to .txt and HTML")
parser.add_argument("--no-notify", action="store_true",
                    help="Skip Telegram and iMessage notifications")
args = parser.parse_args()

print("=" * 60)
print(f"  DINGERS HOTLINE — {TODAY}")
if args.use_cache:
    print("  (using cached data — development mode)")
print("=" * 60)

# ── Import Homer ───────────────────────────────────────────────────────────────

from agents import Homer
from agents.predictor import fetch_odds_comparison
from agents.bet_tracker import save_pick_factors, backfill_pick_odds, model_performance_report, model_pnl_report, group_hit_rate, group_pnl, star_bucket_hit_rate, star_bucket_pnl, yesterday_results_snapshot, trending_picks
from generate_html import generate_picks_html, generate_leaderboard_html, generate_player_data_json

# ── Auto-maintenance (runs every morning before picks) ─────────────────────────
# Labels yesterday's pick_factors with actual HR results and refreshes 2026 training data.
# ML retraining happens in record_results.py (night run) — NOT here — so weights
# are stable for the entire day and the morning top-20 is never blown up by a retrain.

def _auto_maintain():
    import sqlite3 as _sqlite3
    from datetime import date as _date, timedelta

    yesterday = (_date.today() - timedelta(days=1)).isoformat()

    # 1. Label yesterday's MLB results ─────────────────────────────────────────
    print("  [Auto] Labeling yesterday's HR results...", end=" ", flush=True)
    try:
        from fetch_actual_results import fetch_homers_for_date, update_pick_factors
        import io as _io, sys as _sys
        _old, _sys.stdout = _sys.stdout, _io.StringIO()
        try:
            homers, homer_teams, active_players = fetch_homers_for_date(yesterday)
            # homers=None → off day / all games pending (skip labeling)
            # homers={} → games completed, nobody homered (still label everyone as 0)
            if homers is not None:
                update_pick_factors(yesterday, homers, homer_teams, active_players, dry_run=False)
        finally:
            _sys.stdout = _old
        if homers is None:
            print("no completed games")
        else:
            print(f"{len(homers)} players homered")
    except Exception as e:
        print(f"skipped ({e})")

    # 2. Refresh 2026 training data ────────────────────────────────────────────
    print("  [Auto] Refreshing 2026 Statcast + HR data...", end=" ", flush=True)
    try:
        from build_historical_dataset import (
            fetch_statcast_season, fetch_hr_events_season,
            write_season_to_db, CURRENT_YEAR
        )
        import io as _io, sys as _sys
        _old, _sys.stdout = _sys.stdout, _io.StringIO()
        try:
            bs   = fetch_statcast_season(CURRENT_YEAR)
            hrev = fetch_hr_events_season(CURRENT_YEAR)
            n, _ = write_season_to_db(CURRENT_YEAR, bs, hrev)
        finally:
            _sys.stdout = _old
        print(f"{n:,} rows in DB" if bs else "no data yet (early season?)")
    except Exception as e:
        print(f"skipped ({e})")

    # Show current ML weights status (retrain now happens in record_results.py at night)
    weights_path = Path(__file__).parent.parent / "ml_weights.json"
    if weights_path.exists():
        try:
            with open(weights_path) as f:
                w = json.load(f)
            print(f"  [Auto] ML weights  "
                  f"(trained {w.get('trained_on','?')}, AUC={w.get('cv_auc_mean',0):.3f}, "
                  f"v{w.get('algo_version','?')})")
        except Exception:
            pass

    print()

if not args.use_cache:
    _auto_maintain()

# ── Yesterday's results snapshot ───────────────────────────────────────────────

def _print_yesterday_snapshot():
    from datetime import timedelta
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    snap = yesterday_results_snapshot(yesterday)
    if not snap["picks"]:
        print(f"  [Yesterday] No picks on record for {yesterday}.")
        print()
        return

    star_map = {5: "★★★★★", 4: "★★★★☆", 3: "★★★☆☆", 2: "★★☆☆☆", 1: "★☆☆☆☆", None: "—"}
    print(f"  YESTERDAY'S RESULTS — {yesterday}")
    print("  " + "─" * 58)
    for p in snap["picks"]:
        if p["homered"] is None:
            status = "  ?"
        elif p["homered"]:
            status = " HR"
        else:
            status = "   "
        stars_str = star_map.get(p["stars"], "—")
        pnl_str   = f"${p['pnl']:+.2f}" if p["pnl"] is not None else "  —  "
        print(f"  #{p['rank']:>2}  {status}  {stars_str}  {p['player']:<28}  {p['odds']:>5}  {pnl_str}")

    if snap["labeled"]:
        hr_line      = f"{snap['hr_count']} HR{'s' if snap['hr_count'] != 1 else ''}"
        pnl_line     = f"${snap['day_pnl']:+.2f}"
        countable    = sum(1 for p in snap["picks"] if p["homered"] is not None)
        n_scratched  = len(snap["picks"]) - countable
        scratch_note = f" ({n_scratched} scratched)" if n_scratched else ""
        print("  " + "─" * 58)
        print(f"  {hr_line} out of {countable} picks{scratch_note}   Day P&L: {pnl_line}")
    else:
        print("  " + "─" * 58)
        print("  (results not yet labeled — run after games complete)")
    print()

_print_yesterday_snapshot()

# ── Get picks (narrative) ──────────────────────────────────────────────────────

if args.use_cache:
    # Load cached context
    cache_file = sorted(Path(__file__).parent.parent.glob(f"cache/debug_context_{TODAY}.json"), reverse=True)
    if not cache_file:
        print("ERROR: No cache file found for today.")
        print(f"Run without --use-cache first, or run cache_data.py manually.")
        sys.exit(1)

    print(f"Loading cached context from {cache_file[0].name}...\n")
    homer = Homer()
    with open(cache_file[0]) as f:
        homer._context = json.load(f)

    # Restore ML weights snapshot from the morning run so scores are identical
    _snap = homer._context.pop("_ml_weights_snapshot", None)
    if _snap:
        Homer._ml_weights = _snap
        Homer._ml_weights_loaded = True
        print(f"  [Lock] Using morning ML weights (AUC={_snap.get('cv_auc_mean',0):.3f}) — scores will not drift.")

    # Lock the top-20 player set from today's existing picks file.
    # Cache re-runs may only REMOVE scratched players — they cannot add new players
    # or drop existing ones due to ML weight changes or lineup confirmation shifts.
    # Pass --no-lock to bypass this when you've intentionally changed scoring logic.
    import re as _re
    _picks_txt = Path(__file__).parent.parent / "picks" / f"picks_{TODAY}.txt"
    if _picks_txt.exists() and not args.no_lock:
        _locked: list[str] = []
        for _line in _picks_txt.read_text(encoding="utf-8").splitlines():
            _m = _re.match(r"^#\d+\s+(.+?)\s+[★☆]", _line.strip())
            if _m:
                # Strip inline status badges like [WAITING] or [LINEUP PENDING]
                _pname = _re.sub(r"\s*\[[^\]]+\]\s*", " ", _m.group(1)).strip()
                _locked.append(_pname)
        if _locked:
            print(f"  [Lock] Preserving today's top-20 player set ({len(_locked)} players locked).")
            _all_sig = homer._context.get("player_signals", {})
            _locked_sig = {}
            for _name in _locked:
                if any(s.lower() in _name.lower() for s in SCRATCHED):
                    print(f"  [Lock] Removing scratched: {_name}")
                    continue
                # Find the matching key in player_signals (keys are "Name|Team")
                _key = next((k for k in _all_sig if k.split("|")[0].lower() == _name.lower()), None)
                if _key:
                    _locked_sig[_key] = _all_sig[_key]
                else:
                    print(f"  [Lock] Warning: '{_name}' not found in cache signals — skipped.")

            # Backfill: if scratches dropped us below 20, pull in next-best players
            _needed = 20 - len(_locked_sig)
            if _needed > 0:
                _locked_names_lower = {k.split("|")[0].lower() for k in _locked_sig}
                _scratch_lower = {s.lower() for s in SCRATCHED}
                _backfill_pool = homer._rank_picks_python(_all_sig, top_n=40, verbose=False, scratched=SCRATCHED)
                _added = 0
                for _bp in _backfill_pool:
                    if _added >= _needed:
                        break
                    _bname = _bp["player"]
                    if _bname.lower() in _locked_names_lower:
                        continue
                    if any(s in _bname.lower() for s in _scratch_lower):
                        continue
                    _bkey = next((k for k in _all_sig if k.split("|")[0].lower() == _bname.lower()), None)
                    if _bkey:
                        _locked_sig[_bkey] = _all_sig[_bkey]
                        print(f"  [Lock] Backfill #{len(_locked_sig)}: {_bname} (score={_bp.get('score', 0):.1f})")
                        _locked_names_lower.add(_bname.lower())
                        _added += 1

            homer._context["player_signals"] = _locked_sig
else:
    print("Fetching picks — this takes about 30–60 seconds...\n")
    homer = Homer()

narrative = homer.run(
    f"Today is {TODAY}. Give me the top 20 HR picks for today with confidence tiers. "
    "Evaluate ALL batters in the confirmed lineups using BallparkPal matchup grades, "
    "park factors, Statcast barrel rate, hard hit %, recent HR form, and our historical record. "
    "For each pick include: player, matchup, batting position, key stats, and reasoning.",
    scratched=SCRATCHED,
)

# ── Compute Best Bets (top-7 by EV) ──────────────────────────────────────────
# Rank top-20 model picks by expected value. Three tiers:
#   Tier 0: ev_10 ≥ $0.50 AND Pinnacle odds present → meaningful Pinnacle-anchored EV
#   Tier 1: ev_10 ≥ $0.50 but no Pinnacle (consensus) → meaningful estimated EV (~)
#   Tier 2: ev_10 < $0.50 or no odds → model score proxy
# Minimum threshold prevents near-zero EV picks ($+0.02) from jumping
# over high-model-score players who have no odds yet.
_MIN_EV = 0.50

def _ev_sort_key(p: dict) -> tuple:
    sig = p.get("signals", {}) or {}
    ev  = sig.get("ev_10")
    if ev is not None and ev >= _MIN_EV:
        tier = 0 if sig.get("pinnacle_odds") else 1
        return (tier, -ev)
    return (2, -(p.get("score") or 0))

_sigs_for_bb = homer._context.get("player_signals", {})
_ranked_for_bb = homer._rank_picks_python(_sigs_for_bb, top_n=20, verbose=False, scratched=SCRATCHED)
_best_bets: list[dict] = sorted(_ranked_for_bb, key=_ev_sort_key)[:5]

def _fmt_best_bets_terminal(best_bets: list[dict]) -> str:
    # Stars (★☆) are "wide" Unicode chars — each takes 2 display columns.
    # Build each row without stars first, measure, pad, then append stars at end.
    width = 58
    lines = [
        "╔" + "═" * width + "╗",
        "║  BEST BETS — Top 7 by Expected Value" + " " * (width - 37) + "║",
        "╠" + "═" * width + "╣",
    ]
    star_map = {5: "★★★★★", 4: "★★★★☆", 3: "★★★☆☆", 2: "★★☆☆☆", 1: "★☆☆☆☆", 0: "☆☆☆☆☆"}
    for i, p in enumerate(best_bets, 1):
        sig  = p.get("signals", {}) or {}
        ev   = sig.get("ev_10")
        pin  = sig.get("pinnacle_odds")
        name = p.get("player", "Unknown")[:20]
        star_n    = (p.get("stars") or "").count("★")
        stars_str = star_map.get(star_n, "—    ")
        if ev is not None:
            prefix = "" if pin else "~"
            ev_str = f"{prefix}${ev:+.2f}"
        else:
            ev_str = "~est"
        rank_label = f"#{p.get('rank') or i}"
        # Build the ASCII portion (no stars) and pad to fill the box width
        ascii_part = f"  #{i}  {name:<20}  EV {ev_str:<8}  {rank_label}"
        # Stars display as 2 cols each — append after padding
        pad = max(0, width - len(ascii_part) - 5 - 2)  # 5 star chars×2cols - 2 border spaces
        lines.append("║" + ascii_part + " " * pad + "  " + stars_str + "║")
    lines.append("╚" + "═" * width + "╝")
    return "\n".join(lines)

# Auto-save cache on fresh run (not needed when using --use-cache)
if not args.use_cache:
    cache_file = Path(__file__).parent.parent / "cache" / f"debug_context_{TODAY}.json"
    try:
        # Snapshot ML weights into the cache so re-runs use identical weights
        _ctx_to_save = dict(homer._context)
        _ml_w = Homer._load_ml_weights()
        if _ml_w:
            _ctx_to_save["_ml_weights_snapshot"] = _ml_w
        with open(cache_file, "w") as f:
            json.dump(_ctx_to_save, f)
        print(f"\n  [Cached context to {cache_file.name} for testing]")
    except Exception as e:
        pass  # silent fail, not critical


print("\n" + _fmt_best_bets_terminal(_best_bets))

print("\n" + "=" * 60)
print("  TODAY'S PICKS")
print("=" * 60)
if args.brief:
    # Print only up to pick #7 — find the 8th separator and cut there
    sep = "─" * 62
    parts = narrative.split(sep)
    brief_text = sep.join(parts[:8]) if len(parts) > 8 else narrative  # 1 header + 7 picks = 8 parts
    print(brief_text)
    print(f"\n  ... picks #8–20 saved to picks_{TODAY}.txt")
else:
    print(narrative)

# ── Export clean .txt file (shareable picks list) ──────────────────────────────
# Skip on cache re-runs when the file already exists — the morning run owns the txt.
# Cache re-runs only update HTML (lineup badges, odds) without altering the record.
try:
    txt_path = Path(__file__).parent.parent / "picks" / f"picks_{TODAY}.txt"
    if args.use_cache and txt_path.exists():
        print(f"\n  [Export] Skipping txt rewrite on cache re-run (morning file preserved).")
    else:
        from datetime import timedelta as _td
        _yesterday = (date.today() - _td(days=1)).isoformat()
        _snap = yesterday_results_snapshot(_yesterday)

        with open(txt_path, "w", encoding="utf-8") as _f:
            # Prepend yesterday's snapshot so every picks file records prior-day history
            if _snap["picks"]:
                _star_map = {5: "★★★★★", 4: "★★★★☆", 3: "★★★☆☆", 2: "★★☆☆☆", 1: "★☆☆☆☆", None: "—"}
                _f.write(f"Yesterday's Results — {_yesterday}\n")
                _f.write("─" * 62 + "\n")
                for _p in _snap["picks"]:
                    _hr = " HR" if _p["homered"] else ("  ?" if _p["homered"] is None else "   ")
                    _stars = _star_map.get(_p["stars"], "—")
                    _pnl   = f"${_p['pnl']:+.2f}" if _p["pnl"] is not None else "  —  "
                    _f.write(f"#{_p['rank']:>2}{_hr}  {_stars}  {_p['player']:<28}  {_p['odds']:>5}  {_pnl}\n")
                if _snap["labeled"]:
                    _countable   = sum(1 for _pp in _snap["picks"] if _pp["homered"] is not None)
                    _n_scratched = len(_snap["picks"]) - _countable
                    _scratch_txt = f" ({_n_scratched} scratched)" if _n_scratched else ""
                    _f.write(f"{'─'*62}\n{_snap['hr_count']} HR(s) / {_countable} picks{_scratch_txt}   Day P&L: ${_snap['day_pnl']:+.2f}\n")
                _f.write("\n")

            _f.write(f"Dingers Hotline — {TODAY}\n")
            _f.write("=" * 62 + "\n\n")
            _f.write(_fmt_best_bets_terminal(_best_bets))
            _f.write("\n\n")
            _f.write(narrative)
            _f.write("\n")
        print(f"\n  [Export] Picks saved to {txt_path.name}")
except Exception as e:
    print(f"  [Export] Could not save .txt: {e}")

# ── Generate bet slips ─────────────────────────────────────────────────────────

print("\n" + "=" * 60)
print("  BET SLIPS — fill in odds + potential_payout from your platform")
print("=" * 60)

picks = homer.get_picks_json(top_n=20, scratched=SCRATCHED)
_all_ranked: list[dict] = []  # filled below; used for HTML generation

# Re-run if a notification was already sent today (survives failed first runs)
_notify_flag = Path(__file__).parent.parent / "cache" / f"notified_{TODAY}.flag"
_is_rerun = _notify_flag.exists()

if not picks:
    print("\nCould not generate structured picks.")
else:
    # Read algo_version from ml_weights.json so it auto-tracks model generations
    _algo_version = "3.1"
    try:
        with open(Path(__file__).parent.parent / "ml_weights.json") as _avf:
            _algo_version = json.load(_avf).get("algo_version", "3.1")
    except Exception:
        pass

    # Save ALL ranked players (not just top 8) for unbiased ML training data.
    # The model needs to see who didn't homer just as much as who did.
    player_signals = homer._context.get("player_signals", {})
    all_ranked = homer._rank_picks_python(player_signals, top_n=20, verbose=not args.brief, scratched=SCRATCHED)
    _all_ranked = all_ranked
    _best_bet_names = {p.get("player") for p in _best_bets}
    saved = 0
    for rank_i, p in enumerate(all_ranked[:20], 1):  # hard cap at exactly 20
        if p.get("signals"):
            try:
                save_pick_factors(TODAY, p["player"], p["signals"],
                                  confidence=p.get("confidence"),
                                  algo_version=_algo_version,
                                  score=p.get("score"),
                                  rank=rank_i,
                                  stars=p.get("stars", "").count("★") or None,
                                  game_pk=p.get("signals", {}).get("game_pk"),
                                  is_best_bet=1 if p.get("player") in _best_bet_names else 0)
                saved += 1
            except Exception:
                pass
    print(f"\n  [ML Training] Saved signal snapshots for {saved} players (top 20 ranked)")


# ── Odds comparison / value finder ────────────────────────────────────────────

print("\n" + "=" * 60)
print("  ODDS COMPARISON — sharp lines + value finder")
print("=" * 60)

try:
    raw_cmp = fetch_odds_comparison()
    cmp_data = json.loads(raw_cmp)

    if cmp_data.get("status") == "success":
        comparisons = cmp_data.get("comparisons", [])
        if comparisons:
            saved_odds = backfill_pick_odds(TODAY, comparisons)
            if saved_odds:
                print(f"  [Odds] Saved best_odds for {saved_odds} picks")
        value_picks = [c for c in comparisons if c.get("value_flag") == "VALUE"]
        if comparisons:
            if args.brief:
                # Brief: just VALUE flags + count
                if value_picks:
                    print(f"  VALUE picks ({len(value_picks)}): " +
                          ", ".join(f"{v['player']} {v['best_odds']}" for v in value_picks[:5]))
                else:
                    print("  No VALUE flags — lines tight. Run odds_check.py for full table.")
            else:
                print("  Pinnacle = sharpest benchmark (no US retail markup).")
                print("  Compare your Novig / ProphetX odds to Pinnacle and Best Odds.")
                print("  If your platform beats Best Odds -> you have extra edge.\n")
                print(f"  {'Player':<26} {'Pinnacle':<11} {'Best Odds':<11} "
                      f"{'Best Book':<18} {'Consensus%':<12} {'Edge':<8} {'Flag'}")
                print("  " + "-" * 96)
                for c in comparisons[:25]:
                    flag     = "VALUE" if c.get("value_flag") == "VALUE" else ""
                    edge     = c.get("value_edge", 0)
                    edge_str = f"+{edge:.1f}pp" if edge >= 0 else f"{edge:.1f}pp"
                    print(f"  {c['player']:<26} {c['pinnacle']:<11} {c['best_odds']:<11} "
                          f"{c['best_book']:<18} {c['consensus_prob']:<12} "
                          f"{edge_str:<8} {flag}")
                print()
                if value_picks:
                    print("  ── VALUE picks — full book breakdown ──────────────────")
                    for vp in value_picks[:8]:
                        print(f"\n  {vp['player']}  ({vp['matchup']})")
                        for book, odds in vp["all_books"].items():
                            marker   = " <- BEST"       if book == vp["best_book"] else ""
                            pin_mark = " <- SHARP LINE" if "Pinnacle" in book      else ""
                            print(f"    {book:<22} {odds}{marker}{pin_mark}")
                        print(f"  >> Novig / ProphetX: check app — beat Pinnacle {vp['pinnacle']} = value")
                else:
                    print("  No VALUE flags today — lines are tight across books.")
                    print("  Compare your Novig/ProphetX to the Pinnacle column above.")
        else:
            print("  No prop odds data yet — books post HR props ~2-4h before first pitch.")
    else:
        print(f"  {cmp_data.get('message', 'Could not fetch odds comparison.')}")
except Exception as e:
    print(f"  Odds comparison unavailable: {e}")


# ── Prompt for missing best_odds on today's top-20 ────────────────────────────

if not args.brief and sys.stdin.isatty():
    try:
        import sqlite3 as _sq2
        _db2 = _sq2.connect(str(Path(__file__).parent.parent / "data" / "bets.db"))
        _missing = _db2.execute("""
            SELECT player, rank FROM (
                SELECT player, rank,
                       ROW_NUMBER() OVER (PARTITION BY bet_date ORDER BY rank, player) AS rn
                FROM pick_factors
                WHERE bet_date = ? AND best_odds IS NULL AND rank IS NOT NULL
                  AND algo_version NOT LIKE 'hist_%'
            ) WHERE rn <= 20
            ORDER BY rank
        """, (TODAY,)).fetchall()
        if _missing:
            print(f"\n  {len(_missing)} top-20 pick(s) missing odds — enter now or press Enter to skip:")
            for _mp, _mr in _missing:
                _inp = input(f"    #{_mr} {_mp} best odds (e.g. +350): ").strip()
                if _inp:
                    _db2.execute(
                        "UPDATE pick_factors SET best_odds=? WHERE bet_date=? AND player=?",
                        (_inp, TODAY, _mp)
                    )
                    _db2.commit()
                    print(f"    Saved {_mp} → {_inp}")
        _db2.close()
    except Exception as _me:
        pass


# ── Trending picks alert ───────────────────────────────────────────────────────
try:
    _trending = trending_picks(min_streak=3, top_n_threshold=10)
    if _trending:
        print("\n" + "=" * 60)
        print("  TRENDING — Top-10 for 3+ consecutive days")
        print("=" * 60)
        for _t in _trending:
            _rank_str = "/".join(f"#{r}" for r in _t["ranks"])
            print(f"  {_t['player']:<28} {_t['streak']}d streak  [{_rank_str}]  ({_t['dates'][0]} – {_t['dates'][-1]})")
        print("=" * 60)
except Exception:
    pass

# ── Model performance dashboard ────────────────────────────────────────────────

print()
try:
    if args.brief:
        # Brief: one-line model status instead of full dashboard
        import re as _re
        _report = model_performance_report()
        _auc_line = next((l for l in _report.splitlines() if "AUC" in l), "")
        _top3_line = next((l for l in _report.splitlines() if "Top 3" in l), "")
        if _auc_line: print(" ", _auc_line.strip())
        if _top3_line: print(" ", _top3_line.strip())
    else:
        print(model_performance_report())
except Exception as e:
    print(f"  [Model dashboard unavailable: {e}]")

try:
    pnl_data = json.loads(model_pnl_report())
    summary = pnl_data.get("model_pnl_summary", {})
    if summary and summary.get("days_tracked", 0) > 0:
        if args.brief:
            print(f"  Model P&L: {summary['cumulative_pnl']}  ROI: {summary['roi']}  "
                  f"({summary['win_pct']} hit rate, {summary['days_tracked']}d)")
        else:
            print("\n" + "=" * 60)
            print("  MODEL P&L  (fictitious — $10 on every top-20 pick)")
            print("=" * 60)
            print(f"  Days tracked:   {summary['days_tracked']}")
            print(f"  Total picks:    {summary['total_picks_with_odds']}  ({summary['win_pct']} hit rate)")
            print(f"  Total wagered:  {summary['total_wagered']}")
            print(f"  Cumulative P&L: {summary['cumulative_pnl']}")
            print(f"  ROI:            {summary['roi']}")
            daily = pnl_data.get("daily", [])
            if daily:
                print(f"\n  {'Date':<12} {'Picks':>6} {'Wins':>5} {'Day P&L':>10} {'Cumulative':>12}")
                print("  " + "-" * 48)
                for d in daily[-10:]:
                    print(f"  {d['date']:<12} {d['picks_with_odds']:>6} {d['wins']:>5} "
                          f"{d['day_pnl']:>10} {d['cumulative_pnl']:>12}")
except Exception:
    pass

# ── Generate HTML for GitHub Pages ────────────────────────────────────────────

try:
        import sqlite3 as _sq, json as _js
        # AUC + ML influence from weights file
        _wp = Path(__file__).parent.parent / "ml_weights.json"
        _auc, _ml_influence = 0.0, 0.0
        if _wp.exists():
            with open(_wp) as _wf:
                _wj = _js.load(_wf)
            _auc = _wj.get("cv_auc_mean", 0.0)
            _ml_influence = min(0.7, max(0.0, (_auc - 0.5) * 2.5))

        # Model fictitious P&L (pick_factors with best_odds — NOT personal bets)
        _model_yesterday_pnl, _model_cumulative_pnl, _model_days_tracked = None, None, None
        _net_pnl, _roi, _record, _win_rate, _streak = 0.0, 0.0, "—", "—", None
        try:
            _pnl_js = _js.loads(model_pnl_report())
            _pnl_summary = _pnl_js.get("model_pnl_summary", {})
            _pnl_daily   = _pnl_js.get("daily", [])
            if _pnl_summary.get("days_tracked", 0) > 0:
                _cum_str = _pnl_summary.get("cumulative_pnl", "$0.00")
                _model_cumulative_pnl = float(_cum_str.replace("$", "").replace("+", ""))
                _model_days_tracked = _pnl_summary.get("days_tracked")
                _roi = float(_pnl_summary.get("roi", "0%").replace("%", "").replace("+", ""))
                _win_rate = _pnl_summary.get("win_pct", "—")
            if _pnl_daily:
                _last_day = _pnl_daily[-1]
                _day_str  = _last_day.get("day_pnl", "$0.00")
                _model_yesterday_pnl = float(_day_str.replace("$", "").replace("+", ""))
                # Streak: consecutive profitable or losing days (most recent first)
                _streak_days = [float(d["day_pnl"].replace("$","").replace("+","")) for d in reversed(_pnl_daily)]
                _streak_type = "W" if _streak_days[0] > 0 else "L"
                _streak_count = sum(1 for d in _streak_days if (d > 0) == (_streak_days[0] > 0) and (d > 0 or True))
                # stop counting when streak breaks
                _streak_count = 0
                for _d in _streak_days:
                    if (_d > 0) == (_streak_days[0] > 0):
                        _streak_count += 1
                    else:
                        break
                _streak = f"{_streak_count}{_streak_type}"
        except Exception as _pnl_err:
            print(f"  [HTML] P&L load failed: {_pnl_err}")

        import datetime as _dt2
        _timestamp = _dt2.datetime.now().strftime("%Y-%m-%d %I:%M %p")

        # Compute hit rates + P&L for EV group and star buckets
        _ranked_for_html = _all_ranked or picks
        _group_data = {
            "best_bets": {"hit_rate": group_hit_rate(True), "pnl": group_pnl(True)},
        }
        _tier_hit_rates = {sc: star_bucket_hit_rate(sc) for sc in range(5, -1, -1)}
        _tier_pnl       = {sc: star_bucket_pnl(sc)      for sc in range(5, -1, -1)}

        import time as _time
        _version = str(int(_time.time()))

        _html_str = generate_picks_html(
            _ranked_for_html,
            today=_timestamp,
            auc=_auc,
            ml_influence=_ml_influence,
            win_rate=_win_rate,
            net_pnl=_net_pnl,
            roi=_roi,
            record=_record,
            model_yesterday_pnl=_model_yesterday_pnl,
            model_cumulative_pnl=_model_cumulative_pnl,
            model_days_tracked=_model_days_tracked,
            streak=_streak,
            group_data=_group_data,
            tier_hit_rates=_tier_hit_rates,
            tier_pnl=_tier_pnl,
            version=_version,
            best_bets=_best_bets,
        )

        # Write version.txt — JS on the page fetches this from raw.githubusercontent.com
        # (no CDN lag) and redirects with ?v=... to bust the GitHub Pages Fastly cache.
        _version_file = Path(__file__).parent.parent / "docs" / "version.txt"
        with open(_version_file, "w") as _vf:
            _vf.write(_version + "\n")

        # Save dated copy
        _html_dated = Path(__file__).parent.parent / "picks" / f"picks_{TODAY}.html"
        with open(_html_dated, "w", encoding="utf-8") as _hf:
            _hf.write(_html_str)

        # Save to docs/ for GitHub Pages (always overwrites → latest picks)
        _html_pages = Path(__file__).parent.parent / "docs" / "index.html"
        with open(_html_pages, "w", encoding="utf-8") as _hf:
            _hf.write(_html_str)

        # Generate leaderboard page
        _lb_html = generate_leaderboard_html(today_str=TODAY)
        _lb_path = Path(__file__).parent.parent / "docs" / "leaderboard.html"
        with open(_lb_path, "w", encoding="utf-8") as _lf:
            _lf.write(_lb_html)

        # Generate player-data.json for player-card.html deep-dive links
        _pd_json = generate_player_data_json(_ranked_for_html, today=TODAY)
        _pd_path = Path(__file__).parent.parent / "docs" / "player-data.json"
        with open(_pd_path, "w", encoding="utf-8") as _pf:
            _pf.write(_pd_json)

        print(f"  [HTML] GitHub Pages updated → docs/index.html + docs/leaderboard.html + docs/player-data.json")
except Exception as _he:
    print(f"  [HTML] Skipped: {_he}")

# ── Auto-commit + push to GitHub ───────────────────────────────────────────────

try:
    import subprocess as _sp
    _repo = str(Path(__file__).parent.parent)
    if not args.use_cache:
        # Full run: commit all generated files
        _git_files = [
            "ml_weights.json", "agents/predictor.py",
            "agents/bet_tracker.py", "scripts/daily_picks.py",
            "ml/optimize_weights.py", "ml/fetch_actual_results.py",
            "ml/build_historical_dataset.py", "README.md", "requirements.txt",
            "tools/generate_html.py", "docs/index.html", "docs/leaderboard.html",
            "docs/player-data.json",
        ]
        _commit_msg = f"Auto-update {TODAY} — picks run"
    else:
        # Cache run: only commit HTML (picks changed, P&L/chips must stay correct)
        _git_files = ["docs/index.html", "docs/leaderboard.html", "docs/player-data.json", f"picks/picks_{TODAY}.html"]
        _commit_msg = f"picks({TODAY}): re-run from cache — lineup update"

    _sp.run(["/usr/bin/git", "-C", _repo, "add"] + _git_files, capture_output=True)
    _result = _sp.run(
        ["/usr/bin/git", "-C", _repo, "commit", "-m", _commit_msg],
        capture_output=True, text=True
    )
    if "nothing to commit" in _result.stdout:
        print("  [GitHub] No changes to commit.")
    else:
        _sp.run(["/usr/bin/git", "-C", _repo, "push"], capture_output=True)
        print("  [GitHub] Changes pushed to github.com/sliwij25/DingersHotline")
except Exception as e:
    print(f"  [GitHub] Push skipped: {e}")

# ── Notifications (Telegram primary, iMessage fallback) ────────────────────────

if not args.use_cache and not args.no_notify:
    import subprocess as _nsp, requests as _req
    _top = picks[:3] if picks else []
    _top3_names = "\n".join(
        f"{i+1}. {p.get('player','?')}" for i, p in enumerate(_top)
    ) if _top else "  no picks yet"
    _updated_prefix = "🔄 UPDATED — " if _is_rerun else ""
    _caption = f"{_updated_prefix}⚾ Dingers Hotline — {TODAY}\n\nTop 3:\n{_top3_names}\n\nFull picks → dingershotline.com"

    # 1. Telegram (primary) — send message with top-3 names + URL
    _tg_sent = False
    try:
        _tg_token = os.getenv("TELEGRAM_BOT_TOKEN") or ""
        if not _tg_token:
            _env_path = os.path.join(os.path.expanduser("~"), ".claude", "channels", "telegram", ".env")
            if os.path.exists(_env_path):
                with open(_env_path) as _f:
                    for _line in _f:
                        if _line.startswith("TELEGRAM_BOT_TOKEN="):
                            _tg_token = _line.strip().split("=", 1)[1]
        if _tg_token:
            _tg_chat = "-1003940624182"  # Dingers Hotline group
            _resp = _req.post(
                f"https://api.telegram.org/bot{_tg_token}/sendMessage",
                data={"chat_id": _tg_chat, "text": _caption},
                timeout=10,
            )
            if _resp.status_code == 200:
                _tg_sent = True
                _notify_flag.touch()
                print("  [Telegram] Notification sent.")
            else:
                raise RuntimeError(_resp.text[:200])
    except Exception as _e:
        print(f"  [Telegram] Skipped: {_e}")

    # 2. iMessage (fallback — only if Telegram failed)
    if not _tg_sent:
        try:
            _imsg = _caption.replace("\n", " ")
            _script = (
                f'tell application "Messages"\n'
                f'  set s to 1st service whose service type is iMessage\n'
                f'  send "{_imsg}" to buddy "+14148811460" of s\n'
                f'end tell'
            )
            _nsp.run(["osascript", "-e", _script], capture_output=True, timeout=30)
            print("  [iMessage] Notification sent (Telegram fallback).")
        except Exception as _e:
            print(f"  [iMessage] Skipped: {_e}")
