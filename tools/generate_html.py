"""
generate_html.py
Generate a self-contained HTML picks page for GitHub Pages.
Called from daily_picks.py after picks are ranked.
"""

from __future__ import annotations
import html as _html
import json as _json
import re as _re
import unicodedata as _unicodedata
from itertools import groupby


def _esc(s) -> str:
    return _html.escape(str(s)) if s is not None else ""


def _player_slug(name: str) -> str:
    nfkd = _unicodedata.normalize("NFKD", str(name))
    ascii_str = nfkd.encode("ascii", "ignore").decode("ascii")
    return _re.sub(r"[^a-z0-9]+", "-", ascii_str.lower()).strip("-")


def generate_player_data_json(picks: list[dict], today: str) -> str:
    players = []
    for rank, pick in enumerate(picks, 1):
        player = pick.get("player", "")
        sig = pick.get("signals") or {}

        game_time_et = ""
        gt = sig.get("game_time") or ""
        if gt:
            try:
                from datetime import datetime as _dt
                from zoneinfo import ZoneInfo
                utc = _dt.fromisoformat(gt.replace("Z", "+00:00"))
                et  = utc.astimezone(ZoneInfo("America/New_York"))
                game_time_et = et.strftime("%-I:%M %p ET")
            except Exception:
                game_time_et = gt[11:16]

        entry = {
            "slug":       _player_slug(player),
            "player":     player,
            "rank":       rank,
            "stars":      pick.get("stars", ""),
            "score":      pick.get("score", 0),
            "confidence": pick.get("confidence", "LOW"),
            "matchup":    pick.get("matchup", ""),
            "game_time_et": game_time_et,
            "reasoning":  pick.get("reasoning", ""),
            "signals": {
                "venue":              sig.get("venue"),
                "is_home":            sig.get("is_home"),
                "pitcher_name":       sig.get("pitcher_name"),
                "pitcher_throws":     sig.get("pitcher_throws"),
                "bat_side":           sig.get("bat_side"),
                "batting_order":      sig.get("batting_order"),
                "season_hr":          sig.get("season_hr"),
                "pa":                 sig.get("pa"),
                "status":             sig.get("status"),
                "xiso":               sig.get("xiso"),
                "xslg":               sig.get("xslg"),
                "barrel_rate":        sig.get("barrel_rate"),
                "hard_hit_pct":       sig.get("hard_hit_pct"),
                "ev_avg":             sig.get("ev_avg"),
                "ev_max":             sig.get("ev_max"),
                "sweet_spot_pct":     sig.get("sweet_spot_pct"),
                "fb_pct":             sig.get("fb_pct"),
                "launch_angle":       sig.get("launch_angle"),
                "blast_rate":         sig.get("blast_rate"),
                "xhr_rate":           sig.get("xhr_rate"),
                "hr_luck":            sig.get("hr_luck"),
                "pitcher_hr_per_9":   sig.get("pitcher_hr_per_9"),
                "pitcher_career_hr_vs_hand": sig.get("pitcher_career_hr_vs_hand"),
                "pitcher_barrel_pct": sig.get("pitcher_barrel_pct"),
                "pitcher_fb_pct":     sig.get("pitcher_fb_pct"),
                "pitcher_breaking_pct": sig.get("pitcher_breaking_pct"),
                "pitcher_offspeed_pct": sig.get("pitcher_offspeed_pct"),
                "batter_xslg_vs_fastball": sig.get("batter_xslg_vs_fastball"),
                "batter_xslg_vs_breaking": sig.get("batter_xslg_vs_breaking"),
                "batter_xslg_vs_offspeed": sig.get("batter_xslg_vs_offspeed"),
                "park_hr_factor":     sig.get("park_hr_factor"),
                "career_park_hr":     sig.get("career_park_hr"),
                "carry_ft":           sig.get("carry_ft"),
                "temp_f":             sig.get("temp_f"),
                "wind_mph":           sig.get("wind_mph"),
                "wind_direction_bpp": sig.get("wind_direction_bpp"),
                "venue_slugging":     sig.get("venue_slugging"),
                "bpp_hr_pct":         sig.get("bpp_hr_pct"),
                "h2h_hr":             sig.get("h2h_hr"),
                "h2h_ab":             sig.get("h2h_ab"),
                "recent_form_14d":    sig.get("recent_form_14d"),
                "ev_10":              sig.get("ev_10"),
                "kelly_size":         sig.get("kelly_size"),
                "value_edge":         sig.get("value_edge"),
                "pinnacle_odds":      sig.get("pinnacle_odds"),
                "best_odds":          sig.get("best_odds"),
                "best_book":          sig.get("best_book"),
            },
        }
        players.append(entry)

    return _json.dumps({"date": today, "players": players}, default=str)


def _stat(label: str, value, suffix: str = "", fmt: str = "") -> str:
    if value is None:
        return ""
    text = f"{value:{fmt}}{suffix}" if fmt else f"{value}{suffix}"
    return (
        f'<div class="stat">'
        f'<span class="stat-label">{_esc(label)}</span>'
        f'<span class="stat-value">{_esc(text)}</span>'
        f'</div>'
    )


def _star_count(stars_str: str) -> int:
    return (stars_str or "").count("★")


def _star_html(stars_str: str) -> str:
    if not stars_str:
        return ""
    filled = stars_str.count("★")
    empty  = stars_str.count("☆")
    return (
        '<span class="stars">'
        + '<span class="star-filled">' + "★" * filled + "</span>"
        + '<span class="star-empty">'  + "☆" * empty  + "</span>"
        + "</span>"
    )


def _confidence_class(conf: str) -> str:
    return {"HIGH": "conf-high", "MEDIUM": "conf-med", "LOW": "conf-low"}.get(
        (conf or "").upper(), "conf-low"
    )


def _bucket_label(n: int) -> str:
    return {
        5: "Elite Dingers",
        4: "Strong Plays",
        3: "Solid Looks",
        2: "Worth Watching",
        1: "Speculative",
        0: "Low Confidence",
    }.get(n, "Other")


def _build_card(rank: int, pick: dict) -> str:
    player    = pick.get("player", "Unknown")
    matchup   = pick.get("matchup", "")
    conf      = pick.get("confidence", "LOW")
    score     = pick.get("score", 0)
    reasoning = pick.get("reasoning", "")
    stars_str = pick.get("stars", "")
    sig       = pick.get("signals", {})

    status    = sig.get("status", "")
    venue     = sig.get("venue", "")
    is_home   = sig.get("is_home")
    platoon   = sig.get("platoon", "")
    pitcher   = sig.get("pitcher_name", "TBD")
    p_throws  = sig.get("pitcher_throws", "?")
    bat_side  = sig.get("bat_side", "?")
    bat_order = sig.get("batting_order")
    season_hr = sig.get("season_hr")
    pa        = sig.get("pa")

    barrel    = sig.get("barrel_rate")
    hh        = sig.get("hard_hit_pct")
    xiso      = sig.get("xiso")
    ev_avg    = sig.get("ev_avg")
    sweet     = sig.get("sweet_spot_pct")
    fb_pct    = sig.get("fb_pct")
    p_hr9     = sig.get("pitcher_hr_per_9")
    form      = sig.get("recent_form_14d")
    park_hr   = sig.get("park_hr_factor")
    temp_f    = sig.get("temp_f")
    wind_mph  = sig.get("wind_mph")
    wind_dir  = sig.get("wind_direction_bpp")   # "in", "out", "cross", or None
    bpp_rank  = sig.get("bpp_proj_rank")
    ev_10     = sig.get("ev_10")
    h2h_hr    = sig.get("h2h_hr")
    h2h_ab    = sig.get("h2h_ab")

    home_away_str = "Home" if is_home else "Away"

    # Parse game_time into readable ET time
    _game_time_str = ""
    _gt = sig.get("game_time") or ""
    if _gt:
        try:
            from datetime import datetime as _dt
            from zoneinfo import ZoneInfo
            _utc = _dt.fromisoformat(_gt.replace("Z", "+00:00"))
            _et  = _utc.astimezone(ZoneInfo("America/New_York"))
            _game_time_str = _et.strftime("%-I:%M %p ET")
        except Exception:
            _game_time_str = _gt[11:16]
    waiting_badge = '<span class="badge-waiting">LINEUP PENDING</span>' if status == "waiting" else ""
    conf_class    = _confidence_class(conf)

    # Tags
    park_html = ""
    if park_hr is not None:
        if park_hr >= 110:
            park_html = f'<span class="tag tag-green">Park {park_hr:.0f}%</span>'
        elif park_hr <= 90:
            park_html = f'<span class="tag tag-red">Park {park_hr:.0f}%</span>'
        else:
            park_html = f'<span class="tag tag-dim">Park {park_hr:.0f}%</span>'

    weather_tags = ""
    if temp_f is not None:
        cls = "tag-green" if temp_f >= 80 else ("tag-red" if temp_f <= 50 else "tag-dim")
        weather_tags += f'<span class="tag {cls}">{temp_f:.0f}°F</span>'
    if wind_mph is not None and wind_mph >= 1:
        dir_label = f" ({wind_dir})" if wind_dir else ""
        wind_cls  = "tag-green" if wind_dir == "out" else ("tag-red" if wind_dir == "in" else "tag-dim")
        weather_tags += f'<span class="tag {wind_cls}">Wind {wind_mph:.0f}mph{dir_label}</span>'

    form_html = ""
    if form and form >= 1:
        form_html = f'<span class="tag tag-amber">{form}HR / 14d</span>'

    pitcher_html = ""
    if p_hr9 is not None:
        cls = "tag-red" if p_hr9 >= 2 else ("tag-amber" if p_hr9 >= 1 else "tag-dim")
        pitcher_html = f'<span class="tag {cls}">Pitcher L3: {p_hr9:.1f} HR/9</span>'

    h2h_html = ""
    if h2h_hr is not None and h2h_hr >= 1:
        h2h_html = f'<span class="tag tag-green">H2H {h2h_hr}HR/{h2h_ab or "—"}AB</span>'

    ev_html   = ""
    odds_html = ""

    pa_html = ""
    if pa is not None and pa < 40:
        pa_html = f'<span class="tag tag-warn">{pa} PA — small sample</span>'

    score_class = "score-high" if score >= 18 else ("score-mid" if score >= 14 else "score-low")

    # Line 1: matchup + time
    matchup_line = f"{_esc(matchup)}"
    if _game_time_str:
        matchup_line += f" &nbsp;·&nbsp; <strong>{_esc(_game_time_str)}</strong>"
    # Line 2: venue + home/away + order
    venue_line = ""
    if venue:
        venue_line += _esc(venue)
    venue_line += f" &nbsp;·&nbsp; {home_away_str}"

    pitcher_line = f"{_esc(bat_side)}HB vs {_esc(pitcher)} ({_esc(p_throws)})"
    if bat_order:
        pitcher_line += f" &nbsp;·&nbsp; #{bat_order} in order"

    stats_row1 = ""
    stats_row2 = ""
    if xiso is not None:
        stats_row1 += _stat("xISO", xiso, fmt=".3f")
    if barrel is not None:
        stats_row1 += _stat("Barrel", barrel, suffix="%", fmt=".1f")
    if hh is not None:
        stats_row1 += _stat("Hard Hit", hh, suffix="%", fmt=".1f")
    if ev_avg is not None:
        stats_row2 += _stat("EV Avg", ev_avg, suffix=" mph", fmt=".1f")
    if sweet is not None:
        stats_row2 += _stat("Sweet Sp", sweet, suffix="%", fmt=".1f")
    if fb_pct is not None:
        stats_row2 += _stat("FB%", fb_pct, suffix="%", fmt=".1f")
    if season_hr is not None:
        stats_row2 += _stat("Season HR", season_hr)

    stats_html = ""
    if stats_row1:
        stats_html += f'<div class="stats-row">{stats_row1}</div>'
    if stats_row2:
        stats_html += f'<div class="stats-row">{stats_row2}</div>'

    tags_html = park_html + weather_tags + form_html + pitcher_html + h2h_html + pa_html

    delay = (rank - 1) * 0.04
    slug  = _player_slug(player)

    return f"""
        <a class="pick-card" href="player-card.html?player={slug}" style="animation-delay:{delay:.2f}s">
            <div class="card-rank">
                <span class="rank-num">#{rank}</span>
                {_star_html(stars_str)}
                {waiting_badge}
            </div>
            <div class="card-body">
                <div class="player-row">
                    <span class="player-name">{_esc(player)}</span>
                    <span class="score-badge {score_class}">{score:.1f}</span>
                </div>
                <div class="matchup-line">{matchup_line}</div>
                <div class="venue-line">{venue_line}</div>
                <div class="pitcher-line">{pitcher_line}</div>
                {stats_html}
                <div class="tags-row">{tags_html}</div>
                {odds_html}
                <div class="why-line"><span class="why-label">Why:</span> {_esc(reasoning)}</div>
            </div>
        </a>"""


def _build_best_bets_html(best_bets: list[dict]) -> str:
    if not best_bets:
        return ""
    cards = []
    for i, p in enumerate(best_bets, 1):
        sig   = p.get("signals", {}) or {}
        ev    = sig.get("ev_10")
        pin   = sig.get("pinnacle_odds")
        name  = _esc(p.get("player", "Unknown"))
        stars = _star_html(p.get("stars", ""))
        matchup = _esc(p.get("matchup", ""))
        overall_rank = p.get("rank") or i

        if ev is not None:
            if pin:
                ev_html = f'<span class="bb-ev bb-ev-confirmed">${ev:+.2f}</span>'
                ev_tip  = "Pinnacle-anchored EV"
            else:
                ev_html = f'<span class="bb-ev bb-ev-est">~${ev:+.2f}</span>'
                ev_tip  = "Consensus EV (no Pinnacle)"
        else:
            ev_html = f'<span class="bb-ev bb-ev-model">est.</span>'
            ev_tip  = "No odds — ranked by model score"

        cards.append(f"""<div class="bb-card" title="{_esc(ev_tip)}">
  <div class="bb-rank">#{i}</div>
  <div class="bb-name">{name}</div>
  <div class="bb-matchup">{matchup}</div>
  <div class="bb-stars">{stars}</div>
  <div class="bb-ev-row">{ev_html}<span class="bb-overall">#{overall_rank} overall</span></div>
</div>""")

    cards_html = "\n".join(cards)
    return f"""<section class="best-bets-section">
  <div class="bb-header">
    <span class="bb-title">Best Bets</span>
    <span class="bb-sub">Top 7 by Expected Value — ranked by EV edge over the market</span>
  </div>
  <div class="bb-grid">
{cards_html}
  </div>
</section>"""


def generate_picks_html(
    picks: list[dict],
    today: str,
    auc: float = 0.0,
    ml_influence: float = 0.0,
    win_rate: str = "—",
    record: str = "—",
    model_yesterday_record: tuple | None = None,
    model_days_tracked: int | None = None,
    streak: str | None = None,
    group_data: dict | None = None,
    tier_hit_rates: dict | None = None,
    version: str = "",
    best_bets: list[dict] | None = None,
) -> str:
    # Layout: compact 5-card EV Plays grid at top, then star-bucket sections below.

    def _tier_header_html(label: str, subtitle: str, n: int, star_n: int | None) -> str:
        hit_rate_html = ""
        if star_n is not None and tier_hit_rates:
            n_picks, n_homers = tier_hit_rates.get(star_n, (0, 0))
            if n_picks > 0:
                rate = n_homers / n_picks * 100
                hit_rate_html = (
                    f'<span class="tier-hit-rate">'
                    f'{rate:.0f}% HR rate'
                    f'<span class="tier-hit-count"> ({n_picks} picks)</span>'
                    f'</span>'
                )
        return (
            f'<div class="tier-header">'
            f'<span class="tier-label">{_esc(label)}</span>'
            f'<span class="tier-subtitle">{_esc(subtitle)}</span>'
            f'<span class="tier-count">{n} pick{"s" if n != 1 else ""}</span>'
            f'{hit_rate_html}'
            f'<div class="tier-rule"></div>'
            f'</div>'
        )

    # ── EV Plays compact grid ─────────────────────────────────────────────────
    gd_bb = (group_data or {}).get("best_bets")
    ev_hit_html = ""
    if gd_bb:
        n_picks, n_homers = gd_bb.get("hit_rate", (0, 0))
        if n_picks > 0:
            rate = n_homers / n_picks * 100
            ev_hit_html = (
                f'<span class="tier-hit-rate">'
                f'{rate:.0f}% HR rate'
                f'<span class="tier-hit-count"> ({n_picks} picks)</span>'
                f'</span>'
            )

    ev_cards_html = ""
    for i, p in enumerate(best_bets or [], 1):
        sig        = p.get("signals", {}) or {}
        ev         = sig.get("ev_10")
        pin        = sig.get("pinnacle_odds")
        name       = _esc(p.get("player", "Unknown"))
        stars      = _star_html(p.get("stars", ""))
        matchup    = _esc(p.get("matchup", ""))
        overall_rk = p.get("rank") or i

        ev_cards_html += f"""<div class="ev-card">
  <div class="ev-card-rank">EV #{i}</div>
  <div class="ev-card-name">{name}</div>
  <div class="ev-card-matchup">{matchup}</div>
  <div class="ev-card-stars">{stars}</div>
</div>"""

    ev_section_html = f"""<section class="ev-plays-section">
  <div class="ev-section-header">
    <span class="ev-section-title">Top EV Plays</span>
    <span class="ev-section-sub">Top 5 by expected value</span>
    {ev_hit_html}
    <div class="ev-section-rule"></div>
  </div>
  <div class="ev-grid">
{ev_cards_html}
  </div>
</section>"""

    # ── Star-bucket sections ──────────────────────────────────────────────────
    _BUCKET_LABELS = {
        5: ("Elite Dingers",  "Top-of-pool, elite model score"),
        4: ("Strong Plays",   "High-confidence model picks"),
        3: ("Solid Looks",    "Good signals across the board"),
        2: ("Worth Watching", "Moderate edge, worth a look"),
        1: ("Speculative",    "Lower confidence, long-shot value"),
        0: ("Low Confidence", "Minimal edge"),
    }

    # Group picks by star count, preserving model rank order within each bucket
    buckets: dict[int, list[tuple[int, dict]]] = {}
    for rank_i, p in enumerate(picks, 1):
        sc = _star_count(p.get("stars", ""))
        buckets.setdefault(sc, []).append((rank_i, p))

    bucket_sections = []
    for sc in sorted(buckets.keys(), reverse=True):
        items = buckets[sc]
        label, subtitle = _BUCKET_LABELS.get(sc, (f"{sc}★", ""))
        header = _tier_header_html(label, subtitle, len(items), sc)
        cards  = "".join(_build_card(rank_i, p) for rank_i, p in items)
        bucket_sections.append(f"""
    <section class="tier-section">
        {header}
        <div class="picks-grid">
{cards}
        </div>
    </section>""")

    sections_html = ev_section_html + "\n".join(bucket_sections)

    auc_str = f"{auc:.3f}" if auc else "—"
    ml_str  = f"{ml_influence * 100:.0f}%" if ml_influence else "—"

    # Model stats tile (hit rate hero + sub-stats including streak)
    if win_rate and win_rate != "—":
        days_fmt = f"{model_days_tracked}" if model_days_tracked else "—"

        if model_yesterday_record:
            yest_wins, yest_picks = model_yesterday_record
            yest_pct = f"{yest_wins / yest_picks * 100:.0f}%" if yest_picks else "—"
            yest_html = f'{yest_wins}/{yest_picks} ({yest_pct})'
        else:
            yest_html = "—"

        if streak:
            is_win = streak.endswith("W")
            streak_color = "#4ADE80" if is_win else "#F87171"
            streak_count = streak[:-1]
            streak_type_label = "WIN" if is_win else "LOSS"
            streak_item = f"""<div class="stats-tile-item">
      <div class="sti-label">Current Streak</div>
      <div class="sti-value" style="color:{streak_color}">{_esc(streak_count)} {streak_type_label}</div>
      <div class="sti-sub">consecutive days ≥20%</div>
    </div>"""
        else:
            streak_item = ""

        model_stats_tile = f"""<div class="model-stats-tile">
  <div class="stats-tile-roi">
    <div class="roi-value">{_esc(win_rate)}</div>
    <div class="roi-label">Hit Rate</div>
  </div>
  <div class="stats-tile-items">
    <div class="stats-tile-item">
      <div class="sti-label">Season Record</div>
      <div class="sti-value">{_esc(record)}</div>
      <div class="sti-sub">picks homered since Apr 16</div>
    </div>
    <div class="stats-tile-item">
      <div class="sti-label">Yesterday</div>
      <div class="sti-value">{_esc(yest_html)}</div>
      <div class="sti-sub">homers / picks (rate)</div>
    </div>
    <div class="stats-tile-item">
      <div class="sti-label">Model AUC</div>
      <div class="sti-value">{_esc(auc_str)}</div>
      <div class="sti-sub">ML weight {_esc(ml_str)}</div>
    </div>
    <div class="stats-tile-item">
      <div class="sti-label">Days Tracked</div>
      <div class="sti-value">{_esc(days_fmt)}</div>
      <div class="sti-sub">labeled results</div>
    </div>
    {streak_item}
  </div>
</div>"""
    else:
        model_stats_tile = ""

    streak_tile = ""  # removed — streak now lives inside model_stats_tile

    return f"""<!DOCTYPE html>
<html lang="en" data-version="{_esc(version)}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dingers Hotline</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Oswald:wght@400;500;600;700&family=Source+Serif+4:wght@400;600&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:         #FAFAF7;
    --surface:    #FFFFFF;
    --surface2:   #F3F2EE;
    --border:     #E2DED6;
    --border-dark:#C8C2B8;
    --navy:       #1B2A4A;
    --navy-mid:   #2D4070;
    --red:        #C8102E;
    --red-dim:    #F9E5E8;
    --gold:       #D4A017;
    --gold-dim:   #FDF5DC;
    --green:      #1A6B3C;
    --green-dim:  #E4F2EB;
    --amber:      #B45309;
    --amber-dim:  #FEF3C7;
    --text:       #1A1A1A;
    --text-sub:   #6B6560;
    --text-dim:   #A8A29E;
    --grass:      #2D5A27;
  }}

  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'Source Serif 4', Georgia, serif;
    font-size: 14px;
    line-height: 1.5;
    min-height: 100vh;
  }}

  /* ─── Pinstripe header ─── */
  .site-header {{
    background: var(--navy);
    background-image: repeating-linear-gradient(
      90deg,
      transparent,
      transparent 47px,
      rgba(255,255,255,0.04) 47px,
      rgba(255,255,255,0.04) 48px
    );
    color: #fff;
    padding: 28px 36px 24px;
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 20px;
    border-bottom: 4px solid var(--red);
  }}

  .header-left {{
    display: flex;
    flex-direction: column;
    gap: 6px;
  }}

  .tg-join {{
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 6px;
    text-align: right;
  }}
  .tg-join-label {{
    font-size: 0.78rem;
    color: rgba(255,255,255,0.70);
    line-height: 1.35;
    max-width: 220px;
  }}
  .tg-join-btn {{
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: #229ED9;
    color: #fff;
    font-weight: 700;
    font-size: 0.88rem;
    padding: 10px 18px;
    border-radius: 8px;
    text-decoration: none;
    white-space: nowrap;
    transition: background 0.15s;
  }}
  .tg-join-btn:hover {{ background: #1a8bbf; }}
  .tg-join-btn svg {{ flex-shrink: 0; }}

  .site-title {{
    font-family: 'Oswald', sans-serif;
    font-weight: 700;
    font-size: clamp(30px, 5vw, 52px);
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: #FFFFFF;
    line-height: 1;
    display: flex;
    align-items: center;
    gap: 12px;
  }}

  .title-ball {{
    display: inline-block;
    width: 0.85em;
    height: 0.85em;
    flex-shrink: 0;
    opacity: 0.9;
  }}

  .site-date {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    color: rgba(255,255,255,0.55);
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }}

  /* ─── Model chips ─── */
  .model-chips {{
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    align-items: center;
  }}

  .nav-link {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: rgba(255,255,255,0.10);
    color: #fff;
    font-family: 'Oswald', sans-serif;
    font-weight: 600;
    font-size: 13px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    text-decoration: none;
    padding: 8px 16px;
    border-radius: 6px;
    border: 1px solid rgba(255,255,255,0.18);
    white-space: nowrap;
    transition: background 0.15s;
  }}
  .nav-link:hover {{ background: rgba(255,255,255,0.18); }}

  .chip {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    padding: 5px 12px;
    border-radius: 3px;
    border: 1px solid rgba(255,255,255,0.2);
    background: rgba(255,255,255,0.08);
    color: rgba(255,255,255,0.7);
    white-space: nowrap;
  }}
  .chip.chip-auc {{ color: #FBBF24; border-color: rgba(251,191,36,0.4); }}
  .chip-since {{ font-size: 9px; opacity: 0.55; font-weight: 400; }}

  /* ─── Model stats tile ─── */
  .model-stats-tile {{
    margin: 28px 36px 28px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    display: flex;
    overflow: hidden;
  }}
  .stats-tile-roi {{
    background: var(--navy);
    color: #fff;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 20px 28px;
    min-width: 110px;
    flex-shrink: 0;
  }}
  .stats-tile-roi .roi-value {{
    font-family: 'Oswald', sans-serif;
    font-size: 2rem;
    font-weight: 700;
    line-height: 1;
    letter-spacing: -0.5px;
  }}
  .stats-tile-roi .roi-label {{
    font-size: 0.6rem;
    text-transform: uppercase;
    letter-spacing: 1px;
    opacity: 0.6;
    margin-top: 4px;
  }}
  .stats-tile-items {{
    display: flex;
    flex: 1;
    border-left: 1px solid var(--border);
  }}
  .stats-tile-item {{
    flex: 1;
    display: flex;
    flex-direction: column;
    justify-content: center;
    padding: 16px 20px;
    border-right: 1px solid var(--border);
  }}
  .stats-tile-item:last-child {{ border-right: none; }}
  .stats-tile-item .sti-label {{
    font-size: 0.6rem;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--text-dim);
    margin-bottom: 4px;
  }}
  .stats-tile-item .sti-value {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 1rem;
    font-weight: 700;
    color: var(--text);
  }}
  .stats-tile-item .sti-sub {{
    font-size: 0.65rem;
    color: var(--text-sub);
    margin-top: 2px;
  }}
  @media (max-width: 600px) {{
    .model-stats-tile {{ flex-direction: column; margin: 0 16px 20px; }}
    .stats-tile-items {{ flex-direction: column; border-left: none; border-top: 1px solid var(--border); }}
    .stats-tile-item {{ border-right: none; border-bottom: 1px solid var(--border); }}
    .stats-tile-item:last-child {{ border-bottom: none; }}
  }}

  /* ─── Streak tile ─── */
  .streak-tile {{
    margin: 0 36px 28px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    display: flex;
    align-items: center;
    padding: 18px 28px;
    gap: 20px;
  }}
  .streak-label {{
    font-size: 0.6rem;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--text-dim);
    white-space: nowrap;
  }}
  .streak-value {{
    font-family: 'Oswald', sans-serif;
    font-size: 2.2rem;
    font-weight: 700;
    line-height: 1;
    letter-spacing: -0.5px;
  }}
  .streak-type {{
    font-family: 'Oswald', sans-serif;
    font-size: 0.75rem;
    font-weight: 600;
    letter-spacing: 2px;
    text-transform: uppercase;
    margin-top: -4px;
  }}
  .streak-desc {{
    font-size: 0.75rem;
    color: var(--text-sub);
  }}
  @media (max-width: 600px) {{
    .streak-tile {{ margin: 0 16px 20px; padding: 16px 20px; }}
  }}

  /* ─── Tier section ─── */
  .tier-section {{
    padding: 28px 36px 8px;
  }}

  .tier-section-accent {{
    border-left: 3px solid var(--gold);
    padding-left: 33px;
    margin-left: 12px;
  }}

  .tier-header {{
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 16px;
  }}

  .tier-header-accent .tier-label {{ color: var(--gold); }}

  .star-filled {{ color: var(--gold); }}
  .star-empty  {{ color: var(--border-dark); }}

  .tier-label {{
    font-family: 'Oswald', sans-serif;
    font-weight: 600;
    font-size: 15px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--navy);
    white-space: nowrap;
  }}

  .tier-subtitle {{
    font-size: 11px;
    color: var(--text-sub);
    white-space: nowrap;
  }}

  .tier-count {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    color: var(--text-dim);
    white-space: nowrap;
  }}

  .tier-hit-rate {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    font-weight: 700;
    color: var(--green);
    background: var(--green-dim);
    border: 1px solid #A7D7B8;
    border-radius: 3px;
    padding: 2px 8px;
    white-space: nowrap;
  }}

  .tier-hit-count {{
    font-weight: 400;
    color: var(--text-dim);
    font-size: 10px;
  }}

  .tier-no-history {{
    color: var(--text-dim);
    background: var(--surface2);
    border-color: var(--border);
    font-weight: 400;
  }}

  .tier-rule {{
    flex: 1;
    height: 1px;
    background: var(--border);
  }}

  /* ─── Cards grid ─── */
  .picks-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(100%, 560px), 1fr));
    gap: 10px;
    margin-bottom: 20px;
  }}

  /* ─── Card ─── */
  .pick-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 16px;
    display: flex;
    gap: 14px;
    opacity: 0;
    transform: translateY(10px);
    animation: reveal 0.35s ease forwards;
    transition: box-shadow 0.15s, border-color 0.15s;
    text-decoration: none;
    color: inherit;
    cursor: pointer;
  }}

  @keyframes reveal {{
    to {{ opacity: 1; transform: translateY(0); }}
  }}

  .pick-card:hover {{
    border-color: var(--navy);
    box-shadow: 0 2px 12px rgba(27,42,74,0.10);
  }}
  .pick-card:hover .player-name {{ color: var(--navy-mid); }}

  /* ─── Rank column ─── */
  .card-rank {{
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 5px;
    min-width: 40px;
    padding-top: 2px;
  }}

  .rank-num {{
    font-family: 'Oswald', sans-serif;
    font-weight: 700;
    font-size: 24px;
    line-height: 1;
    color: var(--navy);
  }}

  .stars {{
    font-size: 10px;
    letter-spacing: 1px;
    line-height: 1;
  }}

  .badge-waiting {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 7px;
    letter-spacing: 0.04em;
    color: var(--amber);
    border: 1px solid #D97706;
    background: var(--amber-dim);
    padding: 2px 4px;
    border-radius: 2px;
    text-align: center;
    line-height: 1.4;
    white-space: nowrap;
    margin-top: 2px;
  }}

  /* ─── Card body ─── */
  .card-body {{
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }}

  .player-row {{
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }}

  .player-name {{
    font-family: 'Oswald', sans-serif;
    font-weight: 600;
    font-size: 19px;
    color: var(--navy);
    letter-spacing: 0.02em;
    flex: 1;
    min-width: 0;
  }}

  .conf-badge {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.1em;
    padding: 2px 7px;
    border-radius: 2px;
    text-transform: uppercase;
  }}

  .conf-high {{ background: var(--green-dim);  color: var(--green);  border: 1px solid #A7D7B8; }}
  .conf-med  {{ background: var(--amber-dim);  color: var(--amber);  border: 1px solid #FCD34D; }}
  .conf-low  {{ background: var(--surface2);   color: var(--text-dim); border: 1px solid var(--border); }}

  .score-badge {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    font-weight: 700;
    padding: 2px 7px;
    border-radius: 2px;
  }}

  .score-high {{ color: var(--red);     background: var(--red-dim);    border: 1px solid #F9A8B4; }}
  .score-mid  {{ color: var(--navy);    background: #EEF1F8;           border: 1px solid #C5CDE8; }}
  .score-low  {{ color: var(--text-sub); background: var(--surface2);  border: 1px solid var(--border); }}

  .matchup-line {{
    font-size: 12px;
    color: var(--text-main);
    font-family: 'Source Serif 4', serif;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .venue-line {{
    font-size: 11px;
    color: var(--text-sub);
    font-family: 'Source Serif 4', serif;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin-top: 1px;
  }}

  .pitcher-line {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    color: var(--text-sub);
  }}

  /* ─── Stats rows ─── */
  .stats-row {{
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
  }}

  .stat {{
    display: flex;
    flex-direction: column;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 4px 9px;
    min-width: 64px;
  }}

  .stat-label {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 8px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--text-dim);
    line-height: 1;
    margin-bottom: 2px;
  }}

  .stat-value {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px;
    font-weight: 700;
    color: var(--navy);
    line-height: 1;
  }}

  /* ─── Tags ─── */
  .tags-row {{
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }}

  .tag {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    font-weight: 500;
    padding: 2px 7px;
    border-radius: 3px;
    border: 1px solid transparent;
    letter-spacing: 0.02em;
  }}

  .tag-green {{ color: var(--green);  background: var(--green-dim);  border-color: #A7D7B8; }}
  .tag-red   {{ color: var(--red);    background: var(--red-dim);    border-color: #F9A8B4; }}
  .tag-amber {{ color: var(--amber);  background: var(--amber-dim);  border-color: #FCD34D; }}
  .tag-dim   {{ color: var(--text-sub); background: var(--surface2); border-color: var(--border); }}
  .tag-warn  {{ color: #92400E;       background: #FEF3C7;           border-color: #FCD34D; }}

  /* ─── Why line ─── */
  .why-line {{
    font-size: 12px;
    color: var(--text-sub);
    line-height: 1.4;
    font-family: 'Source Serif 4', serif;
    font-style: italic;
  }}

  .why-label {{
    font-family: 'JetBrains Mono', monospace;
    font-weight: 700;
    font-size: 9px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--text-dim);
    margin-right: 4px;
    font-style: normal;
  }}

  .odds-line {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    color: var(--text-sub);
    margin-top: 6px;
  }}
  .odds-best {{
    color: #4ADE80;
    font-weight: 700;
  }}
  .odds-book {{
    color: var(--text-dim);
    font-size: 10px;
  }}
  .odds-pin {{
    color: var(--text-dim);
    font-size: 10px;
  }}

  /* ─── Footer ─── */
  .site-footer {{
    background: var(--navy);
    border-top: 3px solid var(--red);
    padding: 14px 36px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    color: rgba(255,255,255,0.4);
    letter-spacing: 0.06em;
    display: flex;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 24px;
  }}
  .disclaimer {{
    width: 100%;
    font-size: 9px;
    color: rgba(255,255,255,0.25);
    letter-spacing: 0.04em;
    text-align: center;
    margin-top: 4px;
  }}

  /* ─── EV Plays strip ─── */
  .ev-plays-section {{
    padding: 24px 36px 8px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 4px;
  }}
  .ev-section-header {{
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 14px;
    flex-wrap: wrap;
  }}
  .ev-section-title {{
    font-family: 'Oswald', sans-serif;
    font-weight: 700;
    font-size: 15px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--gold);
    white-space: nowrap;
  }}
  .ev-section-sub {{
    font-size: 11px;
    color: var(--text-sub);
    white-space: nowrap;
  }}
  .ev-section-rule {{
    flex: 1;
    height: 1px;
    background: var(--border);
  }}
  .ev-grid {{
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 10px;
  }}
  .ev-card {{
    background: var(--surface);
    border: 1.5px solid var(--gold);
    border-radius: 8px;
    padding: 10px 12px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    cursor: default;
    transition: box-shadow 0.15s;
    min-width: 0;
  }}
  .ev-card:hover {{ box-shadow: 0 2px 10px rgba(212,160,23,0.25); }}
  .ev-card-rank {{
    font-size: 0.65rem; font-weight: 700; color: var(--gold);
    text-transform: uppercase; letter-spacing: 0.06em;
  }}
  .ev-card-name {{
    font-family: 'Oswald', sans-serif; font-size: 0.88rem; font-weight: 600;
    color: var(--navy); line-height: 1.2;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }}
  .ev-card-matchup {{
    font-size: 0.65rem; color: var(--text-sub);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }}
  .ev-card-stars {{ font-size: 0.75rem; }}
  .ev-card-ev-row {{
    display: flex; align-items: center; gap: 6px; margin-top: 2px;
  }}
  .ev-card-ev {{
    font-family: 'JetBrains Mono', monospace; font-size: 0.78rem; font-weight: 700;
  }}
  .ev-confirmed {{ color: var(--green); }}
  .ev-est       {{ color: var(--amber); }}
  .ev-model     {{ color: var(--text-dim); }}
  .ev-card-overall {{
    font-size: 0.62rem; color: var(--text-dim);
  }}
  @media (max-width: 700px) {{
    .ev-plays-section {{ padding: 16px 16px 8px; }}
    .ev-grid {{ grid-template-columns: repeat(3, 1fr); }}
  }}
  @media (max-width: 480px) {{
    .ev-grid {{ grid-template-columns: repeat(2, 1fr); }}
  }}

  /* ─── Responsive ─── */
  @media (max-width: 600px) {{
    .site-header   {{ padding: 18px; }}
    .tier-section          {{ padding: 20px 16px 4px; }}
    .tier-section-accent   {{ padding-left: 13px; margin-left: 4px; }}
    .picks-grid    {{ gap: 8px; }}
    .site-footer   {{ padding: 12px 16px; }}
    .stat          {{ min-width: 54px; }}
    .model-stats-tile {{ margin: 0 16px 20px; }}
  }}
</style>
</head>
<body>

<header class="site-header">
  <div class="header-left">
    <div class="site-title">
      <svg class="title-ball" fill="#ffffff" viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg"><g><path d="M455.857,56.144c-74.86-74.859-196.662-74.859-271.521,0C17.087,223.392-9.275,272.783,2.398,298.264c8.318,18.153,32.898,19.077,63.015,17.249l-36.537,97.203c-6.838,18.194-2.549,38.035,11.195,51.778c13.744,13.743,33.583,18.035,51.778,11.195L197.2,436.089c-2.507,34.987-3.349,64.4,16.534,73.511c3.325,1.524,7.055,2.4,11.403,2.4c28.973-0.002,85.294-38.91,230.72-184.335C530.715,252.806,530.715,131.003,455.857,56.144z M441.431,313.239C369.446,385.224,316.37,433.95,279.174,462.2c-41.868,31.797-54.167,30.124-56.94,28.854c-2.851-1.307-4.901-7.374-5.626-16.646c-0.922-11.81,0.244-27.541,1.479-44.196c0.209-2.826,0.421-5.681,0.625-8.557c0.247-3.466-1.288-6.82-4.073-8.899c-1.788-1.335-3.934-2.026-6.102-2.026c-1.209,0-2.424,0.214-3.589,0.652l-120.277,45.21c-10.765,4.047-22.043,1.608-30.174-6.524c-8.131-8.131-10.57-19.411-6.524-30.174l42.126-112.072c1.224-3.258,0.703-6.915-1.382-9.702c-2.085-2.787-5.444-4.321-8.919-4.06c-14.536,1.076-31.012,2.295-42.857,1.278c-8.892-0.763-14.721-2.793-15.994-5.572c-1.27-2.772-2.944-15.072,28.855-56.941c28.25-37.196,76.975-90.271,148.96-162.255c66.904-66.905,175.764-66.905,242.669,0C508.335,137.474,508.335,246.335,441.431,313.239z"/></g><g><path d="M320.096,28.297c-90.213,0-163.608,73.394-163.608,163.608s73.395,163.608,163.608,163.608s163.608-73.395,163.608-163.608S410.31,28.297,320.096,28.297z M320.096,48.698c36.338,0,69.551,13.613,94.828,35.995c-26.187,23.225-59.477,35.903-94.828,35.903c-35.351,0-68.641-12.679-94.828-35.903C250.544,62.309,283.758,48.698,320.096,48.698z M320.096,335.111c-36.338,0.001-69.552-13.611-94.829-35.995c26.187-23.225,59.478-35.903,94.829-35.903c35.351,0,68.641,12.679,94.828,35.903C389.647,321.499,356.433,335.111,320.096,335.111z M429.215,284.535c-30.034-26.977-68.377-41.722-109.12-41.722c-40.743,0-79.086,14.745-109.12,41.722c-21.246-24.99-34.087-57.336-34.087-92.631c0.001-35.293,12.842-67.64,34.088-92.63c30.034,26.977,68.377,41.722,109.12,41.722s79.086-14.745,109.119-41.722c21.246,24.99,34.087,57.336,34.087,92.63C463.302,227.198,450.46,259.545,429.215,284.535z"/></g></svg>
      Dingers Hotline
    </div>
    <div class="site-date">Latest Update: {_esc(today)} &nbsp;·&nbsp; {len(picks)} Picks</div>
  </div>
  <div class="model-chips">
    <a class="nav-link" href="pick-of-the-day.html">Pick of Day ★</a>
    <a class="nav-link" href="leaderboard.html">HR Leaders →</a>
    <a class="nav-link" href="hit-rate.html">Hit Rate 📅</a>
  </div>
  <div class="tg-join">
    <div class="tg-join-label">Get notified the moment today's picks are ready — join the free Telegram channel.</div>
    <a class="tg-join-btn" href="https://t.me/+BHJ6UMUkhyoxNzEx" target="_blank" rel="noopener">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="white" xmlns="http://www.w3.org/2000/svg"><path d="M12 0C5.373 0 0 5.373 0 12s5.373 12 12 12 12-5.373 12-12S18.627 0 12 0zm5.894 8.221-1.97 9.28c-.145.658-.537.818-1.084.508l-3-2.21-1.447 1.394c-.16.16-.295.295-.605.295l.213-3.053 5.56-5.023c.242-.213-.054-.333-.373-.12L7.26 13.835l-2.938-.916c-.638-.203-.651-.638.136-.944l11.438-4.41c.532-.194.997.131.998.656z"/></svg>
      Join Dingers Hotline on Telegram
    </a>
  </div>
</header>

{model_stats_tile}
{streak_tile}

{sections_html}

<footer class="site-footer">
  <span>Dingers Hotline</span>
  <span>Generated {_esc(today)} &nbsp;·&nbsp; Model AUC {_esc(auc_str)}</span>
  <div class="disclaimer">Must be 21+ and present in a legal sports wagering state. Gambling involves risk. Please gamble responsibly. If you or someone you know has a gambling problem, call or text <strong>1-800-GAMBLER</strong>.</div>
</footer>

</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Season HR Leaderboard page
# ─────────────────────────────────────────────────────────────────────────────

def generate_leaderboard_html(today_str: str | None = None) -> str:
    """Fetch Statcast batter leaderboard, sort by HR, return leaderboard.html."""
    import csv
    import io
    import requests
    from datetime import date
    from pathlib import Path

    today_str = today_str or date.today().isoformat()
    season    = date.today().year

    # ── Load Statcast CSV (prefer today's cache) ──────────────────────────────
    cache_path = Path(__file__).parent.parent / "cache" / f"statcast_batter_{today_str}.csv"
    text = None
    if cache_path.exists():
        text = cache_path.read_text(encoding="utf-8").lstrip("﻿")
    else:
        url = (
            f"https://baseballsavant.mlb.com/leaderboard/custom"
            f"?year={season}&type=batter&filter=&sort=4&sortDir=desc&min=5"
            f"&selections=pa,barrel_batted_rate,exit_velocity_avg,xiso,home_run"
            f"&chart=false&r=no&exactNameSearch=false&csv=true"
        )
        try:
            resp = requests.get(url, timeout=20)
            text = resp.text.lstrip("﻿")
        except Exception:
            text = ""

    rows: list[dict] = []
    if text:
        try:
            rows = list(csv.DictReader(io.StringIO(text)))
        except Exception:
            rows = []

    # ── Sort by HR, take top 50 ───────────────────────────────────────────────
    def _hr(r):
        try:
            return int(r.get("home_run") or 0)
        except ValueError:
            return 0

    rows_sorted = sorted(rows, key=_hr, reverse=True)[:50]

    # ── Batch-fetch current team from MLB Stats API ───────────────────────────
    _TEAM_ABBREV = {
        108: "LAA", 109: "AZ",  110: "BAL", 111: "BOS", 112: "CHC",
        113: "CIN", 114: "CLE", 115: "COL", 116: "DET", 117: "HOU",
        118: "KC",  119: "LAD", 120: "WSH", 121: "NYM", 133: "ATH",
        134: "PIT", 135: "SD",  136: "SEA", 137: "SF",  138: "STL",
        139: "TB",  140: "TEX", 141: "TOR", 142: "MIN", 143: "PHI",
        144: "ATL", 145: "CWS", 146: "MIA", 147: "NYY", 158: "MIL",
    }
    pid_list = [r.get("player_id", "") for r in rows_sorted if r.get("player_id", "").strip()]
    team_map: dict[str, str] = {}
    if pid_list:
        try:
            mlb_url = (
                "https://statsapi.mlb.com/api/v1/people"
                f"?personIds={','.join(pid_list)}&hydrate=currentTeam"
            )
            resp = requests.get(mlb_url, timeout=15)
            for p in resp.json().get("people", []):
                tid = p.get("currentTeam", {}).get("id")
                team_map[str(p["id"])] = _TEAM_ABBREV.get(tid, "") if tid else ""
        except Exception:
            pass

    # ── Name formatter: "Last, First" → "First Last" ─────────────────────────
    def _fmt_name(raw: str) -> str:
        parts = raw.split(", ", 1)
        return f"{parts[1]} {parts[0]}" if len(parts) == 2 else raw

    # ── Compute percentile thresholds (p33 / p67) for conditional formatting ──
    def _thresholds(vals: list[float]) -> tuple[float, float]:
        s = sorted(v for v in vals if v is not None)
        if not s:
            return (0.0, 0.0)
        n = len(s)
        p33 = s[int(n * 0.33)]
        p67 = s[int(n * 0.67)]
        return (p33, p67)

    def _safe_float(v) -> float | None:
        try:
            return float(v) if v not in (None, "", "null") else None
        except (ValueError, TypeError):
            return None

    barrels = [_safe_float(r.get("barrel_batted_rate")) for r in rows_sorted]
    xisos   = [_safe_float(r.get("xiso")) for r in rows_sorted]
    evs     = [_safe_float(r.get("exit_velocity_avg")) for r in rows_sorted]

    b_p33, b_p67 = _thresholds(barrels)
    x_p33, x_p67 = _thresholds(xisos)
    e_p33, e_p67 = _thresholds(evs)

    def _cell_class(val: float | None, p33: float, p67: float) -> str:
        if val is None:
            return ""
        if val >= p67:
            return " cell-g"
        if val >= p33:
            return " cell-y"
        return " cell-r"

    # ── Build table rows ──────────────────────────────────────────────────────
    row_html_parts: list[str] = []
    prev_hr = None
    display_rank = 0
    actual_rank  = 0
    for r in rows_sorted:
        actual_rank += 1
        hr = _hr(r)
        if hr != prev_hr:
            display_rank = actual_rank
            prev_hr = hr

        name    = _fmt_name(r.get("last_name, first_name", ""))
        pid     = r.get("player_id", "")
        team    = team_map.get(pid, "")
        barrel  = _safe_float(r.get("barrel_batted_rate"))
        xiso    = _safe_float(r.get("xiso"))
        ev      = _safe_float(r.get("exit_velocity_avg"))

        b_cls = _cell_class(barrel, b_p33, b_p67)
        x_cls = _cell_class(xiso,   x_p33, x_p67)
        e_cls = _cell_class(ev,     e_p33, e_p67)

        barrel_str = f"{barrel:.1f}%" if barrel is not None else "—"
        xiso_str   = f".{int(xiso * 1000):03d}" if xiso is not None else "—"
        ev_str     = f"{ev:.1f}"     if ev     is not None else "—"

        row_html_parts.append(
            f'<tr>'
            f'<td class="td-rank">{display_rank}</td>'
            f'<td class="td-player">{_esc(name)}</td>'
            f'<td class="td-team">{_esc(team)}</td>'
            f'<td class="td-hr">{hr}</td>'
            f'<td class="td-stat{b_cls} col-stat">{barrel_str}</td>'
            f'<td class="td-stat{x_cls} col-stat">{xiso_str}</td>'
            f'<td class="td-stat{e_cls} col-stat">{ev_str}</td>'
            f'</tr>'
        )

    rows_html = "\n      ".join(row_html_parts)

    # threshold labels for metric guide
    b_top = f"≥{b_p67:.1f}%"
    x_top = f"≥.{int(x_p67 * 1000):03d}"
    e_top = f"≥{e_p67:.1f} mph"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Season HR Leaders — Dingers Hotline</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Oswald:wght@400;500;600;700&family=Source+Serif+4:wght@400;600&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:       #FAFAF7;
    --surface:  #FFFFFF;
    --border:   #E2DED6;
    --navy:     #1B2A4A;
    --red:      #C8102E;
    --text:     #1A1A1A;
    --text-sub: #6B6560;
    --muted:    #A8A29E;
  }}
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: 'Source Serif 4', Georgia, serif; font-size: 14px; line-height: 1.5; min-height: 100vh; }}

  .site-header {{
    background: var(--navy);
    background-image: repeating-linear-gradient(90deg, transparent, transparent 47px, rgba(255,255,255,0.04) 47px, rgba(255,255,255,0.04) 48px);
    color: #fff; padding: 28px 36px 24px;
    display: flex; align-items: flex-end; justify-content: space-between; flex-wrap: wrap; gap: 20px;
    border-bottom: 4px solid var(--red);
  }}
  .header-left {{ display: flex; flex-direction: column; gap: 6px; }}
  .site-title {{ font-family: 'Oswald', sans-serif; font-weight: 700; font-size: clamp(30px, 5vw, 52px); letter-spacing: 0.04em; text-transform: uppercase; color: #fff; line-height: 1; display: flex; align-items: center; gap: 12px; }}
  .title-ball {{ display: inline-block; width: 0.85em; height: 0.85em; flex-shrink: 0; opacity: 0.9; }}
  .site-date {{ font-family: 'JetBrains Mono', monospace; font-size: 12px; color: rgba(255,255,255,0.55); letter-spacing: 0.12em; text-transform: uppercase; }}
  .nav-link {{ display: inline-flex; align-items: center; gap: 6px; background: rgba(255,255,255,0.10); color: #fff; font-family: 'Oswald', sans-serif; font-weight: 600; font-size: 14px; letter-spacing: 0.06em; text-transform: uppercase; text-decoration: none; padding: 10px 18px; border-radius: 6px; border: 1px solid rgba(255,255,255,0.18); transition: background 0.15s; white-space: nowrap; }}
  .nav-link:hover {{ background: rgba(255,255,255,0.18); }}

  .page-body {{ max-width: 820px; margin: 32px auto 48px; padding: 0 20px; }}
  .page-title {{ font-family: 'Oswald', sans-serif; font-size: 22px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; color: var(--navy); margin-bottom: 4px; }}
  .page-subtitle {{ font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--muted); letter-spacing: 0.1em; text-transform: uppercase; margin-bottom: 20px; }}

  .lb-table {{ width: 100%; border-collapse: collapse; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }}
  .lb-table thead tr {{ background: var(--navy); color: rgba(255,255,255,0.75); }}
  .lb-table thead th {{ font-family: 'JetBrains Mono', monospace; font-size: 10px; font-weight: 500; letter-spacing: 0.1em; text-transform: uppercase; padding: 10px 12px; text-align: left; }}
  .lb-table thead th.num {{ text-align: right; }}
  .lb-table tbody tr {{ border-bottom: 1px solid var(--border); }}
  .lb-table tbody tr:last-child {{ border-bottom: none; }}
  .lb-table tbody tr:hover {{ filter: brightness(0.97); }}
  .lb-table td {{ padding: 9px 12px; font-size: 13px; }}
  .td-rank {{ font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--muted); width: 36px; text-align: right; padding-right: 16px; }}
  .td-player {{ font-weight: 600; color: var(--text); min-width: 160px; }}
  .td-team {{ font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--text-sub); width: 48px; }}
  .td-hr {{ font-family: 'Oswald', sans-serif; font-size: 18px; font-weight: 700; color: var(--red); text-align: right; width: 52px; }}
  .td-stat {{ font-family: 'JetBrains Mono', monospace; font-size: 12px; font-weight: 600; text-align: right; width: 72px; }}

  .cell-g {{ background: #DCF1E5; color: #155D33; }}
  .cell-y {{ background: #FEF3C7; color: #92400E; }}
  .cell-r {{ background: #FCE8EA; color: #9B1220; }}

  .metric-defs {{ margin-top: 16px; padding: 16px 18px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; display: flex; flex-direction: column; gap: 10px; }}
  .metric-defs-title {{ font-family: 'JetBrains Mono', monospace; font-size: 10px; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; color: var(--muted); margin-bottom: 2px; }}
  .color-legend {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 4px; }}
  .legend-item {{ display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-sub); }}
  .legend-swatch {{ width: 14px; height: 14px; border-radius: 3px; flex-shrink: 0; }}
  .swatch-g {{ background: #DCF1E5; border: 1px solid #A8D9BC; }}
  .swatch-y {{ background: #FEF3C7; border: 1px solid #F9D56E; }}
  .swatch-r {{ background: #FCE8EA; border: 1px solid #F0A8B0; }}
  .metric-rows {{ display: flex; flex-wrap: wrap; gap: 8px 32px; }}
  .metric-def {{ display: flex; align-items: baseline; gap: 6px; font-size: 12px; color: var(--text-sub); }}
  .metric-def-label {{ font-family: 'JetBrains Mono', monospace; font-size: 11px; font-weight: 700; color: var(--navy); white-space: nowrap; }}

  .site-footer {{ text-align: center; padding: 16px 24px; font-size: 11px; color: var(--muted); border-top: 1px solid var(--border); margin-top: 40px; }}
  .disclaimer {{ margin-top: 6px; max-width: 540px; margin-left: auto; margin-right: auto; }}

  @media (max-width: 600px) {{
    .site-header {{ padding: 18px; }}
    .col-stat {{ display: none; }}
    .td-player {{ min-width: unset; }}
  }}
</style>
</head>
<body>

<header class="site-header">
  <div class="header-left">
    <div class="site-title">
      <svg class="title-ball" fill="#ffffff" viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg"><g><path d="M455.857,56.144c-74.86-74.859-196.662-74.859-271.521,0C17.087,223.392-9.275,272.783,2.398,298.264c8.318,18.153,32.898,19.077,63.015,17.249l-36.537,97.203c-6.838,18.194-2.549,38.035,11.195,51.778c13.744,13.743,33.583,18.035,51.778,11.195L197.2,436.089c-2.507,34.987-3.349,64.4,16.534,73.511c3.325,1.524,7.055,2.4,11.403,2.4c28.973-0.002,85.294-38.91,230.72-184.335C530.715,252.806,530.715,131.003,455.857,56.144z M441.431,313.239C369.446,385.224,316.37,433.95,279.174,462.2c-41.868,31.797-54.167,30.124-56.94,28.854c-2.851-1.307-4.901-7.374-5.626-16.646c-0.922-11.81,0.244-27.541,1.479-44.196c0.209-2.826,0.421-5.681,0.625-8.557c0.247-3.466-1.288-6.82-4.073-8.899c-1.788-1.335-3.934-2.026-6.102-2.026c-1.209,0-2.424,0.214-3.589,0.652l-120.277,45.21c-10.765,4.047-22.043,1.608-30.174-6.524c-8.131-8.131-10.57-19.411-6.524-30.174l42.126-112.072c1.224-3.258,0.703-6.915-1.382-9.702c-2.085-2.787-5.444-4.321-8.919-4.06c-14.536,1.076-31.012,2.295-42.857,1.278c-8.892-0.763-14.721-2.793-15.994-5.572c-1.27-2.772-2.944-15.072,28.855-56.941c28.25-37.196,76.975-90.271,148.96-162.255c66.904-66.905,175.764-66.905,242.669,0C508.335,137.474,508.335,246.335,441.431,313.239z"/></g><g><path d="M320.096,28.297c-90.213,0-163.608,73.394-163.608,163.608s73.395,163.608,163.608,163.608s163.608-73.395,163.608-163.608S410.31,28.297,320.096,28.297z M320.096,48.698c36.338,0,69.551,13.613,94.828,35.995c-26.187,23.225-59.477,35.903-94.828,35.903c-35.351,0-68.641-12.679-94.828-35.903C250.544,62.309,283.758,48.698,320.096,48.698z M320.096,335.111c-36.338,0.001-69.552-13.611-94.829-35.995c26.187-23.225,59.478-35.903,94.829-35.903c35.351,0,68.641,12.679,94.828,35.903C389.647,321.499,356.433,335.111,320.096,335.111z M429.215,284.535c-30.034-26.977-68.377-41.722-109.12-41.722c-40.743,0-79.086,14.745-109.12,41.722c-21.246-24.99-34.087-57.336-34.087-92.631c0.001-35.293,12.842-67.64,34.088-92.63c30.034,26.977,68.377,41.722,109.12,41.722s79.086-14.745,109.119-41.722c21.246,24.99,34.087,57.336,34.087,92.63C463.302,227.198,450.46,259.545,429.215,284.535z"/></g></svg>
      Dingers Hotline
    </div>
    <div class="site-date">Season HR Leaders &nbsp;·&nbsp; Updated {_esc(today_str)}</div>
  </div>
  <div style="display:flex;gap:8px;flex-wrap:wrap">
    <a class="nav-link" href="index.html">← Today's Picks</a>
    <a class="nav-link" href="pick-of-the-day.html">Pick of Day ★</a>
  </div>
</header>

<div class="page-body">
  <div class="page-title">2026 Home Run Leaders</div>
  <div class="page-subtitle">Top 50 · Statcast data · Updated daily with morning picks</div>

  <table class="lb-table">
    <thead>
      <tr>
        <th style="width:36px">#</th>
        <th>Player</th>
        <th>Team</th>
        <th class="num">HR</th>
        <th class="num col-stat">Barrel%</th>
        <th class="num col-stat">xISO</th>
        <th class="num col-stat">EV Avg</th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>

  <div class="metric-defs">
    <div class="metric-defs-title">Metric Guide</div>
    <div class="color-legend">
      <div class="legend-item"><div class="legend-swatch swatch-g"></div> Top third of this leaderboard</div>
      <div class="legend-item"><div class="legend-swatch swatch-y"></div> Middle third</div>
      <div class="legend-item"><div class="legend-swatch swatch-r"></div> Bottom third</div>
    </div>
    <div class="metric-rows">
      <div class="metric-def"><span class="metric-def-label">Barrel%</span> % of batted balls hit with elite exit velocity + launch angle. Strongest Statcast predictor of HR output (r=0.70). Top third: {_esc(b_top)}.</div>
      <div class="metric-def"><span class="metric-def-label">xISO</span> Expected isolated power based on quality of contact — park and luck neutral. Better than actual ISO for projecting future HR rate. Top third: {_esc(x_top)}.</div>
      <div class="metric-def"><span class="metric-def-label">EV Avg</span> Average exit velocity in mph. Measures raw power. Top third: {_esc(e_top)}.</div>
    </div>
  </div>
</div>

<footer class="site-footer">
  <span>Dingers Hotline &nbsp;·&nbsp; Season HR Leaders</span>
  <div class="disclaimer">Must be 21+ and present in a legal sports wagering state. Gambling involves risk. Please gamble responsibly. If you or someone you know has a gambling problem, call or text <strong>1-800-GAMBLER</strong>.</div>
</footer>

</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Hit Rate Calendar page
# ─────────────────────────────────────────────────────────────────────────────

def generate_hit_rate_html(pnl_data: dict, today: str) -> str:
    """Generate hit-rate.html from model_pnl_report() data."""
    summary = pnl_data.get("model_pnl_summary", {})
    daily   = pnl_data.get("daily", [])

    win_rate   = summary.get("win_pct", "—")
    days_tracked = summary.get("days_tracked", 0)
    total_picks  = summary.get("total_picks_with_odds", 0)
    total_wins   = summary.get("total_wins", 0)
    record_str   = f"{total_wins} / {total_picks}"

    best_day = max(daily, key=lambda d: d["wins"]) if daily else None
    if best_day:
        bd_rate = f"{best_day['wins'] / best_day['picks_with_odds'] * 100:.0f}%" if best_day["picks_with_odds"] else "—"
        best_day_val  = f"{best_day['wins']} / {best_day['picks_with_odds']}"
        best_day_sub  = f"{best_day['date']} · {bd_rate} rate"
    else:
        best_day_val = "—"
        best_day_sub = ""

    avg_per_day = f"{total_picks / days_tracked:.1f}" if days_tracked else "—"

    # Build lean PICKS_DATA: drop pnl fields, keep only what the calendar needs
    picks_data = []
    for d in daily:
        players = [
            {"rank": p["rank"], "player": p["player"], "homered": p["homered"]}
            for p in d.get("players", [])
        ]
        picks_data.append({
            "date": d["date"],
            "picks_with_odds": d["picks_with_odds"],
            "wins": d["wins"],
            "players": players,
        })

    # Initial month: most recent month that has data
    if daily:
        last_date = daily[-1]["date"]
        init_year  = int(last_date[:4])
        init_month = int(last_date[5:7]) - 1  # JS 0-indexed
    else:
        init_year, init_month = 2026, 3  # April

    picks_json = _json.dumps(picks_data)

    BALL_SVG = '<svg class="title-ball" fill="#ffffff" viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg"><g><path d="M455.857,56.144c-74.86-74.859-196.662-74.859-271.521,0C17.087,223.392-9.275,272.783,2.398,298.264c8.318,18.153,32.898,19.077,63.015,17.249l-36.537,97.203c-6.838,18.194-2.549,38.035,11.195,51.778c13.744,13.743,33.583,18.035,51.778,11.195L197.2,436.089c-2.507,34.987-3.349,64.4,16.534,73.511c3.325,1.524,7.055,2.4,11.403,2.4c28.973-0.002,85.294-38.91,230.72-184.335C530.715,252.806,530.715,131.003,455.857,56.144z"/></g><g><path d="M320.096,28.297c-90.213,0-163.608,73.394-163.608,163.608s73.395,163.608,163.608,163.608s163.608-73.395,163.608-163.608S410.31,28.297,320.096,28.297z M320.096,48.698c36.338,0,69.551,13.613,94.828,35.995c-26.187,23.225-59.477,35.903-94.828,35.903c-35.351,0-68.641-12.679-94.828-35.903C250.544,62.309,283.758,48.698,320.096,48.698z M320.096,335.111c-36.338,0.001-69.552-13.611-94.829-35.995c26.187-23.225,59.478-35.903,94.829-35.903c35.351,0,68.641,12.679,94.828,35.903C389.647,321.499,356.433,335.111,320.096,335.111z"/></g></svg>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hit Rate Calendar — Dingers Hotline</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Oswald:wght@400;500;600;700&family=Source+Serif+4:wght@400;600&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:#FAFAF7;--surface:#FFFFFF;--surface2:#F3F2EE;--border:#E2DED6;
    --border-dark:#C8C2B8;--navy:#1B2A4A;--navy-mid:#2D4070;--red:#C8102E;
    --red-dim:#F9E5E8;--gold:#D4A017;--gold-dim:#FDF5DC;--green:#1A6B3C;
    --green-dim:#E4F2EB;--amber:#B45309;--amber-dim:#FEF3C7;
    --text:#1A1A1A;--text-sub:#6B6560;--text-dim:#A8A29E;
  }}
  *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:'Source Serif 4',Georgia,serif;font-size:14px;line-height:1.5;min-height:100vh}}
  .site-header{{background:var(--navy);background-image:repeating-linear-gradient(90deg,transparent,transparent 47px,rgba(255,255,255,0.04) 47px,rgba(255,255,255,0.04) 48px);color:#fff;padding:28px 36px 24px;display:flex;align-items:flex-end;justify-content:space-between;flex-wrap:wrap;gap:20px;border-bottom:4px solid var(--red)}}
  .header-left{{display:flex;flex-direction:column;gap:6px}}
  .site-title{{font-family:'Oswald',sans-serif;font-weight:700;font-size:clamp(30px,5vw,52px);letter-spacing:.04em;text-transform:uppercase;color:#fff;line-height:1;display:flex;align-items:center;gap:12px}}
  .title-ball{{display:inline-block;width:.85em;height:.85em;flex-shrink:0;opacity:.9}}
  .site-date{{font-family:'JetBrains Mono',monospace;font-size:12px;color:rgba(255,255,255,.55);letter-spacing:.12em;text-transform:uppercase}}
  .model-chips{{display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
  .nav-link{{display:inline-flex;align-items:center;gap:6px;background:rgba(255,255,255,.10);color:#fff;font-family:'Oswald',sans-serif;font-weight:600;font-size:13px;letter-spacing:.06em;text-transform:uppercase;text-decoration:none;padding:8px 16px;border-radius:6px;border:1px solid rgba(255,255,255,.18);white-space:nowrap;transition:background .15s}}
  .nav-link:hover{{background:rgba(255,255,255,.18)}}
  .nav-link.active{{background:rgba(255,255,255,.22);border-color:rgba(255,255,255,.4)}}
  .tg-join{{display:flex;flex-direction:column;align-items:flex-end;gap:6px;text-align:right}}
  .tg-join-label{{font-size:.78rem;color:rgba(255,255,255,.70);line-height:1.35;max-width:220px}}
  .tg-join-btn{{display:inline-flex;align-items:center;gap:8px;background:#229ED9;color:#fff;font-weight:700;font-size:.88rem;padding:10px 18px;border-radius:8px;text-decoration:none;white-space:nowrap;transition:background .15s}}
  .tg-join-btn:hover{{background:#1a8bbf}}
  .model-stats-tile{{margin:28px 36px 0;background:var(--surface);border:1px solid var(--border);border-radius:6px;display:flex;overflow:hidden}}
  .stats-tile-hero{{background:var(--navy);color:#fff;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:20px 28px;min-width:120px;flex-shrink:0}}
  .hero-value{{font-family:'Oswald',sans-serif;font-size:2.2rem;font-weight:700;line-height:1;letter-spacing:-.5px}}
  .hero-label{{font-size:.6rem;text-transform:uppercase;letter-spacing:1px;opacity:.6;margin-top:4px}}
  .stats-tile-items{{display:flex;flex:1;border-left:1px solid var(--border)}}
  .stats-tile-item{{flex:1;display:flex;flex-direction:column;justify-content:center;padding:16px 20px;border-right:1px solid var(--border)}}
  .stats-tile-item:last-child{{border-right:none}}
  .sti-label{{font-size:.6rem;text-transform:uppercase;letter-spacing:1px;color:var(--text-dim);margin-bottom:4px}}
  .sti-value{{font-family:'JetBrains Mono',monospace;font-size:1rem;font-weight:700;color:var(--text)}}
  .sti-sub{{font-size:.65rem;color:var(--text-sub);margin-top:2px}}
  .page-body{{max-width:1100px;margin:0 auto;padding:0 36px 60px}}
  .month-nav{{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;padding-bottom:16px;border-bottom:1px solid var(--border)}}
  .month-nav-btn{{font-family:'Oswald',sans-serif;font-weight:600;font-size:13px;letter-spacing:.06em;text-transform:uppercase;background:var(--surface);border:1px solid var(--border);color:var(--navy);padding:8px 20px;border-radius:6px;cursor:pointer;transition:background .15s}}
  .month-nav-btn:hover{{background:var(--surface2)}}
  .month-nav-btn:disabled{{opacity:.35;cursor:default}}
  .month-title{{font-family:'Oswald',sans-serif;font-weight:700;font-size:1.5rem;letter-spacing:.04em;text-transform:uppercase;color:var(--navy)}}
  .cal-section{{margin-bottom:16px}}
  .cal-dow-header{{display:grid;grid-template-columns:repeat(7,1fr);gap:4px;margin-bottom:4px}}
  .cal-dow{{font-family:'Oswald',sans-serif;font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--text-dim);text-align:center;padding:6px 0}}
  .cal-grid{{display:grid;grid-template-columns:repeat(7,1fr);gap:4px}}
  .cal-cell{{background:var(--surface);border:1px solid var(--border);border-radius:5px;min-height:80px;padding:8px 10px;cursor:default;position:relative;transition:border-color .12s,box-shadow .12s}}
  .cal-cell.has-data{{cursor:pointer}}
  .cal-cell.has-data:hover{{border-color:var(--navy-mid);box-shadow:0 2px 8px rgba(27,42,74,.10)}}
  .cal-cell.selected{{border-color:var(--navy)!important;box-shadow:0 0 0 2px rgba(27,42,74,.18)!important}}
  .cal-cell.empty{{background:transparent;border-color:transparent;cursor:default}}
  .cal-cell.above-avg{{background:var(--green-dim);border-color:#b2d9c3}}
  .cal-cell.below-avg{{background:var(--amber-dim);border-color:#f5d68a}}
  .cal-cell.no-hits{{background:var(--red-dim);border-color:#f0c0c8}}
  .cell-date{{font-family:'Oswald',sans-serif;font-size:13px;font-weight:600;color:var(--text-sub);margin-bottom:4px}}
  .cell-rate{{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;line-height:1.2}}
  .cell-rate.above-avg{{color:var(--green)}}
  .cell-rate.below-avg{{color:var(--amber)}}
  .cell-rate.no-hits{{color:var(--red)}}
  .month-footer{{background:var(--navy);color:#fff;border-radius:6px;padding:14px 20px;display:flex;align-items:center;gap:28px;margin-top:6px;flex-wrap:wrap}}
  .mf-item{{display:flex;flex-direction:column}}
  .mf-label{{font-family:'Oswald',sans-serif;font-size:10px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;opacity:.55;margin-bottom:2px}}
  .mf-val{{font-family:'JetBrains Mono',monospace;font-size:15px;font-weight:700}}
  .mf-val.strong{{color:#4ADE80}}
  .mf-divider{{width:1px;background:rgba(255,255,255,.15);align-self:stretch}}
  .detail-panel{{background:var(--surface);border:2px solid var(--navy);border-radius:8px;margin-bottom:32px;overflow:hidden;animation:slideDown .18s ease-out}}
  @keyframes slideDown{{from{{opacity:0;transform:translateY(-8px)}}to{{opacity:1;transform:translateY(0)}}}}
  .detail-header{{background:var(--navy);color:#fff;padding:16px 24px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}}
  .detail-date{{font-family:'Oswald',sans-serif;font-size:1.1rem;font-weight:700;letter-spacing:.05em;text-transform:uppercase}}
  .detail-chips{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
  .dchip{{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;padding:4px 12px;border-radius:4px}}
  .dchip.rate-strong{{background:rgba(74,222,128,.15);color:#4ADE80;border:1px solid rgba(74,222,128,.3)}}
  .dchip.rate-weak{{background:rgba(248,113,113,.15);color:#F87171;border:1px solid rgba(248,113,113,.3)}}
  .dchip.rate-mid{{background:rgba(251,191,36,.15);color:#FBBF24;border:1px solid rgba(251,191,36,.3)}}
  .dchip.record{{background:rgba(255,255,255,.1);color:rgba(255,255,255,.8);border:1px solid rgba(255,255,255,.2)}}
  .detail-close{{font-family:'Oswald',sans-serif;font-size:12px;font-weight:600;letter-spacing:.06em;background:rgba(255,255,255,.12);color:rgba(255,255,255,.7);border:1px solid rgba(255,255,255,.2);border-radius:4px;padding:5px 12px;cursor:pointer;text-transform:uppercase}}
  .detail-close:hover{{background:rgba(255,255,255,.2);color:#fff}}
  .detail-body{{padding:20px 24px}}
  .pick-section-label{{font-family:'Oswald',sans-serif;font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--border)}}
  .pick-section-label.hit{{color:var(--green)}}
  .pick-section-label.miss{{color:var(--text-sub);margin-top:20px}}
  .pick-table{{width:100%;border-collapse:collapse;margin-bottom:4px}}
  .pick-table th{{font-family:'Oswald',sans-serif;font-size:10px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--text-dim);text-align:left;padding:0 10px 6px 0;border-bottom:1px solid var(--border)}}
  .pick-table td{{padding:7px 10px 7px 0;border-bottom:1px solid var(--surface2);vertical-align:middle}}
  .pick-table tr:last-child td{{border-bottom:none}}
  .rank-badge{{display:inline-flex;align-items:center;justify-content:center;font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;width:26px;height:22px;border-radius:4px;background:var(--surface2);color:var(--text-dim)}}
  .rank-badge.top5{{background:var(--gold-dim);color:var(--gold)}}
  .player-name{{font-weight:600;font-size:13px}}
  .player-name.hit{{color:var(--green)}}
  .player-name.miss{{color:var(--text-sub)}}
  .detail-summary{{display:flex;gap:0;border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-top:20px}}
  .ds-item{{flex:1;padding:12px 16px;border-right:1px solid var(--border)}}
  .ds-item:last-child{{border-right:none}}
  .ds-label{{font-size:.6rem;text-transform:uppercase;letter-spacing:1px;color:var(--text-dim);margin-bottom:3px}}
  .ds-val{{font-family:'JetBrains Mono',monospace;font-size:15px;font-weight:700}}
  .ds-val.hit{{color:var(--green)}}
  .ds-val.rate{{color:var(--navy)}}
  @media(max-width:700px){{
    .site-header{{padding:20px 16px 16px}}
    .page-body{{padding:0 12px 40px}}
    .model-stats-tile{{flex-direction:column;margin:0 12px 20px}}
    .stats-tile-items{{flex-direction:column;border-left:none;border-top:1px solid var(--border)}}
    .stats-tile-item{{border-right:none;border-bottom:1px solid var(--border)}}
    .stats-tile-item:last-child{{border-bottom:none}}
    .cal-cell{{min-height:60px;padding:5px}}
    .cell-rate{{font-size:11px}}
    .detail-body{{padding:14px}}
    .detail-summary{{flex-direction:column}}
    .ds-item{{border-right:none;border-bottom:1px solid var(--border)}}
    .ds-item:last-child{{border-bottom:none}}
  }}
</style>
</head>
<body>
<header class="site-header">
  <div class="header-left">
    <div class="site-title">{BALL_SVG} Dingers Hotline</div>
    <div class="site-date">Hit Rate Calendar — Season 2026</div>
  </div>
  <div class="model-chips">
    <a class="nav-link" href="index.html">Today's Picks</a>
    <a class="nav-link" href="leaderboard.html">HR Leaders →</a>
    <a class="nav-link active" href="#">Hit Rate 📅</a>
  </div>
  <div class="tg-join">
    <div class="tg-join-label">Get notified the moment today's picks are ready.</div>
    <a class="tg-join-btn" href="https://t.me/+BHJ6UMUkhyoxNzEx" target="_blank" rel="noopener">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="white"><path d="M12 0C5.373 0 0 5.373 0 12s5.373 12 12 12 12-5.373 12-12S18.627 0 12 0zm5.894 8.221-1.97 9.28c-.145.658-.537.818-1.084.508l-3-2.21-1.447 1.394c-.16.16-.295.295-.605.295l.213-3.053 5.56-5.023c.242-.213-.054-.333-.373-.12L7.26 13.835l-2.938-.916c-.638-.203-.651-.638.136-.944l11.438-4.41c.532-.194.997.131.998.656z"/></svg>
      Join on Telegram
    </a>
  </div>
</header>

<div class="model-stats-tile">
  <div class="stats-tile-hero">
    <div class="hero-value">{_esc(win_rate)}</div>
    <div class="hero-label">Hit Rate</div>
  </div>
  <div class="stats-tile-items">
    <div class="stats-tile-item">
      <div class="sti-label">Season Record</div>
      <div class="sti-value">{_esc(record_str)}</div>
      <div class="sti-sub">picks homered since Apr 16</div>
    </div>
    <div class="stats-tile-item">
      <div class="sti-label">Days Tracked</div>
      <div class="sti-value">{days_tracked}</div>
      <div class="sti-sub">labeled results</div>
    </div>
    <div class="stats-tile-item">
      <div class="sti-label">Best Day</div>
      <div class="sti-value" style="color:var(--green)">{_esc(best_day_val)}</div>
      <div class="sti-sub">{_esc(best_day_sub)}</div>
    </div>
    <div class="stats-tile-item">
      <div class="sti-label">Picks / Day</div>
      <div class="sti-value">{_esc(avg_per_day)}</div>
      <div class="sti-sub">avg picks per tracked day</div>
    </div>
  </div>
</div>

<div class="page-body" style="margin-top:28px">
  <div id="calendarRoot"></div>
  <div id="detailRoot"></div>
</div>

<script>
const PICKS_DATA = {picks_json};
const byDate = {{}};
PICKS_DATA.forEach(function(d){{ byDate[d.date] = d; }});
const MONTHS = ['January','February','March','April','May','June','July','August','September','October','November','December'];
const DOW = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
let currentYear = {init_year};
let currentMonth = {init_month};
let selectedDate = null;

function hitRateClass(wins, total) {{
  if (!total) return 'neutral';
  if (!wins) return 'no-hits';
  return wins / total >= 0.20 ? 'above-avg' : 'below-avg';
}}

function renderCalendar() {{
  const root = document.getElementById('calendarRoot');
  while (root.firstChild) root.removeChild(root.firstChild);
  const nav = document.createElement('div'); nav.className = 'month-nav';
  const prevBtn = document.createElement('button');
  prevBtn.className = 'month-nav-btn'; prevBtn.textContent = '← Previous Month';
  prevBtn.disabled = (currentYear === 2026 && currentMonth === 3);
  prevBtn.addEventListener('click', function() {{ currentMonth--; if (currentMonth < 0) {{ currentMonth = 11; currentYear--; }} selectedDate = null; renderCalendar(); renderDetail(); }});
  const titleEl = document.createElement('div'); titleEl.className = 'month-title';
  titleEl.textContent = MONTHS[currentMonth] + ' ' + currentYear;
  const nextBtn = document.createElement('button');
  nextBtn.className = 'month-nav-btn'; nextBtn.textContent = 'Next Month →';
  const maxDate = PICKS_DATA.length ? PICKS_DATA[PICKS_DATA.length-1].date : '2026-04-30';
  const maxYear = parseInt(maxDate.slice(0,4)); const maxMonth = parseInt(maxDate.slice(5,7)) - 1;
  nextBtn.disabled = (currentYear === maxYear && currentMonth === maxMonth);
  nextBtn.addEventListener('click', function() {{ currentMonth++; if (currentMonth > 11) {{ currentMonth = 0; currentYear++; }} selectedDate = null; renderCalendar(); renderDetail(); }});
  nav.appendChild(prevBtn); nav.appendChild(titleEl); nav.appendChild(nextBtn); root.appendChild(nav);
  const calSection = document.createElement('div'); calSection.className = 'cal-section';
  const dowRow = document.createElement('div'); dowRow.className = 'cal-dow-header';
  DOW.forEach(function(d) {{ const c = document.createElement('div'); c.className = 'cal-dow'; c.textContent = d; dowRow.appendChild(c); }});
  calSection.appendChild(dowRow);
  const grid = document.createElement('div'); grid.className = 'cal-grid';
  const firstDay = new Date(currentYear, currentMonth, 1).getDay();
  const daysInMonth = new Date(currentYear, currentMonth + 1, 0).getDate();
  for (let i = 0; i < firstDay; i++) {{ const e = document.createElement('div'); e.className = 'cal-cell empty'; grid.appendChild(e); }}
  let mWins = 0, mPicks = 0, mAbove = 0;
  for (let day = 1; day <= daysInMonth; day++) {{
    const mm = String(currentMonth+1).padStart(2,'0'), dd = String(day).padStart(2,'0');
    const dateStr = currentYear + '-' + mm + '-' + dd;
    const data = byDate[dateStr]; const isSel = selectedDate === dateStr;
    const cell = document.createElement('div');
    if (!data) {{
      cell.className = 'cal-cell' + (isSel ? ' selected' : '');
      const dl = document.createElement('div'); dl.className = 'cell-date'; dl.textContent = day; cell.appendChild(dl);
    }} else {{
      const cls = hitRateClass(data.wins, data.picks_with_odds);
      cell.className = 'cal-cell has-data ' + cls + (isSel ? ' selected' : '');
      mWins += data.wins; mPicks += data.picks_with_odds; if (cls === 'above-avg') mAbove++;
      const dl = document.createElement('div'); dl.className = 'cell-date'; dl.textContent = day; cell.appendChild(dl);
      const pct = (data.wins / data.picks_with_odds * 100).toFixed(0);
      const rl = document.createElement('div'); rl.className = 'cell-rate ' + cls;
      rl.textContent = data.wins + '/' + data.picks_with_odds + ' · ' + pct + '%'; cell.appendChild(rl);
      const cap = dateStr;
      cell.addEventListener('click', function() {{
        selectedDate = selectedDate === cap ? null : cap;
        renderCalendar(); renderDetail();
        if (selectedDate) setTimeout(function() {{ document.getElementById('detailRoot').scrollIntoView({{behavior:'smooth',block:'nearest'}}); }}, 50);
      }});
    }}
    grid.appendChild(cell);
  }}
  calSection.appendChild(grid); root.appendChild(calSection);
  if (mPicks > 0) {{
    const footer = document.createElement('div'); footer.className = 'month-footer';
    function mfi(lbl, val, cls) {{
      const g = document.createElement('div'); g.className = 'mf-item';
      const l = document.createElement('div'); l.className = 'mf-label'; l.textContent = lbl;
      const v = document.createElement('div'); v.className = 'mf-val' + (cls ? ' '+cls : ''); v.textContent = val;
      g.appendChild(l); g.appendChild(v); return g;
    }}
    function mfd() {{ const d = document.createElement('div'); d.className = 'mf-divider'; return d; }}
    const mr = (mWins / mPicks * 100).toFixed(1);
    footer.appendChild(mfi('HR Hits', mWins + ' / ' + mPicks, ''));
    footer.appendChild(mfd());
    footer.appendChild(mfi('Hit Rate', mr + '%', parseFloat(mr) >= 20 ? 'strong' : ''));
    footer.appendChild(mfd());
    const daysInData = Object.keys(byDate).filter(function(d){{ return d.startsWith(currentYear + '-' + String(currentMonth+1).padStart(2,'0')); }}).length;
    footer.appendChild(mfi('Days ≥20%', mAbove + ' of ' + daysInData, ''));
    root.appendChild(footer);
  }}
}}

function renderDetail() {{
  const root = document.getElementById('detailRoot');
  while (root.firstChild) root.removeChild(root.firstChild);
  if (!selectedDate) return;
  const d = byDate[selectedDate]; if (!d) return;
  const panel = document.createElement('div'); panel.className = 'detail-panel';
  const hdr = document.createElement('div'); hdr.className = 'detail-header';
  const dateEl = document.createElement('div'); dateEl.className = 'detail-date';
  dateEl.textContent = new Date(selectedDate + 'T12:00:00').toLocaleDateString('en-US', {{weekday:'long',month:'long',day:'numeric',year:'numeric'}});
  hdr.appendChild(dateEl);
  const chips = document.createElement('div'); chips.className = 'detail-chips';
  const rate = d.wins / d.picks_with_odds;
  const rc1 = document.createElement('div');
  rc1.className = 'dchip ' + (rate >= 0.20 ? 'rate-strong' : d.wins === 0 ? 'rate-weak' : 'rate-mid');
  rc1.textContent = (rate * 100).toFixed(1) + '% hit rate'; chips.appendChild(rc1);
  const rc2 = document.createElement('div'); rc2.className = 'dchip record';
  rc2.textContent = d.wins + ' / ' + d.picks_with_odds + ' homered'; chips.appendChild(rc2);
  hdr.appendChild(chips);
  const closeBtn = document.createElement('button'); closeBtn.className = 'detail-close'; closeBtn.textContent = '✕ Close';
  closeBtn.addEventListener('click', function() {{ selectedDate = null; renderCalendar(); renderDetail(); }});
  hdr.appendChild(closeBtn); panel.appendChild(hdr);
  const body = document.createElement('div'); body.className = 'detail-body';
  const homered = d.players.filter(function(p){{ return p.homered; }});
  const missed  = d.players.filter(function(p){{ return !p.homered; }});
  function buildSection(players, isHit) {{
    const lbl = document.createElement('div');
    lbl.className = 'pick-section-label ' + (isHit ? 'hit' : 'miss');
    lbl.textContent = (isHit ? '⚾ HOMERED' : '✕ MISSED') + '  —  ' + players.length + ' player' + (players.length !== 1 ? 's' : '');
    body.appendChild(lbl);
    const tbl = document.createElement('table'); tbl.className = 'pick-table';
    const thead = document.createElement('thead'); const hr = document.createElement('tr');
    ['Rank','Player'].forEach(function(h) {{ const th = document.createElement('th'); th.textContent = h; hr.appendChild(th); }});
    thead.appendChild(hr); tbl.appendChild(thead);
    const tbody = document.createElement('tbody');
    players.forEach(function(p) {{
      const tr = document.createElement('tr');
      const rtd = document.createElement('td');
      const badge = document.createElement('span');
      badge.className = 'rank-badge' + (p.rank <= 5 ? ' top5' : '');
      badge.textContent = '#' + p.rank; rtd.appendChild(badge); tr.appendChild(rtd);
      const ntd = document.createElement('td');
      const ns = document.createElement('span');
      ns.className = 'player-name ' + (isHit ? 'hit' : 'miss');
      ns.textContent = p.player; ntd.appendChild(ns); tr.appendChild(ntd);
      tbody.appendChild(tr);
    }});
    tbl.appendChild(tbody); body.appendChild(tbl);
  }}
  if (homered.length) buildSection(homered, true);
  if (missed.length)  buildSection(missed, false);
  const summary = document.createElement('div'); summary.className = 'detail-summary';
  function dsi(lbl, val, cls) {{
    const item = document.createElement('div'); item.className = 'ds-item';
    const l = document.createElement('div'); l.className = 'ds-label'; l.textContent = lbl;
    const v = document.createElement('div'); v.className = 'ds-val' + (cls ? ' '+cls : ''); v.textContent = val;
    item.appendChild(l); item.appendChild(v); return item;
  }}
  summary.appendChild(dsi('Homered', homered.length + ' player' + (homered.length !== 1 ? 's' : ''), 'hit'));
  summary.appendChild(dsi('Missed',  missed.length  + ' player' + (missed.length  !== 1 ? 's' : ''), ''));
  summary.appendChild(dsi('Hit Rate', (d.wins / d.picks_with_odds * 100).toFixed(1) + '%', 'rate'));
  body.appendChild(summary); panel.appendChild(body); root.appendChild(panel);
}}

renderCalendar();
renderDetail();
</script>
<footer style="background:var(--navy);color:rgba(255,255,255,.45);padding:20px 36px;font-size:.7rem;text-align:center;margin-top:40px">
  <span>Dingers Hotline &nbsp;·&nbsp; Updated {_esc(today)}</span>
  <div style="margin-top:6px">Must be 21+ and present in a legal sports wagering state. Gambling involves risk. Please gamble responsibly. If you or someone you know has a gambling problem, call or text <strong>1-800-GAMBLER</strong>.</div>
</footer>
</body>
</html>"""
