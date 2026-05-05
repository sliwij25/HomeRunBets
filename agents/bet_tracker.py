"""
Bet Tracker Agent

Skills:
  - save_pick_factors         : store signal snapshot for a pick (algorithm tracking)
  - model_pnl_report          : hypothetical P&L if $10 was bet on every top-15 pick
  - model_performance_report  : hit rates, ROI, and rank-bucket breakdown
  - score_bucket_hit_rate     : HR hit rate for a score range
  - score_bucket_pnl          : hypothetical P&L for a score range
"""
import json
from datetime import date
from typing import Optional

from .base import get_db_conn


# ── player_attributes table (permanent player info — handedness, etc.) ────────

_CREATE_PLAYER_ATTRS = """
CREATE TABLE IF NOT EXISTS player_attributes (
    mlb_id     INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL,
    bat_side   TEXT,        -- L / R / S
    throws     TEXT,        -- L / R (pitchers)
    updated_at TEXT    DEFAULT (datetime('now'))
);
"""

def _ensure_player_attrs_table(conn) -> None:
    conn.execute(_CREATE_PLAYER_ATTRS)
    conn.commit()


def upsert_player_attr(mlb_id: int, name: str,
                       bat_side: str = None, throws: str = None) -> None:
    """
    Persist a player's static attributes (handedness, etc.).
    Safe to call every run — only writes when values are non-null and meaningful.
    """
    if not mlb_id or not name:
        return
    conn = get_db_conn()
    try:
        _ensure_player_attrs_table(conn)
        # Only update fields we actually have — don't overwrite good data with None
        fields, vals = ["name", "updated_at"], [name, date.today().isoformat()]
        if bat_side and bat_side != "?":
            fields.append("bat_side"); vals.append(bat_side)
        if throws and throws != "?":
            fields.append("throws"); vals.append(throws)
        set_clause = ", ".join(f"{f} = excluded.{f}" for f in fields)
        conn.execute(f"""
            INSERT INTO player_attributes (mlb_id, {', '.join(fields)})
            VALUES ({mlb_id}, {', '.join('?' for _ in fields)})
            ON CONFLICT(mlb_id) DO UPDATE SET {set_clause}
        """, vals)
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def get_bat_side(mlb_id: int) -> str:
    """Look up a player's bat side from the persistent DB. Returns '?' if unknown."""
    if not mlb_id:
        return "?"
    conn = get_db_conn()
    try:
        _ensure_player_attrs_table(conn)
        row = conn.execute(
            "SELECT bat_side FROM player_attributes WHERE mlb_id = ?", (mlb_id,)
        ).fetchone()
        return row[0] if row and row[0] else "?"
    except Exception:
        return "?"
    finally:
        conn.close()


def get_bat_side_by_name(name: str) -> str:
    """Look up bat side by player name. Returns '?' if unknown."""
    if not name:
        return "?"
    conn = get_db_conn()
    try:
        _ensure_player_attrs_table(conn)
        row = conn.execute(
            "SELECT bat_side FROM player_attributes WHERE name = ?", (name,)
        ).fetchone()
        return row[0] if row and row[0] else "?"
    except Exception:
        return "?"
    finally:
        conn.close()


# ── pick_factors table helpers ─────────────────────────────────────────────────

_CREATE_PICK_FACTORS = """
CREATE TABLE IF NOT EXISTS pick_factors (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_date         TEXT    NOT NULL,
    player           TEXT    NOT NULL,
    algo_version     TEXT    DEFAULT '2.0',
    confidence       TEXT,
    score            REAL,
    rank             INTEGER,
    homered          INTEGER,
    ev_10            REAL,
    kelly_size       REAL,
    value_edge       REAL,
    pinnacle_odds    TEXT,
    best_odds        TEXT,
    platoon          TEXT,
    barrel_rate      REAL,
    hard_hit_pct     REAL,
    hr_fb_ratio      REAL,
    xiso             REAL,
    bpp_hr_pct       REAL,
    park_hr_factor   REAL,
    recent_form_14d  INTEGER,
    pitcher_hr_per_9 REAL,
    h2h_hr           INTEGER,
    h2h_ab           INTEGER,
    is_home          INTEGER,
    lineup_confirmed INTEGER,
    venue_slugging   TEXT,
    game_pk          TEXT,
    created_at       TEXT    DEFAULT (datetime('now')),
    UNIQUE(bet_date, player, game_pk)
);
"""

# Columns added after initial release — migrated safely at runtime
_MIGRATION_COLUMNS = [
    ("score",            "REAL"),
    ("rank",             "INTEGER"),
    ("homered",          "INTEGER"),
    ("xiso",             "REAL"),
    ("xslg",             "REAL"),
    ("xhr_rate",         "REAL"),
    ("fb_pct",           "REAL"),
    ("launch_angle",     "REAL"),
    ("ev_avg",           "REAL"),
    ("sweet_spot_pct",   "REAL"),
    ("bpp_hr_pct",       "REAL"),
    ("park_hr_factor",   "REAL"),
    ("lineup_confirmed", "INTEGER"),
    ("best_odds",        "TEXT"),
    ("team",             "TEXT"),
    ("blast_rate",       "REAL"),
    ("altitude_ft",      "REAL"),
    ("humidity_pct",     "REAL"),
    ("pressure_mb",      "REAL"),
    ("carry_ft",              "REAL"),
    ("stars",                 "INTEGER"),
    ("pitcher_hr_vs_hand",    "REAL"),
    ("pitcher_barrel_pct",    "REAL"),
    ("hr_luck",               "REAL"),
    ("pitcher_fb_pct",        "REAL"),
    ("pitcher_breaking_pct",  "REAL"),
    ("pitcher_offspeed_pct",  "REAL"),
    ("career_park_hr",              "INTEGER"),
    ("pitcher_career_hr_vs_hand",   "REAL"),
    ("batter_xslg_vs_fastball",     "REAL"),
    ("batter_xslg_vs_breaking",     "REAL"),
    ("batter_xslg_vs_offspeed",     "REAL"),
    ("game_pk",               "TEXT"),  # doubleheader support
    ("is_best_bet",           "INTEGER"),  # 1 = top-7 EV pick, 0 = also watching
]


def _ensure_pick_factors_table(conn) -> None:
    conn.execute(_CREATE_PICK_FACTORS)
    conn.commit()

    # Check schema BEFORE running column migrations so we can detect the old UNIQUE constraint.
    # Migrate old UNIQUE(bet_date, player) → UNIQUE(bet_date, player, game_pk)
    # Required for doubleheader support — same player can appear in two games on one day.
    # SQLite can't DROP CONSTRAINT, so we rebuild the table when the old schema is detected.
    schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='pick_factors'"
    ).fetchone()
    needs_rebuild = (
        schema is not None
        and "UNIQUE(bet_date, player)" in schema[0]
        and "UNIQUE(bet_date, player, game_pk)" not in schema[0]
    )
    if needs_rebuild:
        existing_cols = [row[1] for row in conn.execute("PRAGMA table_info(pick_factors)").fetchall()]
        conn.execute("ALTER TABLE pick_factors RENAME TO _pick_factors_old")
        conn.execute(_CREATE_PICK_FACTORS)
        # Copy all columns that exist in both old and new table
        new_base = {row[1] for row in conn.execute("PRAGMA table_info(pick_factors)").fetchall()}
        shared = [c for c in existing_cols if c in new_base]
        cols_sql = ", ".join(shared)
        conn.execute(f"INSERT INTO pick_factors ({cols_sql}) SELECT {cols_sql} FROM _pick_factors_old")
        conn.execute("DROP TABLE _pick_factors_old")
        conn.commit()

    # Add new columns to existing tables without breaking old rows
    existing = {row[1] for row in conn.execute("PRAGMA table_info(pick_factors)").fetchall()}
    for col_name, col_type in _MIGRATION_COLUMNS:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE pick_factors ADD COLUMN {col_name} {col_type}")

    # Drop legacy index and ensure new composite-key index exists
    conn.execute("DROP INDEX IF EXISTS idx_pick_factors_date_player")
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_pick_factors_date_player_game
        ON pick_factors (bet_date, player, game_pk)
    """)
    conn.commit()


def save_pick_factors(bet_date: str, player: str, signals: dict,
                      confidence: str = None,
                      algo_version: str = "2.0",
                      score: float = None,
                      rank: int = None,
                      stars: int = None,
                      game_pk: str = None,
                      is_best_bet: int = 0) -> str:
    """
    Persist the algorithmic signal snapshot for one pick.
    Called automatically by daily_picks.py for ALL ranked players (not just bets),
    so we can train the ML model on unbiased outcome data.

    Args:
        bet_date:     YYYY-MM-DD
        player:       Full player name
        signals:      Dict from _rank_picks_python() pick["signals"]
        confidence:   "HIGH" / "MEDIUM" / "LOW"
        algo_version: Tag to track when the algorithm is updated
        score:        Raw Homer score (float)
        rank:         Rank among all players scored that day (1 = best)
    """
    conn = get_db_conn()
    try:
        _ensure_pick_factors_table(conn)
        if rank is not None:
            conn.execute(
                "DELETE FROM pick_factors WHERE bet_date=? AND rank=? AND player!=?",
                (bet_date, rank, player)
            )
        if game_pk is None:
            conn.execute(
                "DELETE FROM pick_factors WHERE bet_date=? AND player=? AND game_pk IS NULL",
                (bet_date, player)
            )
        conn.execute("""
            INSERT INTO pick_factors
              (bet_date, player, algo_version, confidence, score, rank, stars,
               ev_10, kelly_size, value_edge, pinnacle_odds, best_odds,
               platoon, barrel_rate, hard_hit_pct, hr_fb_ratio,
               xiso, xslg, xhr_rate, fb_pct, launch_angle, ev_avg, sweet_spot_pct,
               bpp_hr_pct, park_hr_factor,
               recent_form_14d, pitcher_hr_per_9, pitcher_hr_vs_hand, pitcher_barrel_pct,
               h2h_hr, h2h_ab, is_home, lineup_confirmed, venue_slugging,
               team, blast_rate, altitude_ft, humidity_pct, pressure_mb, carry_ft, hr_luck,
               career_park_hr, pitcher_career_hr_vs_hand,
               batter_xslg_vs_fastball, batter_xslg_vs_breaking, batter_xslg_vs_offspeed,
               game_pk, is_best_bet)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(bet_date, player, game_pk) DO UPDATE SET
              rank=excluded.rank, score=excluded.score, stars=excluded.stars,
              algo_version=excluded.algo_version, confidence=excluded.confidence,
              ev_10=excluded.ev_10, kelly_size=excluded.kelly_size,
              value_edge=excluded.value_edge, pinnacle_odds=excluded.pinnacle_odds,
              best_odds=excluded.best_odds, platoon=excluded.platoon,
              barrel_rate=excluded.barrel_rate, hard_hit_pct=excluded.hard_hit_pct,
              hr_fb_ratio=excluded.hr_fb_ratio, xiso=excluded.xiso, xslg=excluded.xslg,
              xhr_rate=excluded.xhr_rate, fb_pct=excluded.fb_pct,
              launch_angle=excluded.launch_angle, ev_avg=excluded.ev_avg,
              sweet_spot_pct=excluded.sweet_spot_pct, bpp_hr_pct=excluded.bpp_hr_pct,
              park_hr_factor=excluded.park_hr_factor, recent_form_14d=excluded.recent_form_14d,
              pitcher_hr_per_9=excluded.pitcher_hr_per_9,
              pitcher_hr_vs_hand=excluded.pitcher_hr_vs_hand,
              pitcher_barrel_pct=excluded.pitcher_barrel_pct,
              h2h_hr=excluded.h2h_hr, h2h_ab=excluded.h2h_ab,
              is_home=excluded.is_home, lineup_confirmed=excluded.lineup_confirmed,
              venue_slugging=excluded.venue_slugging, team=excluded.team,
              blast_rate=excluded.blast_rate, altitude_ft=excluded.altitude_ft,
              humidity_pct=excluded.humidity_pct, pressure_mb=excluded.pressure_mb,
              carry_ft=excluded.carry_ft, hr_luck=excluded.hr_luck,
              career_park_hr=excluded.career_park_hr,
              pitcher_career_hr_vs_hand=excluded.pitcher_career_hr_vs_hand,
              batter_xslg_vs_fastball=excluded.batter_xslg_vs_fastball,
              batter_xslg_vs_breaking=excluded.batter_xslg_vs_breaking,
              batter_xslg_vs_offspeed=excluded.batter_xslg_vs_offspeed,
              game_pk=excluded.game_pk, is_best_bet=excluded.is_best_bet
        """, (
            bet_date, player, algo_version,
            confidence or signals.get("confidence"),
            score,
            rank,
            stars,
            signals.get("ev_10"),
            signals.get("kelly_size"),
            signals.get("value_edge"),
            signals.get("pinnacle_odds"),
            signals.get("best_odds"),
            signals.get("platoon"),
            signals.get("barrel_rate"),
            signals.get("hard_hit_pct"),
            signals.get("hr_fb_ratio"),
            signals.get("xiso"),
            signals.get("xslg"),
            signals.get("xhr_rate"),
            signals.get("fb_pct"),
            signals.get("launch_angle"),
            signals.get("ev_avg"),
            signals.get("sweet_spot_pct"),
            signals.get("bpp_hr_pct"),
            signals.get("park_hr_factor"),
            signals.get("recent_form_14d"),
            signals.get("pitcher_hr_per_9"),
            signals.get("pitcher_hr_vs_hand"),
            signals.get("pitcher_barrel_pct"),
            signals.get("h2h_hr"),
            signals.get("h2h_ab"),
            1 if signals.get("is_home") else 0,
            1 if signals.get("lineup_confirmed", True) else 0,
            signals.get("venue_slugging"),
            signals.get("team"),
            signals.get("blast_rate"),
            signals.get("altitude_ft"),
            signals.get("humidity_pct"),
            signals.get("pressure_mb"),
            signals.get("carry_ft"),
            signals.get("hr_luck"),
            signals.get("career_park_hr"),
            signals.get("pitcher_career_hr_vs_hand"),
            signals.get("batter_xslg_vs_fastball"),
            signals.get("batter_xslg_vs_breaking"),
            signals.get("batter_xslg_vs_offspeed"),
            game_pk or signals.get("game_pk"),
            is_best_bet,
        ))
        # Backfill stars on existing rows that were saved before stars column existed
        if stars is not None:
            _gpk = game_pk or signals.get("game_pk")
            conn.execute(
                "UPDATE pick_factors SET stars=? WHERE bet_date=? AND player=? AND game_pk IS ? AND stars IS NULL",
                (stars, bet_date, player, _gpk)
            )
        conn.commit()
        return f"Saved signals for {player} ({bet_date})"
    finally:
        conn.close()


def backfill_pick_odds(bet_date: str, comparisons: list) -> int:
    """
    Update pick_factors with best_odds + pinnacle_odds for a given date.
    Called by record_results.py at night when odds are guaranteed to be posted.
    Only fills NULL rows — never overwrites existing odds data.
    Returns the number of rows updated.
    """
    from difflib import SequenceMatcher

    conn = get_db_conn()
    try:
        _ensure_pick_factors_table(conn)
        picks = conn.execute(
            "SELECT player FROM pick_factors WHERE bet_date=? AND rank IS NOT NULL",
            (bet_date,)
        ).fetchall()
        pick_names = [r[0] for r in picks]

        updated = 0
        for comp in comparisons:
            odds_name = comp.get("player", "")
            best_o    = comp.get("best_odds")
            pin_o     = comp.get("pinnacle")
            if not odds_name or (not best_o and not pin_o):
                continue

            # exact match first, then fuzzy
            matched = odds_name if odds_name in pick_names else None
            if not matched:
                best_ratio, best_name = 0.0, None
                for pname in pick_names:
                    r = SequenceMatcher(None, odds_name.lower(), pname.lower()).ratio()
                    if r > best_ratio:
                        best_ratio, best_name = r, pname
                if best_ratio >= 0.82:
                    matched = best_name

            if not matched:
                continue

            conn.execute("""
                UPDATE pick_factors
                SET best_odds     = COALESCE(best_odds,     ?),
                    pinnacle_odds = COALESCE(pinnacle_odds, ?)
                WHERE bet_date=? AND player=?
            """, (best_o, pin_o, bet_date, matched))
            updated += conn.total_changes

        conn.commit()
        return updated
    finally:
        conn.close()


def model_pnl_report() -> str:
    """
    Hypothetical P&L if $10 was bet on every top-15 pick each day.
    Losses count as -$10 regardless of whether odds were captured.
    Wins only count when best_odds is known (user can supply missing ones manually).
    """
    conn = get_db_conn()
    try:
        rows = conn.execute("""
            SELECT bet_date, player, rank, best_odds, homered
            FROM (
                SELECT bet_date, player, rank, score, best_odds, homered,
                       ROW_NUMBER() OVER (
                           PARTITION BY bet_date
                           ORDER BY rank ASC, score DESC, player ASC
                       ) AS rn
                FROM pick_factors
                WHERE homered IS NOT NULL
                  AND rank IS NOT NULL
                  AND algo_version NOT LIKE 'hist_%'
            )
            WHERE rn <= 15
            ORDER BY bet_date, rank
        """).fetchall()
    finally:
        conn.close()

    if not rows:
        return json.dumps({"error": "No labeled picks yet."})

    def _to_decimal(odds_str: str) -> float | None:
        try:
            o = int(odds_str)
            return (o / 100 + 1) if o > 0 else (100 / abs(o) + 1)
        except Exception:
            return None

    days: dict = {}
    for bet_date, player, rank, best_odds, homered in rows:
        if homered:
            dec = _to_decimal(best_odds)
            if dec is None:
                dec = 4.5   # fallback: +350 avg HR prop when odds not captured
            pnl = round(dec * 10 - 10, 2)
        else:
            pnl = -10.0
        if bet_date not in days:
            days[bet_date] = {"picks": 0, "wins": 0, "pnl": 0.0, "players": []}
        days[bet_date]["picks"] += 1
        days[bet_date]["wins"] += int(homered)
        days[bet_date]["pnl"] = round(days[bet_date]["pnl"] + pnl, 2)
        days[bet_date]["players"].append({
            "rank": rank, "player": player,
            "odds": best_odds or "—", "homered": bool(homered), "pnl": pnl,
        })

    cumulative = 0.0
    daily_rows = []
    for date_str in sorted(days):
        d = days[date_str]
        cumulative = round(cumulative + d["pnl"], 2)
        daily_rows.append({
            "date": date_str,
            "picks_with_odds": d["picks"],
            "wins": d["wins"],
            "day_pnl": f"${d['pnl']:+.2f}",
            "cumulative_pnl": f"${cumulative:+.2f}",
            "players": d["players"],
        })

    total_picks = sum(d["picks"] for d in days.values())
    total_wins  = sum(d["wins"]  for d in days.values())
    return json.dumps({
        "model_pnl_summary": {
            "days_tracked": len(days),
            "total_picks_with_odds": total_picks,
            "total_wins": total_wins,
            "win_pct": f"{total_wins/total_picks*100:.1f}%" if total_picks else "0%",
            "total_wagered": f"${total_picks * 10:.2f}",
            "cumulative_pnl": f"${cumulative:+.2f}",
            "roi": f"{cumulative / (total_picks * 10) * 100:+.1f}%" if total_picks else "0%",
        },
        "daily": daily_rows,
    }, indent=2)


def yesterday_results_snapshot(yesterday: str) -> dict:
    """
    Return the top-15 picks from yesterday with homered labels.
    Used at the start of each daily run to display previous-day results.

    Returns a dict with keys:
      date        — the date queried
      picks       — list of dicts: rank, player, stars, homered, best_odds, pnl
      hr_count    — number who homered
      day_pnl     — hypothetical $10/pick P&L for the day
      labeled     — True if any homered labels exist yet
    """
    conn = get_db_conn()
    try:
        rows = conn.execute("""
            SELECT player, rank, stars, homered, best_odds, score
            FROM (
                SELECT player, rank, stars, homered, best_odds, score,
                       ROW_NUMBER() OVER (
                           PARTITION BY bet_date
                           ORDER BY rank ASC, score DESC, player ASC
                       ) AS rn
                FROM pick_factors
                WHERE bet_date = ?
                  AND rank IS NOT NULL
                  AND algo_version NOT LIKE 'hist_%'
            )
            WHERE rn <= 15
            ORDER BY rank ASC, score DESC
        """, (yesterday,)).fetchall()
    finally:
        conn.close()

    if not rows:
        return {"date": yesterday, "picks": [], "hr_count": 0, "day_pnl": 0.0, "labeled": False}

    def _to_decimal(odds_str):
        try:
            o = int(odds_str)
            return (o / 100 + 1) if o > 0 else (100 / abs(o) + 1)
        except Exception:
            return None

    labeled = any(r[3] is not None for r in rows)
    picks, day_pnl, hr_count = [], 0.0, 0
    for player, rank, stars, homered, best_odds, score in rows:
        if homered:
            dec = _to_decimal(best_odds) or 4.5
            pnl = round(dec * 10 - 10, 2)
            hr_count += 1
        elif homered == 0:
            pnl = -10.0
        else:
            pnl = None  # not labeled yet
        if pnl is not None:
            day_pnl = round(day_pnl + pnl, 2)
        picks.append({
            "rank":    rank,
            "player":  player,
            "stars":   stars,
            "homered": homered,
            "odds":    best_odds or "—",
            "pnl":     pnl,
        })

    return {
        "date":     yesterday,
        "picks":    picks,
        "hr_count": hr_count,
        "day_pnl":  day_pnl,
        "labeled":  labeled,
    }


# Score thresholds mirroring predictor._star_rating — single source of truth.
# Each entry: star_count → (min_score_inclusive, max_score_exclusive | None=unbounded)
STAR_SCORE_RANGES: dict[int, tuple[float | None, float | None]] = {
    5: (19.0, None),
    4: (16.0, 19.0),
    3: (14.0, 16.0),
    2: (13.0, 14.0),
    1: (None, 13.0),
}


def _score_where(min_score: float | None, max_score: float | None) -> tuple[str, list]:
    clauses, params = [], []
    if min_score is not None:
        clauses.append("score >= ?")
        params.append(min_score)
    if max_score is not None:
        clauses.append("score < ?")
        params.append(max_score)
    return (" AND ".join(clauses), params)


def _top20_base_query(score_clause: str, params: list) -> str:
    """CTE that mirrors model_pnl_report: top-15 picks per day, then filtered by score."""
    return (
        f"SELECT best_odds, homered FROM ("
        f"  SELECT best_odds, homered, score,"
        f"         ROW_NUMBER() OVER (PARTITION BY bet_date ORDER BY rank, player) AS rn"
        f"  FROM pick_factors"
        f"  WHERE homered IS NOT NULL AND rank IS NOT NULL"
        f"    AND algo_version NOT LIKE 'hist_%'"
        f") WHERE rn <= 15 AND {score_clause}",
        params,
    )


def score_bucket_hit_rate(min_score: float | None, max_score: float | None) -> tuple[int, int]:
    """Return (n_picks, n_homers) for top-15 picks whose score falls in [min_score, max_score)."""
    conn = get_db_conn()
    try:
        _ensure_pick_factors_table(conn)
        score_clause, params = _score_where(min_score, max_score)
        sql = (
            f"SELECT COUNT(*), SUM(homered) FROM ("
            f"  SELECT homered, score,"
            f"         ROW_NUMBER() OVER (PARTITION BY bet_date ORDER BY rank, player) AS rn"
            f"  FROM pick_factors"
            f"  WHERE homered IS NOT NULL AND rank IS NOT NULL"
            f"    AND algo_version NOT LIKE 'hist_%'"
            f") WHERE rn <= 15 AND {score_clause}"
        )
        row = conn.execute(sql, params).fetchone()
        return (row[0], int(row[1] or 0))
    finally:
        conn.close()


def score_bucket_pnl(min_score: float | None, max_score: float | None) -> float | None:
    """Return cumulative hypothetical P&L ($10/pick) for top-15 picks in the given score range.

    Mirrors model_pnl_report: losses = -$10, wins require best_odds.
    Returns None if there are no qualifying labeled rows yet.
    """
    conn = get_db_conn()
    try:
        _ensure_pick_factors_table(conn)
        score_clause, params = _score_where(min_score, max_score)
        sql, qparams = _top20_base_query(score_clause, params)
        rows = conn.execute(sql, qparams).fetchall()
    finally:
        conn.close()

    if not rows:
        return None

    def _to_decimal(odds_str):
        try:
            o = int(odds_str)
            return (o / 100 + 1) if o > 0 else (100 / abs(o) + 1)
        except Exception:
            return None

    total = 0.0
    for best_odds, homered in rows:
        if homered:
            dec = _to_decimal(best_odds)
            if dec is None:
                continue
            total += round(dec * 10 - 10, 2)
        else:
            total -= 10.0
    return round(total, 2)


_STAR_FROM_RANK_SQL = """
  COALESCE(stars,
    CASE WHEN rank <= 5 THEN 4
         WHEN rank <= 10 THEN 3
         WHEN rank <= 15 THEN 2
         ELSE 1 END)
"""


def star_bucket_hit_rate(star_count: int) -> tuple[int, int]:
    """Return (n_picks, n_homers) for top-15 picks with exactly star_count stars.
    When stars column is NULL, derives star count from rank bands."""
    conn = get_db_conn()
    try:
        _ensure_pick_factors_table(conn)
        row = conn.execute(
            f"""
            SELECT COUNT(*), SUM(homered) FROM (
              SELECT homered,
                     {_STAR_FROM_RANK_SQL} AS derived_stars,
                     ROW_NUMBER() OVER (PARTITION BY bet_date ORDER BY rank, player) AS rn
              FROM pick_factors
              WHERE homered IS NOT NULL AND rank IS NOT NULL
                AND algo_version NOT LIKE 'hist_%'
            ) WHERE rn <= 15 AND derived_stars = ?
            """,
            (star_count,),
        ).fetchone()
        return (row[0], int(row[1] or 0))
    finally:
        conn.close()


def star_bucket_pnl(star_count: int) -> float | None:
    """Return cumulative hypothetical P&L ($10/pick) for top-15 picks with star_count stars.
    When stars column is NULL, derives star count from rank bands."""
    conn = get_db_conn()
    try:
        _ensure_pick_factors_table(conn)
        rows = conn.execute(
            f"""
            SELECT best_odds, homered FROM (
              SELECT best_odds, homered,
                     {_STAR_FROM_RANK_SQL} AS derived_stars,
                     ROW_NUMBER() OVER (PARTITION BY bet_date ORDER BY rank, player) AS rn
              FROM pick_factors
              WHERE homered IS NOT NULL AND rank IS NOT NULL
                AND algo_version NOT LIKE 'hist_%'
            ) WHERE rn <= 15 AND derived_stars = ?
            """,
            (star_count,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return None

    def _to_decimal(odds_str):
        try:
            o = int(odds_str)
            return (o / 100 + 1) if o > 0 else (100 / abs(o) + 1)
        except Exception:
            return None

    total = 0.0
    for best_odds, homered in rows:
        if homered:
            dec = _to_decimal(best_odds)
            if dec is None:
                dec = 4.5   # fallback: +350 avg HR prop when odds not captured
            total += round(dec * 10 - 10, 2)
        else:
            total -= 10.0
    return round(total, 2) if rows else None


def group_hit_rate(best_bets: bool) -> tuple[int, int]:
    """Return (n_picks, n_homers) for the Best Bets (is_best_bet=1) or Also Watching group.
    Falls back to rank<=7 proxy for rows saved before is_best_bet column existed.
    """
    conn = get_db_conn()
    try:
        _ensure_pick_factors_table(conn)
        row = conn.execute(
            """
            SELECT COUNT(*), SUM(homered) FROM (
              SELECT homered,
                     COALESCE(is_best_bet, CASE WHEN rank <= 7 THEN 1 ELSE 0 END) AS grp,
                     ROW_NUMBER() OVER (PARTITION BY bet_date ORDER BY rank, player) AS rn
              FROM pick_factors
              WHERE homered IS NOT NULL AND rank IS NOT NULL
                AND algo_version NOT LIKE 'hist_%'
            ) WHERE rn <= 15 AND grp = ?
            """,
            (1 if best_bets else 0,),
        ).fetchone()
        return (row[0], int(row[1] or 0))
    finally:
        conn.close()


def group_pnl(best_bets: bool) -> float | None:
    """Return cumulative hypothetical P&L ($10/pick) for Best Bets or Also Watching group."""
    conn = get_db_conn()
    try:
        _ensure_pick_factors_table(conn)
        rows = conn.execute(
            """
            SELECT best_odds, homered FROM (
              SELECT best_odds, homered,
                     COALESCE(is_best_bet, CASE WHEN rank <= 7 THEN 1 ELSE 0 END) AS grp,
                     ROW_NUMBER() OVER (PARTITION BY bet_date ORDER BY rank, player) AS rn
              FROM pick_factors
              WHERE homered IS NOT NULL AND rank IS NOT NULL
                AND algo_version NOT LIKE 'hist_%'
            ) WHERE rn <= 15 AND grp = ?
            """,
            (1 if best_bets else 0,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return None

    def _to_decimal(odds_str):
        try:
            o = int(odds_str)
            return (o / 100 + 1) if o > 0 else (100 / abs(o) + 1)
        except Exception:
            return None

    total = 0.0
    for best_odds, homered in rows:
        if homered:
            dec = _to_decimal(best_odds)
            if dec is None:
                dec = 4.5   # +350 fallback when odds not captured
            total += round(dec * 10 - 10, 2)
        else:
            total -= 10.0
    return round(total, 2)


def trending_picks(min_streak: int = 3, top_n_threshold: int = 10) -> list[dict]:
    """
    Return players ranked in top_n_threshold for min_streak or more consecutive days.
    Each entry: {"player": str, "streak": int, "ranks": list[int], "dates": list[str]}
    Sorted by streak length descending.
    """
    conn = get_db_conn()
    try:
        rows = conn.execute(
            """
            SELECT player, bet_date, rank
            FROM pick_factors
            WHERE rank IS NOT NULL AND rank <= ?
            ORDER BY player, bet_date
            """,
            (top_n_threshold,),
        ).fetchall()
    finally:
        conn.close()

    from itertools import groupby as _gb
    from datetime import date as _date, timedelta as _td

    results = []
    for player, group in _gb(rows, key=lambda r: r[0]):
        entries = [(r[1], r[2]) for r in group]  # (date_str, rank)
        # Find max consecutive-day streaks
        streak_dates, streak_ranks = [entries[0][0]], [entries[0][1]]
        best = [(list(streak_dates), list(streak_ranks))]
        for i in range(1, len(entries)):
            prev_d = _date.fromisoformat(entries[i-1][0])
            curr_d = _date.fromisoformat(entries[i][0])
            if (curr_d - prev_d).days == 1:
                streak_dates.append(entries[i][0])
                streak_ranks.append(entries[i][1])
            else:
                streak_dates, streak_ranks = [entries[i][0]], [entries[i][1]]
            if len(streak_dates) > len(best[0][0]):
                best = [(list(streak_dates), list(streak_ranks))]
        best_dates, best_ranks = best[0]
        if len(best_dates) >= min_streak:
            results.append({
                "player": player,
                "streak": len(best_dates),
                "dates":  best_dates,
                "ranks":  best_ranks,
            })

    results.sort(key=lambda x: x["streak"], reverse=True)
    return results


def model_performance_report() -> str:
    """
    Print a plain-text model performance dashboard to stdout.
    Covers pick accuracy, rank bucket hit rates, confidence calibration,
    betting P&L, and ML model status. Called automatically at the end of
    daily_picks.py so it appears in the log every morning.
    """
    import os, json
    from datetime import date, timedelta

    lines = []
    add = lines.append

    add("=" * 60)
    add("  MODEL PERFORMANCE DASHBOARD")
    add("=" * 60)

    conn = get_db_conn()
    try:
        _ensure_pick_factors_table(conn)

        today_str = date.today().isoformat()
        week_ago  = (date.today() - timedelta(days=7)).isoformat()
        month_ago = (date.today() - timedelta(days=30)).isoformat()

        # ── 1. Pick accuracy (pick_factors with labeled outcomes) ─────────────
        total_labeled = conn.execute(
            "SELECT COUNT(*) FROM pick_factors WHERE homered IS NOT NULL"
        ).fetchone()[0]

        # Count how many live picks (with rank) are labeled — separate from historical bulk data
        live_labeled = conn.execute(
            "SELECT COUNT(*) FROM pick_factors WHERE homered IS NOT NULL AND rank IS NOT NULL"
        ).fetchone()[0]

        add(f"\n  PICK ACCURACY  ({live_labeled:,} live labeled picks | {total_labeled:,} total incl. historical)")
        add(f"  {'Bucket':<14} {'Picks':>6} {'HRs':>6} {'Hit Rate':>10}  {'vs base':>8}")
        add("  " + "-" * 50)

        base_rate = 8.1  # historical base rate from dataset

        buckets = [
            ("Top 3",   "rank <= 3"),
            ("Top 5",   "rank <= 5"),
            ("6-10",    "rank BETWEEN 6 AND 10"),
            ("11-20",   "rank BETWEEN 11 AND 20"),
            ("All live","rank IS NOT NULL"),
        ]
        any_rank_data = False
        for label, where in buckets:
            row = conn.execute(
                f"SELECT COUNT(*), SUM(homered) FROM pick_factors "
                f"WHERE homered IS NOT NULL AND {where}"
            ).fetchone()
            n, hits = row[0], (row[1] or 0)
            if n == 0:
                continue
            any_rank_data = True
            rate = hits / n * 100
            vs   = rate - base_rate
            vs_s = f"+{vs:.1f}pp" if vs >= 0 else f"{vs:.1f}pp"
            bar  = "█" * int(rate / 3)
            add(f"  {label:<14} {n:>6} {hits:>6} {rate:>9.1f}%  {vs_s:>8}  {bar}")

        if not any_rank_data:
            add("  (Populates after first game day — run picks daily to build this up)")

        # Last 7 days trend
        row7 = conn.execute(
            "SELECT COUNT(*), SUM(homered) FROM pick_factors "
            "WHERE homered IS NOT NULL AND rank <= 15 AND bet_date >= ?", (week_ago,)
        ).fetchone()
        if row7[0]:
            r7 = (row7[1] or 0) / row7[0] * 100
            add(f"\n  Last 7 days (top 15): {row7[0]} picks, {r7:.1f}% hit rate")

        # ── 2. Confidence tier calibration ────────────────────────────────────
        add(f"\n  CONFIDENCE CALIBRATION")
        add(f"  {'Tier':<10} {'Picks':>6} {'HRs':>6} {'Hit Rate':>10}")
        add("  " + "-" * 36)
        for tier in ("HIGH", "MEDIUM", "LOW"):
            row = conn.execute(
                "SELECT COUNT(*), SUM(homered) FROM pick_factors "
                "WHERE homered IS NOT NULL AND confidence=?", (tier,)
            ).fetchone()
            n, hits = row[0], (row[1] or 0)
            if n == 0:
                continue
            rate = hits / n * 100
            add(f"  {tier:<10} {n:>6} {hits:>6} {rate:>9.1f}%")

        # ── 4. ML model status ─────────────────────────────────────────────────
        add(f"\n  ML MODEL STATUS")
        weights_path = os.path.join(os.path.dirname(__file__), "..", "ml_weights.json")
        weights_path = os.path.normpath(weights_path)
        if os.path.exists(weights_path):
            try:
                with open(weights_path) as f:
                    w = json.load(f)
                trained_on = w.get("trained_on", "?")
                auc        = w.get("cv_auc_mean", 0)
                n_samples  = w.get("n_samples", 0)
                auc_std    = w.get("cv_auc_std", 0)
                days_since = (date.today() - date.fromisoformat(trained_on)).days if trained_on != "?" else "?"

                auc_grade = "strong" if auc >= 0.70 else "useful" if auc >= 0.60 else "developing"
                ml_weight_pct = min(70, max(0, (auc - 0.5) * 250))

                add(f"  {'Trained:':<20} {trained_on}  ({days_since} days ago)")
                add(f"  {'Training samples:':<20} {n_samples:,}")
                add(f"  {'Cross-val AUC:':<20} {auc:.3f} ± {auc_std:.3f}  [{auc_grade}]")
                add(f"  {'ML influence:':<20} {ml_weight_pct:.0f}% of final score  (grows as AUC improves)")

                # Top 3 features
                coeffs = w.get("coefficients", {})
                top3   = sorted(coeffs.items(), key=lambda x: abs(x[1]), reverse=True)[:3]
                top3_s = "  ".join(f"{f}({c:+.2f})" for f, c in top3)
                add(f"  {'Top features:':<20} {top3_s}")

                # Labeled picks since last training
                new_since = conn.execute(
                    "SELECT COUNT(*) FROM pick_factors WHERE homered IS NOT NULL AND bet_date > ?",
                    (trained_on,)
                ).fetchone()[0]
                # Retrain triggers when: (≥7 days old AND ≥200 new rows) OR ≥2000 new rows
                age_ok   = isinstance(days_since, int) and days_since >= 7
                rows_ok  = new_since >= 200
                bulk_ok  = new_since >= 2000
                if bulk_ok or (age_ok and rows_ok):
                    status = "retrain due tonight!"
                elif rows_ok:
                    days_left = max(0, 7 - (days_since if isinstance(days_since, int) else 0))
                    status = f"rows ready, retrain in {days_left}d"
                else:
                    status = f"{max(0, 200 - new_since)} rows until retrain"
                add(f"  {'New labeled picks:':<20} {new_since} since last training  ({status})")

            except Exception as e:
                add(f"  Could not read ml_weights.json: {e}")
        else:
            labeled_n = conn.execute(
                "SELECT COUNT(*) FROM pick_factors WHERE homered IS NOT NULL"
            ).fetchone()[0]
            add(f"  No model yet — {labeled_n}/100 labeled picks collected")
            add(f"  Model trains automatically once 100 picks are labeled.")

    finally:
        conn.close()

    add("\n" + "=" * 60)
    return "\n".join(lines)


def factor_performance_report() -> str:
    """
    Analyze which pick signals correlate with HRs across all labeled pick_factors rows.
    Uses homered column directly — no singles table dependency.
    """
    conn = get_db_conn()
    try:
        _ensure_pick_factors_table(conn)

        rows = conn.execute("""
            SELECT
                player, bet_date,
                confidence, ev_10, kelly_size, value_edge,
                platoon, barrel_rate, hard_hit_pct, hr_fb_ratio,
                recent_form_14d, pitcher_hr_per_9,
                h2h_hr, h2h_ab, is_home, algo_version,
                homered
            FROM pick_factors
            WHERE homered IS NOT NULL
              AND rank IS NOT NULL AND rank <= 15
            ORDER BY bet_date DESC
        """).fetchall()

        if not rows:
            return json.dumps({
                "status": "no_data",
                "message": "No labeled picks yet. Runs automatically each morning."
            }, indent=2)

        cols = ["player","bet_date","confidence","ev_10","kelly_size","value_edge",
                "platoon","barrel_rate","hard_hit_pct","hr_fb_ratio",
                "recent_form_14d","pitcher_hr_per_9","h2h_hr","h2h_ab",
                "is_home","algo_version","homered"]
        picks  = [dict(zip(cols, r)) for r in rows]
        total  = len(picks)
        hits   = [p for p in picks if p["homered"]]
        h_rate = len(hits) / total * 100 if total else 0

        def split_rate(key, condition_fn, label):
            group = [p for p in picks if condition_fn(p.get(key))]
            if not group:
                return None
            gh = sum(1 for p in group if p["homered"])
            return {"label": label, "picks": len(group),
                    "hits": gh, "hit_pct": f"{gh/len(group)*100:.1f}%"}

        sections = {}

        for tier in ("HIGH", "MEDIUM", "LOW"):
            g = [p for p in picks if p.get("confidence") == tier]
            if g:
                gh = sum(1 for p in g if p["homered"])
                sections.setdefault("by_confidence", {})[tier] = {
                    "picks": len(g), "hits": gh, "hit_pct": f"{gh/len(g)*100:.1f}%"
                }

        sections["ev_positive"]      = split_rate("ev_10", lambda v: v is not None and v > 0, "ev_10 > 0")
        sections["ev_negative"]      = split_rate("ev_10", lambda v: v is not None and v <= 0, "ev_10 <= 0")
        sections["value_flag"]       = split_rate("value_edge", lambda v: v is not None and v >= 3.0, "value_edge >= 3pp")
        sections["no_value_flag"]    = split_rate("value_edge", lambda v: v is not None and v < 3.0, "value_edge < 3pp")
        sections["platoon_plus"]     = split_rate("platoon", lambda v: v == "PLATOON+", "PLATOON+")
        sections["platoon_minus"]    = split_rate("platoon", lambda v: v == "platoon-", "platoon-")
        sections["h2h_has_hr"]       = split_rate("h2h_hr", lambda v: v is not None and v >= 1, "h2h_hr >= 1")
        sections["h2h_no_hr"]        = split_rate("h2h_hr", lambda v: v is not None and v == 0, "h2h_hr = 0")
        sections["pitcher_vuln"]     = split_rate("pitcher_hr_per_9", lambda v: v is not None and v >= 1.0, "pitcher HR/9 >= 1.0")
        sections["barrel_elite"]     = split_rate("barrel_rate", lambda v: v is not None and v >= 10.0, "barrel_rate >= 10%")
        sections["home_batter"]      = split_rate("is_home", lambda v: v == 1, "batting at home")
        sections["away_batter"]      = split_rate("is_home", lambda v: v == 0, "batting away")
        sections["hot_streak"]       = split_rate("recent_form_14d", lambda v: v is not None and v >= 2, "2+ HR in last 14 days")
        sections = {k: v for k, v in sections.items() if v is not None}

        version_stats = {}
        for p in picks:
            v = p.get("algo_version") or "unknown"
            version_stats.setdefault(v, {"picks": 0, "hits": 0})
            version_stats[v]["picks"] += 1
            if p["homered"]:
                version_stats[v]["hits"] += 1
        for v, s in version_stats.items():
            s["hit_pct"] = f"{s['hits']/s['picks']*100:.1f}%" if s["picks"] else "0%"

        return json.dumps({
            "status":           "success",
            "total_labeled":    total,
            "overall_hits":     len(hits),
            "overall_hit_pct":  f"{h_rate:.1f}%",
            "by_algo_version":  version_stats,
            "signal_breakdown": sections,
        }, indent=2)
    finally:
        conn.close()

