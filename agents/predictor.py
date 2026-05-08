"""
BallparkPal Predictor Agent
Uses local Ollama (llama3.1) — no API key required.

Skills:
  - fetch_ballparkpal_projections  : scrape today's HR probability table
  - fetch_park_factors             : park HR%, wind, temperature, weather for today's games
  - fetch_pitcher_matchups         : batter vs pitcher matchup grades + park-adjusted HR%
  - fetch_statcast_batter_stats    : barrel rate, hard hit %, HR/FB, xISO, pull%
  - fetch_statcast_pitcher_stats   : HR/9, FB%, hard hit % allowed, xFIP
  - fetch_confirmed_lineups        : today's confirmed batting orders + starting pitchers
  - fetch_hr_prop_odds             : current sportsbook HR prop lines (requires ODDS_API_KEY)
    """
import csv
import io
import json
import os
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from .base import get_db_conn
from .bet_tracker import upsert_player_attr, get_bat_side, get_bat_side_by_name

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

BALLPARKPAL_BASE  = "https://www.ballparkpal.com"
BALLPARKPAL_LOGIN = f"{BALLPARKPAL_BASE}/Login.php"
PARK_FACTORS_URL  = f"{BALLPARKPAL_BASE}/Park-Factors.php"
MATCHUPS_URL      = f"{BALLPARKPAL_BASE}/Matchups.php"
SAVANT_BASE       = "https://baseballsavant.mlb.com"
MLB_API_BASE      = "https://statsapi.mlb.com/api/v1"
ODDS_API_BASE     = "https://api.the-odds-api.com/v4"
FANGRAPHS_PF_URL  = "https://www.fangraphs.com/guts.aspx?type=pf&season={season}&teamid=0"
OPENWEATHER_URL   = "https://api.openweathermap.org/data/2.5/weather"

# ── BallparkPal authenticated session ─────────────────────────────────────────
# Cached session so we only log in once per Python process.
_bpp_session: requests.Session | None = None
_bpp_session_ts: float = 0.0
_SESSION_TTL = 3600   # re-login after 1 hour


def _get_bpp_session() -> requests.Session | None:
    """
    Return an authenticated BallparkPal session.
    Reads BALLPARKPAL_EMAIL + BALLPARKPAL_PASSWORD from env.
    Returns None if credentials are missing or login fails.
    """
    global _bpp_session, _bpp_session_ts

    email    = os.getenv("BALLPARKPAL_EMAIL", "").strip()
    password = os.getenv("BALLPARKPAL_PASSWORD", "").strip()

    if not email or not password:
        return None

    # Return cached session if still fresh
    if _bpp_session and (time.time() - _bpp_session_ts) < _SESSION_TTL:
        return _bpp_session

    session = requests.Session()
    session.headers.update(_HEADERS)

    try:
        resp = session.post(
            BALLPARKPAL_LOGIN,
            data={"email": email, "password": password, "login": ""},
            timeout=20,
            allow_redirects=True,
        )
        # Successful login redirects away from the checkout/login page
        if "Secure Checkout" in resp.text or "Login" in (resp.url or ""):
            return None   # still on login/paywall page — credentials wrong

        _bpp_session    = session
        _bpp_session_ts = time.time()
        return session
    except requests.RequestException:
        return None

# ── Tool definitions ───────────────────────────────────────────────────────────

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "fetch_ballparkpal_projections",
            "description": "Scrape today's home run projections from BallparkPal.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_park_factors",
            "description": (
                "Scrape today's park factors and weather from BallparkPal. "
                "Returns HR% factor, wind receptiveness, wind speed/direction, "
                "temperature, humidity, altitude, and park trait per game."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_pitcher_matchups",
            "description": (
                "Scrape today's batter-vs-pitcher matchup data from BallparkPal. "
                "Returns matchup grade (0-10), park-adjusted HR%, handedness, XB%, BB%, K%."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_statcast_batter_stats",
            "description": (
                "Fetch a batter's Statcast metrics: barrel rate, hard hit %, "
                "HR/FB ratio, xISO, sweet spot %, pull %, exit velo, launch angle."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "player": {"type": "string", "description": "Player name (partial match supported)."},
                },
                "required": ["player"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_statcast_pitcher_stats",
            "description": (
                "Fetch a pitcher's Statcast vulnerability metrics: barrel rate allowed, "
                "hard hit % allowed, HR/FB allowed, xFIP, FB%."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pitcher": {"type": "string", "description": "Pitcher name (partial match supported)."},
                },
                "required": ["pitcher"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_confirmed_lineups",
            "description": (
                "Fetch today's confirmed batting orders and starting pitchers from MLB Stats API. "
                "Always call this first to confirm player is in lineup."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "game_date": {"type": "string", "description": "YYYY-MM-DD (defaults to today)."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_hr_prop_odds",
            "description": "Fetch current sportsbook HR prop odds. Requires ODDS_API_KEY env var.",
            "parameters": {
                "type": "object",
                "properties": {
                    "player": {"type": "string", "description": "Optional player name filter."},
                },
                "required": [],
            },
        },
    },
]

# ── Tool implementations ───────────────────────────────────────────────────────

def fetch_ballparkpal_projections() -> str:
    session = _get_bpp_session()
    if not session:
        return json.dumps({"status": "no_auth",
                           "message": "BallparkPal credentials not set or login failed. "
                                      "Add BALLPARKPAL_EMAIL and BALLPARKPAL_PASSWORD to api/.env"})
    try:
        resp = session.get(MATCHUPS_URL, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return json.dumps({"status": "error", "message": str(exc)})

    soup, projections = BeautifulSoup(resp.text, "lxml"), []
    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if not header_row:
            continue
        headers = [th.get_text(strip=True).lower() for th in header_row.find_all(["th", "td"])]
        if not any(h in headers for h in ["player", "name", "batter", "hitter"]):
            continue
        for row in table.find_all("tr")[1:]:
            cols = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cols) < 2:
                continue
            proj = {h: cols[i] for i, h in enumerate(headers) if i < len(cols)}
            if proj:
                projections.append(proj)

    if not projections:
        return json.dumps({"status": "no_data",
                           "message": "Could not parse projections from BallparkPal.",
                           "url": MATCHUPS_URL})
    return json.dumps({"status": "success", "count": len(projections),
                       "projections": projections[:60]}, indent=2)


def fetch_park_factors() -> str:
    session = _get_bpp_session()
    if not session:
        return fetch_park_factors_fallback()
    try:
        resp = session.get(PARK_FACTORS_URL, timeout=20)
        resp.raise_for_status()
        # If redirected to checkout, fall back
        if "Secure Checkout" in resp.text:
            return fetch_park_factors_fallback()
    except requests.RequestException as exc:
        return json.dumps({"status": "error", "message": str(exc)})

    soup  = BeautifulSoup(resp.text, "lxml")
    table = (soup.find("table", {"id": "parkFactorsTable"})
             or soup.find("table", class_=lambda c: c and "park" in c.lower())
             or (soup.find_all("table") or [None])[0])

    if not table:
        return json.dumps({"status": "no_data", "message": "Could not locate park factors table.",
                           "url": PARK_FACTORS_URL})

    header_row = table.find("tr")
    headers = [
        (th.get("data-column") or th.get("data-sort") or th.get_text(strip=True)).lower()
        for th in header_row.find_all(["th", "td"])
    ]
    def _bpp_wind_direction(td) -> str | None:
        """Extract BPP's stadium-relative wind direction from the SVG image filename.
        SVG names: InCenter, InLeftCenter, OutLeft, FromLeft, etc.
        Returns 'in', 'out', 'cross', or None if indeterminate."""
        img = td.find("img")
        if not img:
            return None
        src = (img.get("src") or "").lower()
        name = src.rsplit("/", 1)[-1].replace(".svg", "")
        if name.startswith("in"):
            return "in"
        if name.startswith("out"):
            return "out"
        if name.startswith("from"):
            return "cross"
        return None

    games = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        game = {(headers[i] if i < len(headers) else f"col{i}"):
                (td.get("data-sort") or td.get_text(strip=True))
                for i, td in enumerate(cells)}
        # Enrich windforecast cells with BPP's stadium-relative direction (In/Out/Cross).
        # data-sort only has the speed; direction is encoded in the SVG image filename.
        for col_name in ("windforecast1", "windforecast2", "windforecast3"):
            col_idx = next((i for i, h in enumerate(headers) if h == col_name), None)
            if col_idx is not None and col_idx < len(cells):
                direction = _bpp_wind_direction(cells[col_idx])
                if direction:
                    game[col_name.replace("forecast", "direction")] = direction
        if game:
            games.append(game)

    if not games:
        return json.dumps({"status": "no_data", "message": "Park factors table had no rows.",
                           "url": PARK_FACTORS_URL})
    return json.dumps({"status": "success", "count": len(games), "games": games}, indent=2)


def fetch_pitcher_matchups() -> str:
    session = _get_bpp_session()
    if not session:
        return fetch_pitcher_matchups_fallback()
    try:
        resp = session.get(MATCHUPS_URL, timeout=20)
        resp.raise_for_status()
        if "Secure Checkout" in resp.text:
            return fetch_pitcher_matchups_fallback()
    except requests.RequestException as exc:
        return json.dumps({"status": "error", "message": str(exc)})

    soup  = BeautifulSoup(resp.text, "lxml")
    table = (soup.find("table", {"id": "matchupTable"})
             or soup.find("table", class_="proj-table")
             or (soup.find_all("table") or [None])[0])

    if not table:
        return json.dumps({"status": "no_data", "message": "Could not locate matchup table.",
                           "url": MATCHUPS_URL})

    header_row = table.find("tr")
    headers = [
        (th.get("data-column") or th.get_text(strip=True)).lower()
        for th in header_row.find_all(["th", "td"])
    ]
    matchups = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        matchup = {(headers[i] if i < len(headers) else f"col{i}"):
                   (td.get("data-sort") or td.get_text(strip=True))
                   for i, td in enumerate(cells)}
        if matchup:
            matchups.append(matchup)

    if not matchups:
        return json.dumps({"status": "no_data", "message": "Matchup table had no rows.",
                           "url": MATCHUPS_URL})
    return json.dumps({"status": "success", "count": len(matchups),
                       "matchups": matchups[:100]}, indent=2)


def fetch_statcast_batter_stats(player: str) -> str:
    season = date.today().year
    url = (
        f"{SAVANT_BASE}/leaderboard/custom"
        f"?year={season}&type=batter&filter=&sort=4&sortDir=desc&min=10"
        f"&selections=barrel_batted_rate,hard_hit_percent,hr_flyballs_rate_batter,"
        f"xiso,sweet_spot_percent,pull_percent,exit_velocity_avg,launch_angle_avg"
        f"&chart=false&x=barrel_batted_rate&y=barrel_batted_rate"
        f"&r=no&exactNameSearch=false&csv=true"
    )
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return json.dumps({"status": "error", "message": str(exc)})

    try:
        reader  = csv.DictReader(io.StringIO(resp.text))
        rows    = list(reader)
        search  = player.lower()
        matches = [r for r in rows
                   if search in (r.get("last_name, first_name") or r.get("player_name") or "").lower()]
    except Exception as exc:
        return json.dumps({"status": "error", "message": f"CSV parse error: {exc}"})

    if not matches:
        return json.dumps({"status": "not_found", "player": player,
                           "message": f"No Statcast data found for '{player}' in {season}."})

    hit = matches[0]
    return json.dumps({
        "status":           "success",
        "player":           hit.get("last_name, first_name") or hit.get("player_name"),
        "season":           season,
        "pa":               hit.get("pa"),
        "barrel_rate":      hit.get("barrel_batted_rate"),
        "hard_hit_pct":     hit.get("hard_hit_percent"),
        "hr_fb_ratio":      hit.get("hr_flyballs_rate_batter"),
        "xiso":             hit.get("xiso"),
        "sweet_spot_pct":   hit.get("sweet_spot_percent"),
        "pull_pct":         hit.get("pull_percent"),
        "avg_exit_velo":    hit.get("exit_velocity_avg"),
        "avg_launch_angle": hit.get("launch_angle_avg"),
    }, indent=2)


def fetch_statcast_pitcher_stats(pitcher: str) -> str:
    season = date.today().year
    url = (
        f"{SAVANT_BASE}/leaderboard/custom"
        f"?year={season}&type=pitcher&filter=&sort=4&sortDir=desc&min=10"
        f"&selections=barrel_batted_rate,hard_hit_percent,hr_flyball_rate,"
        f"exit_velocity_avg,xfip,fb_percent"
        f"&chart=false&x=barrel_batted_rate&y=barrel_batted_rate"
        f"&r=no&exactNameSearch=false&csv=true"
    )
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return json.dumps({"status": "error", "message": str(exc)})

    try:
        reader  = csv.DictReader(io.StringIO(resp.text))
        rows    = list(reader)
        search  = pitcher.lower()
        matches = [r for r in rows
                   if search in (r.get("last_name, first_name") or r.get("player_name") or "").lower()]
    except Exception as exc:
        return json.dumps({"status": "error", "message": f"CSV parse error: {exc}"})

    if not matches:
        return json.dumps({"status": "not_found", "pitcher": pitcher,
                           "message": f"No Statcast data found for '{pitcher}' in {season}."})

    hit = matches[0]
    return json.dumps({
        "status":               "success",
        "pitcher":              hit.get("last_name, first_name") or hit.get("player_name"),
        "season":               season,
        "batters_faced":        hit.get("pa"),
        "barrel_rate_allowed":  hit.get("barrel_batted_rate"),
        "hard_hit_pct_allowed": hit.get("hard_hit_percent"),
        "hr_fb_ratio_allowed":  hit.get("hr_flyball_rate"),
        "avg_exit_velo_against":hit.get("exit_velocity_avg"),
        "xfip":                 hit.get("xfip"),
        "fb_pct":               hit.get("fb_percent"),
    }, indent=2)


def fetch_confirmed_lineups(game_date: str = None) -> str:
    target_date = game_date or date.today().isoformat()
    # Confirmed batting orders live in game.lineups.awayPlayers/homePlayers (not teams.*.battingOrder)
    # probablePitcher(person) hydration only returns id/name/link — pitchHand needs a separate people call
    url = (f"{MLB_API_BASE}/schedule"
           f"?sportId=1&date={target_date}"
           f"&hydrate=lineups(person),probablePitcher,team,venue")
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        return json.dumps({"status": "error", "message": str(exc)})

    dates = data.get("dates", [])
    if not dates:
        return json.dumps({"status": "no_games", "date": target_date,
                           "message": "No games scheduled for this date."})

    # ── Collect all team IDs for roster fetching ─────────────────────────────
    team_ids = set()
    pitcher_ids = set()
    for game in dates[0].get("games", []):
        for side_key in ("away", "home"):
            team = game.get("teams", {}).get(side_key, {}).get("team", {})
            if team.get("id"):
                team_ids.add(team["id"])
            sp = game.get("teams", {}).get(side_key, {}).get("probablePitcher", {})
            if sp.get("id"):
                pitcher_ids.add(sp["id"])

    # Batch fetch rosters for all teams
    team_rosters: dict[int, list] = {}
    for team_id in team_ids:
        try:
            roster_url = f"https://statsapi.mlb.com/api/v1/teams/{team_id}/roster"
            resp = requests.get(roster_url, timeout=5)
            resp.raise_for_status()
            roster_data = resp.json()
            team_rosters[team_id] = roster_data.get("roster", [])
        except Exception:
            team_rosters[team_id] = []

    # Batch people call — gets pitchHand for all starting pitchers in one request
    pitcher_throws: dict[int, str] = {}
    if pitcher_ids:
        try:
            p_resp = requests.get(
                f"{MLB_API_BASE}/people",
                params={"personIds": ",".join(str(i) for i in pitcher_ids),
                        "hydrate": "currentTeam"},
                timeout=10,
            )
            for person in p_resp.json().get("people", []):
                pid  = person.get("id")
                hand = (person.get("pitchHand") or {}).get("code", "?")
                if pid:
                    pitcher_throws[pid] = hand
        except Exception:
            pass

    games_out = []
    for game in dates[0].get("games", []):
        lineup_data = game.get("lineups", {})
        away_side   = game.get("teams", {}).get("away", {})
        home_side   = game.get("teams", {}).get("home", {})

        def team_info(side, lineup_players, team_id):
            team = side.get("team", {})
            sp   = side.get("probablePitcher", {})
            sp_id    = sp.get("id")
            sp_throws = pitcher_throws.get(sp_id, "?") if sp_id else "?"

            # Get confirmed batters from lineup
            confirmed_batters = []
            for p in lineup_players:
                bat_side = (p.get("batSide") or {}).get("code", "?")
                pid      = p.get("id")
                pname    = p.get("fullName")
                confirmed_batters.append({
                    "id":       pid,
                    "name":     pname,
                    "bat_side": bat_side,
                    "status":   "confirmed",  # In confirmed lineup
                })
                # Persist handedness — confirmed lineup is the most reliable source
                if pid and pname and bat_side and bat_side != "?":
                    upsert_player_attr(pid, pname, bat_side=bat_side)

            # Get all roster players with status
            all_players = []
            roster = team_rosters.get(team_id, [])
            
            # Create set of confirmed player IDs for quick lookup
            confirmed_ids = {p["id"] for p in confirmed_batters if p["id"]}
            
            # Add confirmed players first
            all_players.extend(confirmed_batters)
            
            # Add roster players not in confirmed lineup
            for player_entry in roster:
                if not isinstance(player_entry, dict):
                    continue

                player_info = player_entry.get("person", {})
                player_id   = player_info.get("id")
                player_name = player_info.get("fullName")

                if not player_name or player_id in confirmed_ids:
                    continue

                # Skip pitchers — not HR candidates
                if (player_entry.get("position") or {}).get("type") == "Pitcher":
                    continue

                # bat_side is available from the roster API even for non-lineup players
                bat_side = (player_info.get("batSide") or {}).get("code", "?")

                # Persist to DB so we have it even when API doesn't return it next time
                if player_id and player_name and bat_side and bat_side != "?":
                    upsert_player_attr(player_id, player_name, bat_side=bat_side)

                all_players.append({
                    "id":       player_id,
                    "name":     player_name,
                    "bat_side": bat_side,
                    "status":   "waiting",  # On roster, waiting for lineup confirmation
                })

            return {
                "team":             team.get("name"),
                "team_id":          team.get("id"),
                "starting_pitcher": sp.get("fullName"),
                "pitcher_id":       sp_id,
                "pitcher_throws":   sp_throws,
                "lineup_confirmed": bool(confirmed_batters),
                "batting_order":    [b["name"] for b in confirmed_batters],
                "batters":          all_players,  # Now includes all roster players with status
            }

        games_out.append({
            "game_pk":   game.get("gamePk"),
            "venue":     game.get("venue", {}).get("name"),
            "venue_id":  game.get("venue", {}).get("id"),
            "game_time": game.get("gameDate"),
            "status":    game.get("status", {}).get("detailedState"),
            "away":      team_info(away_side, lineup_data.get("awayPlayers", []), away_side.get("team", {}).get("id")),
            "home":      team_info(home_side, lineup_data.get("homePlayers", []), home_side.get("team", {}).get("id")),
        })

    return json.dumps({"status": "success", "date": target_date,
                       "game_count": len(games_out), "games": games_out}, indent=2)


def fetch_hr_prop_odds(player: str = None) -> str:
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        return json.dumps({
            "status":  "no_api_key",
            "message": "ODDS_API_KEY not set in api/.env",
        })

    try:
        events_resp = requests.get(
            f"{ODDS_API_BASE}/sports/baseball_mlb/events?apiKey={api_key}", timeout=15)
        events_resp.raise_for_status()
        events = events_resp.json()
    except requests.RequestException as exc:
        return json.dumps({"status": "error", "message": str(exc)})

    if not events:
        return json.dumps({"status": "no_events", "message": "No MLB events found today."})

    props = []
    for event in events[:6]:
        event_id  = event.get("id")
        away, home = event.get("away_team"), event.get("home_team")
        try:
            p_resp = requests.get(
                f"{ODDS_API_BASE}/sports/baseball_mlb/events/{event_id}/odds"
                f"?apiKey={api_key}&regions=us&markets=batter_home_runs&oddsFormat=american",
                timeout=15)
            if p_resp.status_code == 422:
                continue
            p_resp.raise_for_status()
            p_data = p_resp.json()
        except requests.RequestException:
            continue

        for bookmaker in p_data.get("bookmakers", [])[:3]:
            for market in bookmaker.get("markets", []):
                if market.get("key") != "batter_home_runs":
                    continue
                for outcome in market.get("outcomes", []):
                    name  = outcome.get("name", "")
                    price = outcome.get("price")
                    if player and player.lower() not in name.lower():
                        continue
                    props.append({
                        "player":    name,
                        "matchup":   f"{away} @ {home}",
                        "bookmaker": bookmaker.get("title"),
                        "odds":      f"+{price}" if price > 0 else str(price),
                    })

    if not props:
        return json.dumps({"status": "no_data",
                           "message": f"No HR props found" + (f" for '{player}'." if player else ".")})

    best = {}
    for p in props:
        name = p["player"]
        try:
            raw = int(str(p["odds"]).replace("+", ""))
        except ValueError:
            raw = 9999
        if name not in best or raw < int(str(best[name]["odds"]).replace("+", "")):
            best[name] = p

    return json.dumps({
        "status":      "success",
        "props_found": len(best),
        "hr_props":    sorted(best.values(), key=lambda x: int(str(x["odds"]).replace("+", ""))),
    }, indent=2)


def _american_to_implied_prob(odds) -> float:
    """Convert American odds (int or string like '+275' or '-150') to implied probability (0–1)."""
    try:
        o = int(str(odds).replace("+", ""))
        if o > 0:
            return 100 / (o + 100)
        else:
            return abs(o) / (abs(o) + 100)
    except (ValueError, TypeError):
        return 0.0


def _american_to_decimal(odds: int) -> float:
    """Convert American odds to decimal."""
    return (odds / 100) + 1 if odds > 0 else (100 / abs(odds)) + 1


def _compute_ev(pinnacle_prob: float, best_odds_int: int, stake: float = 10.0) -> float:
    """Expected value of a $stake bet at best_odds using Pinnacle prob as true probability."""
    profit = stake * (_american_to_decimal(best_odds_int) - 1)
    return round(pinnacle_prob * profit - (1 - pinnacle_prob) * stake, 2)


def _compute_kelly(pinnacle_prob: float, best_odds_int: int,
                   bankroll: float = 200.0, max_fraction: float = 0.15) -> float:
    """Kelly Criterion bet size, capped at max_fraction of bankroll."""
    b = _american_to_decimal(best_odds_int) - 1
    if b <= 0:
        return 0.0
    kelly_frac = (b * pinnacle_prob - (1 - pinnacle_prob)) / b
    if kelly_frac <= 0:
        return 0.0
    return round(min(kelly_frac * bankroll, max_fraction * bankroll), 2)


# MLB team abbreviation/city → stadium name (for park factor venue matching)
# BallparkPal Park-Factors game strings use "Away @ Home" format; the home team is the venue.
_TEAM_VENUE: dict[str, str] = {
    # Full name fragments and abbreviations both mapped to venue name
    "yankees": "yankee stadium",        "nyy": "yankee stadium",
    "red sox": "fenway park",           "bos": "fenway park",
    "cubs":    "wrigley field",         "chc": "wrigley field",
    "dodgers": "dodger stadium",        "lad": "dodger stadium",
    "giants":  "oracle park",           "sf":  "oracle park", "sfg": "oracle park",
    "rockies": "coors field",           "col": "coors field",
    "orioles": "camden yards",          "bal": "camden yards",
    "phillies":"citizens bank park",    "phi": "citizens bank park",
    "braves":  "truist park",           "atl": "truist park",
    "astros":  "minute maid park",      "hou": "minute maid park",
    "mets":    "citi field",            "nym": "citi field",
    "cardinals":"busch stadium",        "stl": "busch stadium",
    "tigers":  "comerica park",         "det": "comerica park",
    "pirates": "pnc park",              "pit": "pnc park",
    "padres":  "petco park",            "sd":  "petco park", "sdp": "petco park",
    "mariners":"t-mobile park",         "sea": "t-mobile park",
    "twins":   "target field",          "min": "target field",
    "brewers": "american family field", "mil": "american family field",
    "rangers": "globe life field",      "tex": "globe life field",
    "angels":  "angel stadium",         "laa": "angel stadium",
    "athletics":"athletics ballpark",   "ath": "athletics ballpark", "oak": "athletics ballpark",
    "rays":    "tropicana field",       "tb":  "tropicana field", "tbr": "tropicana field",
    "blue jays":"rogers centre",        "tor": "rogers centre",
    "white sox":"guaranteed rate field","chw": "guaranteed rate field", "cws": "guaranteed rate field",
    "indians": "progressive field",     "cle": "progressive field", "guardians": "progressive field",
    "royals":  "kauffman stadium",      "kc":  "kauffman stadium",
    "nationals":"nationals park",       "was": "nationals park", "wsh": "nationals park",
    "marlins": "loanDepot park",        "mia": "loanDepot park",
    "reds":    "great american ball park", "cin": "great american ball park",
    "diamondbacks":"chase field",       "ari": "chase field",
}


# Fixed dome — roof never opens
_FIXED_DOMES: frozenset[str] = frozenset({
    "tropicana field",           # Tampa Bay Rays
})

# Retractable-roof stadiums — BPP signals roof status via windreceptiveness="Roof Closed"
# When roof is closed BPP returns temp=0 and windforecast="Variable"; treat as dome.
# When roof is open BPP returns real weather; treat as outdoor stadium.
_RETRACTABLE_STADIUMS: frozenset[str] = frozenset({
    "rogers centre",             # Toronto Blue Jays
    "american family field",     # Milwaukee Brewers
    "chase field",               # Arizona Diamondbacks
    "minute maid park",          # Houston Astros
    "daikin park",               # Houston Astros (renamed from Minute Maid)
    "loandepot park",            # Miami Marlins
    "t-mobile park",             # Seattle Mariners
    "globe life field",          # Texas Rangers
})

# Union — used for display/scoring helpers that only need to know "is this ever a dome"
_DOME_STADIUMS: frozenset[str] = _FIXED_DOMES | _RETRACTABLE_STADIUMS

# Static venue constants — park HR factor and outfield size for every MLB park.
# Used as fallback when BPP data is unavailable or when a player is added via roster
# fallback before live park data has been fetched. Keyed by lowercase venue name.
_VENUE_PARK_CONSTANTS: dict[str, dict] = {
    "yankee stadium":                   {"park_hr_factor": 87.0,  "outfield_size": "Variable"},
    "fenway park":                      {"park_hr_factor": 79.0,  "outfield_size": "Variable"},
    "wrigley field":                    {"park_hr_factor": 103.0, "outfield_size": "Medium"},
    "dodger stadium":                   {"park_hr_factor": 102.0, "outfield_size": "Medium"},
    "uniqlo field at dodger stadium":   {"park_hr_factor": 102.0, "outfield_size": "Medium"},
    "oracle park":                      {"park_hr_factor": 79.0,  "outfield_size": "Variable"},
    "coors field":                      {"park_hr_factor": 113.0, "outfield_size": "X-Large"},
    "oriole park at camden yards":      {"park_hr_factor": 105.0, "outfield_size": "Variable"},
    "camden yards":                     {"park_hr_factor": 105.0, "outfield_size": "Variable"},
    "citizens bank park":               {"park_hr_factor": 85.0,  "outfield_size": "Small"},
    "truist park":                      {"park_hr_factor": 90.0,  "outfield_size": "Medium"},
    "minute maid park":                 {"park_hr_factor": 106.0, "outfield_size": "Medium"},
    "daikin park":                      {"park_hr_factor": 106.0, "outfield_size": "Medium"},
    "citi field":                       {"park_hr_factor": 86.0,  "outfield_size": "Medium"},
    "busch stadium":                    {"park_hr_factor": 103.0, "outfield_size": "Large"},
    "comerica park":                    {"park_hr_factor": 83.0,  "outfield_size": "Large"},
    "pnc park":                         {"park_hr_factor": 76.0,  "outfield_size": "Variable"},
    "petco park":                       {"park_hr_factor": 95.0,  "outfield_size": "Medium"},
    "t-mobile park":                    {"park_hr_factor": 110.0, "outfield_size": "Small"},
    "target field":                     {"park_hr_factor": 65.0,  "outfield_size": "Medium"},
    "american family field":            {"park_hr_factor": 103.0, "outfield_size": "Small"},
    "globe life field":                 {"park_hr_factor": 90.0,  "outfield_size": "Large"},
    "angel stadium":                    {"park_hr_factor": 99.0,  "outfield_size": "Small"},
    "sutter health park":               {"park_hr_factor": 132.0, "outfield_size": "Large"},
    "athletics ballpark":               {"park_hr_factor": 105.0, "outfield_size": "Medium"},
    "tropicana field":                  {"park_hr_factor": 96.0,  "outfield_size": "Medium"},
    "rogers centre":                    {"park_hr_factor": 100.0, "outfield_size": "Medium"},
    "guaranteed rate field":            {"park_hr_factor": 119.0, "outfield_size": "Small"},
    "rate field":                       {"park_hr_factor": 119.0, "outfield_size": "Small"},
    "progressive field":                {"park_hr_factor": 90.0,  "outfield_size": "Small"},
    "kauffman stadium":                 {"park_hr_factor": 115.0, "outfield_size": "X-Large"},
    "nationals park":                   {"park_hr_factor": 88.0,  "outfield_size": "Medium"},
    "loandepot park":                   {"park_hr_factor": 81.0,  "outfield_size": "Large"},
    "loanDepot park":                   {"park_hr_factor": 81.0,  "outfield_size": "Large"},
    "great american ball park":         {"park_hr_factor": 114.0, "outfield_size": "Small"},
    "chase field":                      {"park_hr_factor": 95.0,  "outfield_size": "Large"},
    # Mexico City series (7,350 ft elevation — higher than Coors; very HR-friendly thin air)
    "estadio alfredo harp helu":        {"park_hr_factor": 120.0, "outfield_size": "Medium"},
}


# Venue → OWM city query string (used to fetch wind direction when BPP doesn't provide degrees)
_VENUE_CITY: dict[str, str] = {
    "yankee stadium":           "New York,US",
    "fenway park":              "Boston,US",
    "wrigley field":            "Chicago,US",
    "dodger stadium":           "Los Angeles,US",
    "oracle park":              "San Francisco,US",
    "coors field":              "Denver,US",
    "camden yards":             "Baltimore,US",
    "citizens bank park":       "Philadelphia,US",
    "truist park":              "Atlanta,US",
    "minute maid park":         "Houston,US",
    "daikin park":              "Houston,US",
    "citi field":               "New York,US",
    "busch stadium":            "St. Louis,US",
    "comerica park":            "Detroit,US",
    "pnc park":                 "Pittsburgh,US",
    "petco park":               "San Diego,US",
    "t-mobile park":            "Seattle,US",
    "target field":             "Minneapolis,US",
    "american family field":    "Milwaukee,US",
    "globe life field":         "Arlington,US",
    "angel stadium":            "Anaheim,US",
    "athletics ballpark":       "Las Vegas,US",
    "tropicana field":          "St. Petersburg,US",
    "rogers centre":            "Toronto,CA",
    "guaranteed rate field":    "Chicago,US",
    "rate field":               "Chicago,US",
    "progressive field":        "Cleveland,US",
    "kauffman stadium":         "Kansas City,US",
    "nationals park":           "Washington,US",
    "loandepot park":           "Miami,US",
    "great american ball park": "Cincinnati,US",
    "chase field":              "Phoenix,US",
    "loanDepot park":           "Miami,US",
    "sutter health park":       "Sacramento,US",
    "sahlen field":             "Buffalo,US",
}


def _team_to_venue(team_str: str) -> str | None:
    """Try to resolve a team name/abbreviation to a stadium name."""
    t = team_str.strip().lower()
    # Direct lookup
    if t in _TEAM_VENUE:
        return _TEAM_VENUE[t]
    # Partial match — team fragment in key
    for key, venue in _TEAM_VENUE.items():
        if key in t or t in key:
            return venue
    return None


def _platoon_edge(bat_side: str, pitcher_throws: str) -> str:
    """
    Return platoon advantage label.
    Advantage = batter faces opposite-hand pitcher (hits more HRs).
    Switch hitters always have advantage.
    """
    if not bat_side or not pitcher_throws or "?" in (bat_side + pitcher_throws):
        return ""
    if bat_side == "S":
        return "PLATOON+"     # switch hitter always advantaged
    if bat_side != pitcher_throws:
        return "PLATOON+"     # opposite hands = advantage
    return "platoon-"         # same hands = disadvantage


def _safe_float(val) -> float | None:
    """Convert a value to float, returning None on failure."""
    try:
        f = float(val)
        return round(f, 2)
    except (TypeError, ValueError):
        return None


def _parse_pct(val) -> float | None:
    """
    Parse a percentage value that may arrive in any of these formats:
      '26.5%'  → 26.5
      '0.265'  → 26.5  (BallparkPal data-sort uses decimal form)
      '26.5'   → 26.5
    Returns None on failure.
    """
    if val is None:
        return None
    s = str(val).strip().rstrip("%")
    try:
        f = float(s)
        # BallparkPal data-sort stores as decimal (0.0–1.0); convert to %
        if 0.0 < f <= 1.0:
            f = round(f * 100, 2)
        return round(f, 2)
    except (TypeError, ValueError):
        return None


def _safe_int(val) -> int | None:
    """Convert a value to int, returning None on failure."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def fetch_odds_comparison(confirmed_teams: set | None = None) -> str:
    """
    Fetch HR prop odds (over 0.5 HRs) from all available sportsbooks via The Odds API.
    Includes US books (DraftKings, FanDuel, BetMGM, etc.) + Pinnacle (EU region).

    Pinnacle is the sharpest sportsbook in the world — their line is the closest
    thing to a true market price. If ProphetX/Novig beats Pinnacle, you have real edge.

    For each player computes:
      - Pinnacle line (sharp benchmark)
      - Best available line across all books + which book offers it
      - Consensus implied probability (average across all books, strips vig)
      - Value edge = consensus_prob − best_odds_implied_prob (positive = value)
      - VALUE flag when edge >= 3 percentage points

    Note: Novig and ProphetX are not available on The Odds API. Compare your
    platform's displayed odds against the Pinnacle and Best Odds columns manually.
    """
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        return json.dumps({"status": "no_api_key",
                           "message": "ODDS_API_KEY not set in api/.env"})

    # ── Intra-day cache: reuse today's odds rather than burning API quota ─────
    _today = date.today().isoformat()
    _cache_path = Path("cache") / f"odds_{_today}.json"
    if _cache_path.exists():
        try:
            cached = json.loads(_cache_path.read_text())
            if cached.get("status") == "success":
                return _cache_path.read_text()
        except Exception:
            pass  # corrupt cache — fall through to fresh fetch

    # ── Fetch today's MLB events (auto-failover to backup key on quota) ───────
    def _get_events(key: str):
        r = requests.get(
            f"{ODDS_API_BASE}/sports/baseball_mlb/events?apiKey={key}",
            timeout=15)
        return r

    try:
        events_resp = _get_events(api_key)
        if events_resp.status_code == 401:
            backup_key = os.getenv("ODDS_API_KEY_BACKUP")
            if backup_key:
                print("[ODDS] Primary key quota exhausted — switching to backup key")
                events_resp = _get_events(backup_key)
                if events_resp.status_code == 200:
                    api_key = backup_key  # use backup for all subsequent calls
            if events_resp.status_code == 401:
                return json.dumps({
                    "status": "quota_exceeded",
                    "message": "Both Odds API keys have exhausted their quota. Keys reset monthly.",
                })
        events_resp.raise_for_status()
        events = events_resp.json()
    except requests.RequestException as exc:
        return json.dumps({"status": "error", "message": str(exc)})

    if not events:
        return json.dumps({"status": "no_events",
                           "message": "No MLB events found today."})

    # Filter to games with confirmed lineups to conserve Odds API quota.
    # confirmed_teams is a set of full team names (e.g. "New York Yankees").
    # When None, fetch all events (original behaviour — backwards compatible).
    if confirmed_teams is not None:
        if not confirmed_teams:
            return json.dumps({"status": "no_confirmed_lineups",
                               "message": "No confirmed lineups yet — skipping odds fetch.",
                               "comparisons": []})
        events = [
            e for e in events
            if e.get("away_team") in confirmed_teams
            or e.get("home_team") in confirmed_teams
        ]

    # ── For each game, fetch HR prop odds from US + EU (Pinnacle) ─────────────
    # player_name → {"matchup": str, "books": {book_title: odds_int},
    #                "pinnacle": odds_int | None}
    all_player_odds: dict[str, dict] = {}

    for event in events[:12]:          # cap at 12 games to conserve API quota
        event_id = event.get("id")
        away     = event.get("away_team", "")
        home     = event.get("home_team", "")
        matchup  = f"{away} @ {home}"
        try:
            p_resp = requests.get(
                f"{ODDS_API_BASE}/sports/baseball_mlb/events/{event_id}/odds"
                f"?apiKey={api_key}&regions=us,eu&markets=batter_home_runs"
                f"&oddsFormat=american",
                timeout=15)
            if p_resp.status_code == 422:
                continue
            if p_resp.status_code == 401:
                err = p_resp.json() if p_resp.content else {}
                return json.dumps({
                    "status": "quota_exceeded",
                    "message": f"Odds API quota exhausted — {err.get('message', 'upgrade at the-odds-api.com')}",
                })
            p_resp.raise_for_status()
            p_data = p_resp.json()
        except requests.RequestException:
            continue

        for bookmaker in p_data.get("bookmakers", []):
            book_key   = bookmaker.get("key", "")
            book_title = bookmaker.get("title", book_key)
            is_pinnacle = book_key == "pinnacle"

            for market in bookmaker.get("markets", []):
                if market.get("key") != "batter_home_runs":
                    continue
                for outcome in market.get("outcomes", []):
                    # Player name lives in "description"; "name" is "Over"/"Under"
                    player_name = (outcome.get("description") or
                                   outcome.get("name", "")).strip()
                    price       = outcome.get("price")
                    point       = outcome.get("point")
                    direction   = outcome.get("name", "")

                    # Only standard "will hit a HR" prop (over 0.5), skip 2+ / 3+
                    if direction != "Over" or point != 0.5:
                        continue
                    if not player_name or price is None:
                        continue

                    if player_name not in all_player_odds:
                        all_player_odds[player_name] = {
                            "matchup":  matchup,
                            "books":    {},
                            "pinnacle": None,
                        }

                    existing = all_player_odds[player_name]["books"].get(book_title)
                    if existing is None or price > existing:
                        all_player_odds[player_name]["books"][book_title] = price

                    if is_pinnacle:
                        curr_pin = all_player_odds[player_name]["pinnacle"]
                        if curr_pin is None or price > curr_pin:
                            all_player_odds[player_name]["pinnacle"] = price

    if not all_player_odds:
        return json.dumps({
            "status":  "no_data",
            "message": "No HR prop data returned. Books typically post player props "
                       "2–4 hours before first pitch — check back closer to game time.",
        })

    def _fmt(o: int | None) -> str:
        if o is None:
            return "—"
        return f"+{o}" if o > 0 else str(o)

    # ── Compute consensus + value per player ──────────────────────────────────
    results = []
    for player_name, info in all_player_odds.items():
        books = info["books"]
        if not books:
            continue

        probs = {book: _american_to_implied_prob(odds)
                 for book, odds in books.items()}

        consensus_prob = sum(probs.values()) / len(probs)
        best_book      = max(books, key=lambda b: books[b])
        best_odds_int  = books[best_book]
        best_prob      = probs[best_book]
        value_edge     = consensus_prob - best_prob   # positive = value

        pinnacle_odds  = info["pinnacle"]
        pinnacle_prob  = _american_to_implied_prob(pinnacle_odds) if pinnacle_odds else None

        # EV and Kelly: prefer Pinnacle (sharpest), fall back to consensus
        true_prob      = pinnacle_prob if pinnacle_prob is not None else consensus_prob
        ev     = _compute_ev(true_prob, best_odds_int)
        kelly  = _compute_kelly(true_prob, best_odds_int)

        results.append({
            "player":         player_name,
            "matchup":        info["matchup"],
            "books_sampled":  len(books),
            "pinnacle":       _fmt(pinnacle_odds),
            "pinnacle_prob":  f"{pinnacle_prob * 100:.1f}%" if pinnacle_prob else f"{consensus_prob * 100:.1f}% (consensus)",
            "best_book":      best_book,
            "best_odds":      _fmt(best_odds_int),
            "consensus_prob": f"{consensus_prob * 100:.1f}%",
            "value_edge":     round(value_edge * 100, 1),
            "value_flag":     "VALUE" if value_edge >= 0.03 else "",
            "ev_10":          ev,        # EV on a $10 bet at best available odds
            "kelly_size":     kelly,     # Kelly optimal bet size ($200 bankroll)
            "all_books":      {book: _fmt(o) for book, o in
                               sorted(books.items(), key=lambda x: x[1], reverse=True)},
        })

    results.sort(key=lambda x: x["value_edge"], reverse=True)

    out = json.dumps({
        "status":        "success",
        "players_found": len(results),
        "note": (
            "Pinnacle = sharpest market benchmark. "
            "value_edge (pp) = consensus_prob - best_odds_implied_prob. "
            "Compare Novig/ProphetX odds manually to the Pinnacle column."
        ),
        "comparisons": results,
    }, indent=2)

    # Cache for the rest of today so re-runs don't burn quota
    try:
        _cache_path.parent.mkdir(parents=True, exist_ok=True)
        _cache_path.write_text(out)
    except Exception:
        pass

    return out


def fetch_park_factors_fallback() -> str:
    """
    Fallback park factors using FanGraphs (season-level) + OpenWeatherMap (live weather).
    Used when BallparkPal credentials are unavailable.
    """
    season = date.today().year
    results = []

    # ── FanGraphs park factors ────────────────────────────────────────────────
    try:
        url  = FANGRAPHS_PF_URL.format(season=season)
        resp = requests.get(url, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
        soup   = BeautifulSoup(resp.text, "lxml")
        table  = soup.find("table", {"id": "GutsBoard1_dg1_ctl00"}) or soup.find("table")
        if table:
            headers_row = table.find("tr")
            hdrs = [th.get_text(strip=True).lower()
                    for th in headers_row.find_all(["th", "td"])]
            for row in table.find_all("tr")[1:]:
                cols = [td.get_text(strip=True) for td in row.find_all("td")]
                if len(cols) >= 2:
                    entry = {hdrs[i]: cols[i] for i in range(min(len(hdrs), len(cols)))}
                    results.append(entry)
    except Exception as exc:
        results = [{"error": f"FanGraphs scrape failed: {exc}"}]

    # ── Live weather per stadium ──────────────────────────────────────────────
    weather_key = os.getenv("OPENWEATHER_API_KEY", "").strip()
    weather_data = {}
    if weather_key:
        # Key MLB stadium cities
        stadiums = {
            "Yankee Stadium":      ("New York", "US"),
            "Fenway Park":         ("Boston", "US"),
            "Wrigley Field":       ("Chicago", "US"),
            "Dodger Stadium":      ("Los Angeles", "US"),
            "Oracle Park":         ("San Francisco", "US"),
            "Coors Field":         ("Denver", "US"),
            "Camden Yards":        ("Baltimore", "US"),
            "Citizens Bank Park":  ("Philadelphia", "US"),
            "Truist Park":         ("Atlanta", "US"),
            "Minute Maid Park":    ("Houston", "US"),
        }
        for stadium, (city, country) in stadiums.items():
            try:
                w = requests.get(OPENWEATHER_URL, params={
                    "q": f"{city},{country}", "appid": weather_key,
                    "units": "imperial",
                }, timeout=10).json()
                weather_data[stadium] = {
                    "temp_f":      round(w["main"]["temp"]),
                    "humidity":    w["main"]["humidity"],
                    "wind_mph":    round(w["wind"]["speed"]),
                    "wind_deg":    w["wind"].get("deg"),    # 0–360 degrees for arrow display
                    "description": w["weather"][0]["description"],
                }
            except Exception:
                pass

    return json.dumps({
        "status":       "fallback",
        "source":       "FanGraphs (season) + OpenWeatherMap (live)",
        "note":         "Add BALLPARKPAL_EMAIL/PASSWORD to api/.env for full BallparkPal data",
        "park_factors": results[:30],
        "weather":      weather_data,
    }, indent=2)


def fetch_pitcher_matchups_fallback() -> str:
    """
    Fallback matchup data computed from Statcast when BallparkPal is unavailable.
    Computes a matchup score from today's confirmed lineups + pitcher/batter Statcast stats.
    """
    today  = date.today().isoformat()
    season = date.today().year

    # Get today's confirmed lineups + starting pitchers
    try:
        resp = requests.get(
            f"{MLB_API_BASE}/schedule?sportId=1&date={today}"
            f"&hydrate=lineups,probablePitcher,team",
            timeout=15,
        )
        data   = resp.json()
        dates  = data.get("dates", [])
        games  = dates[0].get("games", []) if dates else []
    except Exception as exc:
        return json.dumps({"status": "error", "message": str(exc)})

    if not games:
        return json.dumps({"status": "no_games", "date": today})

    matchups = []
    for game in games:
        venue = game.get("venue", {}).get("name", "")
        for side_key, opp_key in [("away", "home"), ("home", "away")]:
            side     = game.get("teams", {}).get(side_key, {})
            opp_side = game.get("teams", {}).get(opp_key, {})
            pitcher  = opp_side.get("probablePitcher", {})
            order    = side.get("battingOrder", [])

            if not order or not pitcher:
                continue

            pitcher_name = pitcher.get("fullName", "")

            # Fetch pitcher Statcast stats once
            p_stats = _fetch_savant_csv(pitcher_name, season, "pitcher",
                                        "barrel_batted_rate,hard_hit_percent,"
                                        "hr_flyball_rate,fb_percent")

            for batter in order[:6]:   # top 6 in lineup only
                batter_name = batter.get("fullName", "")
                b_stats = _fetch_savant_csv(batter_name, season, "batter",
                                            "barrel_batted_rate,hard_hit_percent,"
                                            "hr_flyballs_rate_batter,pull_percent")

                # Compute a simple 0-10 matchup score
                score = _compute_matchup_score(b_stats, p_stats)

                matchups.append({
                    "batter":          batter_name,
                    "pitcher":         pitcher_name,
                    "venue":           venue,
                    "matchup_score":   score,
                    "batter_barrel":   b_stats.get("barrel_batted_rate"),
                    "batter_hard_hit": b_stats.get("hard_hit_percent"),
                    "batter_hr_fb":    b_stats.get("hr_flyballs_rate_batter"),
                    "pitcher_hr_fb":   p_stats.get("hr_flyball_rate"),
                    "pitcher_fb_pct":  p_stats.get("fb_percent"),
                })

    matchups.sort(key=lambda x: x["matchup_score"], reverse=True)
    return json.dumps({
        "status":   "fallback",
        "source":   "Statcast computed matchup scores (0-10)",
        "note":     "Add BALLPARKPAL_EMAIL/PASSWORD to api/.env for BallparkPal grades",
        "matchups": matchups[:60],
    }, indent=2)


def _fetch_savant_csv(player: str, season: int, player_type: str,
                      selections: str) -> dict:
    """Fetch a single player's Statcast stats from the leaderboard CSV."""
    url = (
        f"{SAVANT_BASE}/leaderboard/custom"
        f"?year={season}&type={player_type}&filter=&sort=4&sortDir=desc&min=5"
        f"&selections={selections}"
        f"&chart=false&r=no&exactNameSearch=false&csv=true"
    )
    try:
        resp    = requests.get(url, headers=_HEADERS, timeout=20)
        reader  = csv.DictReader(io.StringIO(resp.text))
        search  = player.lower()
        matches = [r for r in reader
                   if search in (r.get("last_name, first_name") or
                                 r.get("player_name") or "").lower()]
        return matches[0] if matches else {}
    except Exception:
        return {}


def _find_best_name_match(player_name: str, name_dict: dict) -> dict:
    """
    Find the best matching entry in a dict keyed by player names (in "last_name, first_name" format).
    
    Prioritizes:
    1. Exact match (case-insensitive) on full name
    2. Exact match on reversed name (first last → last, first format)
    3. Exact match on just last name
    4. Partial matches with more matching parts = better
    5. Fall back to any partial match
    
    Args:
        player_name: Full player name from lineup (e.g., "Kyle Schwarber")
        name_dict: Dict keyed by stat player names (e.g., "schwarber, kyle")
    
    Returns:
        The best matching dict value, or {} if no match found.
    """
    if not player_name or not name_dict:
        return {}
    
    player_lower = player_name.lower()
    parts = player_name.lower().split()
    
    # Try exact match on full name (both formats)
    for key in name_dict.keys():
        if not isinstance(key, str):
            continue
        key_lower = key.lower()
        # Check if full name matches in any order
        if key_lower == player_lower or key_lower == " ".join(reversed(parts)):
            return name_dict[key]

    # Try matching last name + first name initial to avoid sibling collisions (e.g. Josh vs Bo Naylor)
    if len(parts) >= 2:
        last_name  = parts[-1]
        first_init = parts[0][0]  # first letter of first name
        for key in name_dict.keys():
            if not isinstance(key, str):
                continue
            key_lower = key.lower()
            if key_lower.startswith(last_name + ","):
                # Require first-name initial to also match
                after_comma = key_lower.split(",", 1)[-1].strip()
                if after_comma.startswith(first_init):
                    return name_dict[key]

    # Count matching parts and prioritize best match
    best_match = None
    best_match_score = 0

    for key in name_dict.keys():
        if not isinstance(key, str):
            continue
        key_lower = key.lower()
        # Score based on how many name parts appear in the key
        match_count = sum(1 for part in parts if len(part) > 2 and part in key_lower)
        if match_count > best_match_score:
            best_match = name_dict[key]
            best_match_score = match_count
    
    return best_match if best_match else {}


def _compute_matchup_score(b_stats: dict, p_stats: dict) -> float:
    """Compute a 0-10 matchup score from batter + pitcher Statcast stats."""
    score = 5.0   # baseline

    # Batter power — barrel rate
    try:
        barrel = float(b_stats.get("barrel_batted_rate") or 0)
        if barrel >= 15:   score += 1.5
        elif barrel >= 10: score += 0.75
        elif barrel < 5:   score -= 1.0
    except (TypeError, ValueError):
        pass

    # Batter hard hit %
    try:
        hh = float(b_stats.get("hard_hit_percent") or 0)
        if hh >= 50:   score += 1.0
        elif hh >= 45: score += 0.5
        elif hh < 38:  score -= 0.5
    except (TypeError, ValueError):
        pass

    # Pitcher HR vulnerability
    try:
        p_hr_fb = float(p_stats.get("hr_flyball_rate") or 0)
        if p_hr_fb >= 15:   score += 1.5
        elif p_hr_fb >= 12: score += 0.75
        elif p_hr_fb < 8:   score -= 1.0
    except (TypeError, ValueError):
        pass

    # Pitcher fly ball rate (more FB = more HR opps)
    try:
        fb = float(p_stats.get("fb_percent") or 0)
        if fb >= 40:   score += 0.5
        elif fb < 30:  score -= 0.5
    except (TypeError, ValueError):
        pass

    return round(min(max(score, 0), 10), 1)


def fetch_recent_hr_form(days: int = 14) -> str:
    """Fetch HR leaders over the last N days from Baseball Savant HR events.

    Keyed by player_id (batter field) to avoid name collisions (e.g. two
    active players named Max Muncy with different MLB IDs).
    """
    import datetime as _dt
    today  = date.today()
    start  = (today - _dt.timedelta(days=days)).isoformat()
    end    = today.isoformat()
    season = today.year

    url = (
        f"{SAVANT_BASE}/statcast_search/csv"
        f"?all=true&hfAB=home_run%7C&hfGT=R%7C&hfSea={season}%7C"
        f"&player_type=batter&type=details"
        f"&game_date_gt={start}&game_date_lt={end}&csv=true"
    )
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return json.dumps({"status": "error", "message": str(exc)})

    try:
        text   = resp.text.lstrip("\ufeff")
        reader = csv.DictReader(io.StringIO(text))
        # Key by player_id — avoids name collisions (e.g. two players named "Muncy, Max")
        counts: dict[int, int] = {}
        id_to_name: dict[int, str] = {}
        for r in reader:
            pid_raw = (r.get("batter") or "").strip()
            name    = (r.get("player_name") or "").strip()
            if not pid_raw:
                continue
            try:
                pid = int(pid_raw)
            except ValueError:
                continue
            counts[pid] = counts.get(pid, 0) + 1
            if pid not in id_to_name and name:
                id_to_name[pid] = name
    except Exception as exc:
        return json.dumps({"status": "error", "message": f"CSV parse error: {exc}"})

    if not counts:
        return json.dumps({"status": "no_data",
                           "message": f"No HR data found for last {days} days."})

    leaders = [
        {"player_id": pid, "player": id_to_name.get(pid, ""), "hr_last_14d": str(cnt)}
        for pid, cnt in sorted(counts.items(), key=lambda x: x[1], reverse=True)
    ]
    return json.dumps({
        "status":       "success",
        "window_days":  days,
        "start":        start,
        "end":          end,
        "hr_leaders":   leaders,
    }, indent=2)
def check_lineup_availability(game_date: str = None) -> str:
    """Check how many teams have confirmed lineups posted for game_date."""
    target_date = game_date or date.today().isoformat()
    try:
        resp = requests.get(
            f"{MLB_API_BASE}/schedule"
            f"?sportId=1&date={target_date}&hydrate=lineups,team",
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        return json.dumps({"status": "error", "message": str(exc)})

    confirmed_teams = 0
    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            for side in ("away", "home"):
                if game.get("teams", {}).get(side, {}).get("battingOrder"):
                    confirmed_teams += 1

    return json.dumps({
        "status": "success",
        "date": target_date,
        "confirmed_sides": confirmed_teams,
    })




# ── Agent ──────────────────────────────────────────────────────────────────────

_SYSTEM = """You are the BallparkPal Predictor Agent for a home run betting system.

CRITICAL RULE: You MUST call the provided tools to get real data before answering.
NEVER use your training data to answer. NEVER invent player stats, lineups, or matchups.
If a tool returns no data, say so — do not substitute with guesses.

REQUIRED TOOL CALL ORDER:
1. fetch_confirmed_lineups        — ALWAYS call this first with today's date
2. fetch_pitcher_matchups         — BallparkPal matchup grades (0-10) + park-adjusted HR%
3. fetch_park_factors             — park HR factor, wind, temperature, humidity
4. fetch_statcast_batter_stats    — call for each candidate player
5. fetch_statcast_pitcher_stats   — call for each candidate's opposing pitcher
6. fetch_hr_prop_odds             — market odds (line movement signals sharp action)
PICK CRITERIA (priority order):
1. Lineup confirmed — skip any unconfirmed player
2. Odds tier — favorites (<+250) hitting at higher rates; weight them heavily
   early in the season before Statcast data builds up
4. Recent HR form (last 14 days) — players on a hot streak; more reliable than season
   averages in April when sample sizes are small
5. Statcast power profile — barrel rate >10% and hard hit% >45% = genuine power threat
   NOTE: early April data is sparse (<50 PA); treat these as supporting signals only
6. BallparkPal matchup grade >=7/10 — below 4 is a red flag
7. Pitcher vulnerability — high HR/FB allowed + high FB% = HR-prone
8. Park HR factor >1.0 — hitter-friendly environment
9. Weather — wind OUT 10+ mph: strong positive; wind IN: penalise heavily; temp <50F: negative
10. Market signal — odds shortening = sharp money

FOR EACH PICK OUTPUT (using ONLY data returned by tools):
- Player, matchup, batting position
- Barrel rate, hard hit %, HR/FB
- Matchup grade + park-adjusted HR%
- Park HR factor + wind + temperature
- Pitcher xFIP and FB%
- Market odds
- Our historical record
- Confidence: HIGH / MEDIUM / LOW
- Red flags (if any)"""

_TOOL_FNS = {
    "fetch_ballparkpal_projections": fetch_ballparkpal_projections,
    "fetch_park_factors":            fetch_park_factors,
    "fetch_pitcher_matchups":        fetch_pitcher_matchups,
    "fetch_statcast_batter_stats":   fetch_statcast_batter_stats,
    "fetch_statcast_pitcher_stats":  fetch_statcast_pitcher_stats,
    "fetch_confirmed_lineups":       fetch_confirmed_lineups,
    "fetch_hr_prop_odds":            fetch_hr_prop_odds,
}


# ── New analytical helpers ─────────────────────────────────────────────────────

def _fetch_pitcher_recent_form(pitcher_id: int, n_starts: int = 3) -> dict:
    """
    Fetch pitcher game log from the MLB Stats API.
    Returns blended HR/9: 60% last-3-starts + 40% season, plus raw values.
    A start = appearance with >= 3 innings pitched.
    """
    if not pitcher_id:
        return {}
    try:
        resp = requests.get(
            f"{MLB_API_BASE}/people/{pitcher_id}/stats",
            params={"stats": "gameLog", "group": "pitching",
                    "season": date.today().year, "limit": 40},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}

    all_logs = []
    for group in data.get("stats", []):
        for split in group.get("splits", []):
            stat = split.get("stat", {})
            try:
                ip = float(stat.get("inningsPitched") or 0)
            except (ValueError, TypeError):
                ip = 0
            if ip < 3:
                continue
            all_logs.append({
                "date":       split.get("date", ""),
                "hr_allowed": int(stat.get("homeRuns") or 0),
                "ip":         ip,
                "er":         int(stat.get("earnedRuns") or 0),
            })

    if not all_logs:
        return {}

    recent_logs = all_logs[:n_starts]
    recent_hr   = sum(g["hr_allowed"] for g in recent_logs)
    recent_ip   = sum(g["ip"] for g in recent_logs)
    recent_hr9  = round(recent_hr / recent_ip * 9, 2) if recent_ip else 0

    season_hr  = sum(g["hr_allowed"] for g in all_logs)
    season_ip  = sum(g["ip"] for g in all_logs)
    season_hr9 = round(season_hr / season_ip * 9, 2) if season_ip else 0

    # Blend: 60% recent form, 40% season — captures current vulnerability + baseline
    blended_hr9 = round(0.6 * recent_hr9 + 0.4 * season_hr9, 2)

    return {
        "starts_sampled": len(recent_logs),
        "hr_per_9":       blended_hr9,
        "recent_hr9":     recent_hr9,
        "season_hr9":     season_hr9,
        "total_hr":       recent_hr,
        "logs":           recent_logs,
    }


def _fetch_pitcher_career_splits(pitcher_id: int) -> dict:
    """
    Fetch career HR/9 vs LHB (sitCode=vl) and vs RHB (sitCode=vr) from MLB Stats API.
    Career totals give a large, reliable sample — more stable than season splits in April.
    Returns {"vs_lhb_hr9": float, "vs_rhb_hr9": float} or {} if insufficient data (< 20 IP).
    """
    if not pitcher_id:
        return {}
    try:
        resp = requests.get(
            f"{MLB_API_BASE}/people/{pitcher_id}/stats",
            params={"stats": "careerStatSplits", "group": "pitching",
                    "sitCodes": "vl,vr", "gameType": "R"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}

    result = {}
    for group in data.get("stats", []):
        for split in group.get("splits", []):
            code = split.get("split", {}).get("code", "")
            stat = split.get("stat", {})
            try:
                ip = float(stat.get("inningsPitched") or 0)
            except (ValueError, TypeError):
                ip = 0
            if ip < 20:
                continue
            hr  = int(stat.get("homeRuns") or 0)
            hr9 = round(hr / ip * 9, 2)
            if code == "vl":
                result["vs_lhb_hr9"] = hr9
            elif code == "vr":
                result["vs_rhb_hr9"] = hr9
    return result


def _fetch_pitcher_career_splits_batch(pitcher_hand_pairs: list[tuple[int, str]]) -> dict:
    """
    Batch-fetch career HR/9 vs batter handedness for multiple pitchers.
    pitcher_hand_pairs: [(pitcher_id, bat_side), ...] where bat_side in {"L","R","S"}
    Returns {pitcher_id: hr9_vs_hand_or_None}.
    """
    if not pitcher_hand_pairs:
        return {}
    results: dict = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        futs = {
            executor.submit(_fetch_pitcher_career_splits, pid): (pid, side)
            for pid, side in pitcher_hand_pairs
        }
        for fut in as_completed(futs):
            pid, bat_side = futs[fut]
            try:
                splits = fut.result()
                if bat_side in ("L", "S"):
                    results[pid] = splits.get("vs_lhb_hr9")
                else:
                    results[pid] = splits.get("vs_rhb_hr9")
            except Exception:
                results[pid] = None
    return results


def _fetch_head_to_head(batter_id: int, pitcher_id: int) -> dict:
    """
    Fetch career batter-vs-pitcher stats from the MLB Stats API.
    Returns AB, HR, AVG, OPS for this specific matchup.
    Only returns data if batter has faced pitcher at least 5 times.
    """
    if not batter_id or not pitcher_id:
        return {}
    try:
        resp = requests.get(
            f"{MLB_API_BASE}/people/{batter_id}/stats",
            params={"stats": "vsPlayer", "group": "hitting",
                    "opposingPlayerId": pitcher_id},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}

    for group in data.get("stats", []):
        for split in group.get("splits", []):
            stat = split.get("stat", {})
            ab = int(stat.get("atBats") or 0)
            if ab < 5:
                continue
            return {
                "ab":  ab,
                "hr":  int(stat.get("homeRuns") or 0),
                "avg": stat.get("avg", ".000"),
                "ops": stat.get("ops", ".000"),
                "k_pct": round(int(stat.get("strikeOuts") or 0) / ab * 100, 1),
            }
    return {}


def _fetch_home_away_splits_batch(player_ids: list) -> dict:
    """
    Fetch 2026 home/away split stats for a list of player IDs in one MLB API call.
    Returns {player_id: {"home": {...}, "away": {...}}}.
    """
    if not player_ids:
        return {}
    try:
        resp = requests.get(
            f"{MLB_API_BASE}/stats",
            params={
                "stats":     "statSplits",
                "group":     "hitting",
                "season":    date.today().year,
                "playerIds": ",".join(str(p) for p in player_ids[:60]),
                "sitCodes":  "h,a",
                "gameType":  "R",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}

    result: dict[int, dict] = {}
    for group in data.get("stats", []):
        for split in group.get("splits", []):
            player = split.get("player", {})
            pid    = player.get("id")
            code   = (split.get("split") or {}).get("code", "")
            stat   = split.get("stat", {})
            if not pid or code not in ("h", "a"):
                continue
            if pid not in result:
                result[pid] = {"home": {}, "away": {}}
            key = "home" if code == "h" else "away"
            result[pid][key] = {
                "hr":       int(stat.get("homeRuns") or 0),
                "pa":       int(stat.get("plateAppearances") or 0),
                "slugging": stat.get("slg", ".000"),
                "ops":      stat.get("ops", ".000"),
            }
    return result


def _fetch_career_park_hrs_batch(player_venue_pairs: list[tuple[int, int]]) -> dict[int, int]:
    """
    For each (mlbam_player_id, venue_id) pair, fetch the player's career HR count
    at that specific venue from Baseball Savant statcast_search.
    Returns {player_id: career_hr_count}. Skips on HTTP error or empty response.
    Max 5 concurrent requests to stay within Savant rate limits.
    """
    if not player_venue_pairs:
        return {}

    SAVANT_SEARCH = "https://baseballsavant.mlb.com/statcast_search/csv"

    def _fetch_one(pid: int, venue_id: int) -> tuple[int, int | None]:
        try:
            resp = requests.get(
                SAVANT_SEARCH,
                params={
                    "all":              "true",
                    "player_type":      "batter",
                    "batters_lookup[]": pid,
                    "hfAB":             "home_run|",
                    "hfGT":             "R|",
                    "stadium":          venue_id,
                    "type":             "details",
                    "min_pas":          "0",
                },
                timeout=20,
                headers={"User-Agent": "DingersHotline/1.0"},
            )
            resp.raise_for_status()
            text = resp.text.strip()
            if not text or text.startswith("Error"):
                return pid, 0
            lines = [l for l in text.splitlines() if l.strip()]
            hr_count = max(0, len(lines) - 1)  # subtract header row
            return pid, hr_count
        except Exception:
            return pid, None

    result: dict[int, int] = {}
    # Deduplicate — same player can appear multiple times (DH) at same venue
    unique_pairs = list({(pid, vid) for pid, vid in player_venue_pairs})

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_fetch_one, pid, vid): pid for pid, vid in unique_pairs}
        for future in futures:
            pid, count = future.result()
            if count is not None:
                result[pid] = count

    return result


def _fetch_batter_pitch_splits(player_ids: list[int]) -> dict[int, dict]:
    """
    Fetch batter xSLG vs fastball/breaking_ball/offspeed from Baseball Savant leaderboard.
    Returns {player_id: {"xslg_fastball": float, "xslg_breaking": float, "xslg_offspeed": float}}.
    Early-season (before ~June): Savant pitch-type columns may be empty — returns {} per player.
    """
    if not player_ids:
        return {}
    year = date.today().year
    try:
        resp = requests.get(
            "https://baseballsavant.mlb.com/leaderboard/custom",
            params={
                "year":       year,
                "type":       "batter",
                "filter":     "",
                "selections": "xwoba,xslg,xwoba_fastball,xwoba_breaking_ball,xwoba_offspeed_pitch,"
                              "xslg_fastball,xslg_breaking_ball,xslg_offspeed_pitch",
                "min_results": 0,
                "min_pa":     0,
            },
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        rows = resp.json()
    except Exception:
        return {}

    if not isinstance(rows, list):
        return {}

    result: dict[int, dict] = {}
    player_id_set = {int(p) for p in player_ids}
    for row in rows:
        pid_raw = row.get("player_id") or row.get("batter")
        if pid_raw is None:
            continue
        try:
            pid = int(pid_raw)
        except (ValueError, TypeError):
            continue
        if pid not in player_id_set:
            continue

        def _sf(val) -> float | None:
            if val in (None, "", "null"):
                return None
            try:
                return float(val)
            except (ValueError, TypeError):
                return None

        splits = {
            "xslg_fastball":  _sf(row.get("xslg_fastball")),
            "xslg_breaking":  _sf(row.get("xslg_breaking_ball")),
            "xslg_offspeed":  _sf(row.get("xslg_offspeed_pitch")),
        }
        if any(v is not None for v in splits.values()):
            result[pid] = splits
    return result


class Homer:
    """
    Gather-then-analyze predictor.

    Python fetches all data, builds per-game player cards, then ranks picks
    using a deterministic Python scoring function. No LLM ranking — avoids
    llama3.1:8b hallucinating wrong teams, wrong players, and made-up stats
    when the context exceeds its reliable processing window.

    _gather_data() is cached on the instance so run() + get_picks_json()
    called back-to-back only fetch data once.
    """

    _fetch_career_park_hrs_batch = staticmethod(_fetch_career_park_hrs_batch)
    _fetch_pitcher_career_splits_batch = staticmethod(_fetch_pitcher_career_splits_batch)
    _fetch_batter_pitch_splits = staticmethod(_fetch_batter_pitch_splits)

    @staticmethod
    def _compute_elite_boost_thresholds(batter_stats: dict) -> dict:
        """
        Given batter_stats = {player_key: {"home_run": N, "xslg": "0.xxx"}},
        return {"hr_threshold": int|None, "xslg_threshold": float|None} where
        each threshold is the 10th-highest value (top-10 players qualify).
        When fewer than 10 players, the minimum value qualifies everyone.
        """
        hr_values: list[int] = []
        xslg_values: list[float] = []
        for stats in batter_stats.values():
            hr = stats.get("home_run")
            if hr is not None:
                try:
                    hr_values.append(int(hr))
                except (ValueError, TypeError):
                    pass
            xslg = stats.get("xslg")
            if xslg is not None:
                try:
                    xslg_values.append(float(xslg))
                except (ValueError, TypeError):
                    pass

        hr_threshold = None
        xslg_threshold = None

        if hr_values:
            hr_values.sort(reverse=True)
            hr_threshold = hr_values[9] if len(hr_values) >= 10 else min(hr_values)

        if xslg_values:
            xslg_values.sort(reverse=True)
            xslg_threshold = xslg_values[9] if len(xslg_values) >= 10 else min(xslg_values)

        return {"hr_threshold": hr_threshold, "xslg_threshold": xslg_threshold}

    def __init__(self):
        self._context: dict | None = None   # cache so data is only fetched once

    def _fetch_bat_tracking(self) -> dict:
        """Fetch Baseball Savant bat-tracking leaderboard and return name→blast_rate dict.
        blast_per_swing: fraction of swings that qualify as a Blast
        ((percent_squared_up × 100) + bat_speed ≥ 164). ~7% is league average.
        Caches to cache/bat_tracking_YYYY-MM-DD.csv.
        """
        from pathlib import Path as _Path
        season     = date.today().year
        today_str  = date.today().isoformat()
        cache_path = _Path(__file__).parent.parent / "cache" / f"bat_tracking_{today_str}.csv"

        if cache_path.exists():
            text = cache_path.read_text(encoding="utf-8")
        else:
            url = (
                f"{SAVANT_BASE}/leaderboard/bat-tracking"
                f"?attackZone=&batSide=&contactType=&count=&"
                f"dateStart={season}-01-01&dateEnd={season}-12-31&"
                f"gameType=R&isHardHit=&minSwings=&minGroupSwings=1&"
                f"pitchType=&seasonYear={season}&team=&type=batter&csv=true"
            )
            try:
                resp = requests.get(url, headers=_HEADERS, timeout=20)
                text = resp.text.lstrip('\ufeff')
                cache_path.parent.mkdir(exist_ok=True)
                cache_path.write_text(text, encoding="utf-8")
            except Exception:
                return {}

        try:
            reader = csv.DictReader(io.StringIO(text))
            result = {}
            for row in reader:
                raw = (row.get("name") or "").strip().strip('"')
                if not raw:
                    continue
                # CSV name is "Last, First" — convert to "first last" for matching
                if "," in raw:
                    last, first = raw.split(",", 1)
                    key = f"{first.strip()} {last.strip()}".lower()
                else:
                    key = raw.lower()
                blast = row.get("blast_per_swing")
                if blast:
                    result[key] = float(blast)
            return result
        except Exception:
            return {}

    def _fetch_full_statcast(self, player_type: str, selections: str) -> dict:
        """Fetch the full Statcast leaderboard CSV and return a name→stats dict.
        Caches raw CSV to cache/statcast_{type}_YYYY-MM-DD.csv — re-runs that day skip the fetch.
        """
        from pathlib import Path as _Path
        season     = date.today().year
        today_str  = date.today().isoformat()
        cache_path = _Path(__file__).parent.parent / "cache" / f"statcast_{player_type}_{today_str}.csv"

        if cache_path.exists():
            text = cache_path.read_text(encoding="utf-8")
        else:
            url = (
                f"{SAVANT_BASE}/leaderboard/custom"
                f"?year={season}&type={player_type}&filter=&sort=4&sortDir=desc&min=5"
                f"&selections={selections}"
                f"&chart=false&r=no&exactNameSearch=false&csv=true"
            )
            try:
                resp = requests.get(url, headers=_HEADERS, timeout=20)
                text = resp.text.lstrip('\ufeff')
                cache_path.parent.mkdir(exist_ok=True)
                cache_path.write_text(text, encoding="utf-8")
            except Exception:
                return {}

        try:
            reader = csv.DictReader(io.StringIO(text))
            result = {}
            for row in reader:
                name = (row.get("last_name, first_name") or row.get("player_name") or "").lower()
                pid_raw = row.get("player_id") or row.get("batter") or row.get("pitcher") or ""
                try:
                    result[int(pid_raw)] = row        # primary: player_id (unambiguous)
                except (ValueError, TypeError):
                    pass
                if name:
                    result.setdefault(name, row)      # secondary: name (no overwrite on collision)
            return result
        except Exception:
            return {}

    def _build_game_cards(self, lineups_json: str, batter_stats: dict,
                          pitcher_stats: dict, our_history: list,
                          recent_form: list, pitcher_form: dict,
                          home_away: dict,
                          pitcher_career_splits: dict | None = None) -> tuple:
        """
        Build a compact per-game card for every batter (confirmed and roster).
        Includes: Statcast, pitcher vulnerability, platoon edge, pitcher recent form,
        head-to-head career stats (top 4 batters), home/away splits, our record.

        Returns (cards_text, player_signals) where player_signals is a dict:
          {player_name: {platoon, barrel_rate, hard_hit_pct, hr_fb_ratio,
                         recent_form_14d, pitcher_hr_per_9, h2h_hr, h2h_ab,
                         is_home, venue_slugging, status}}
        """
        try:
            lineups = json.loads(lineups_json)
        except Exception:
            return "Could not parse lineup data.", {}

        if lineups.get("status") != "success":
            return lineups.get("message", "No lineup data."), {}

        lines          = []
        player_signals = {}   # "player_name||game_pk" → signals dict

        # ── Build per-team G1/G2 doubleheader index ───────────────────────────
        from collections import defaultdict as _defaultdict
        _team_game_order: dict[str, list] = _defaultdict(list)
        for _g in lineups.get("games", []):
            _gtime = _g.get("game_time", "")
            _gpk   = str(_g.get("game_pk", ""))
            for _side in ("away", "home"):
                _team = _g.get(_side, {}).get("team", "")
                if _team:
                    _team_game_order[_team].append((_gtime, _gpk))
        _team_game_label: dict[tuple, str | None] = {}
        for _team, _entries in _team_game_order.items():
            _entries.sort()
            _is_dh = len(_entries) > 1
            for _i, (_, _gpk) in enumerate(_entries):
                _team_game_label[(_team, _gpk)] = f"G{_i+1}" if _is_dh else None
        # ─────────────────────────────────────────────────────────────────────

        for game in lineups.get("games", []):
            away_side = game.get("away", {})
            home_side = game.get("home", {})
            venue     = game.get("venue", "")
            time_raw  = game.get("game_time") or ""
            time_     = time_raw[:16]   # display only (no tz — used in section headers)

            game_pk_str = str(game.get("game_pk") or "")

            for side, opp, is_home in [(away_side, home_side, False),
                                       (home_side, away_side, True)]:
                sp           = opp.get("starting_pitcher") or "TBD"
                sp_id        = opp.get("pitcher_id")
                sp_throws    = opp.get("pitcher_throws", "?")
                team         = side.get("team", "")
                game_label   = _team_game_label.get((team, game_pk_str))
                batters_full = side.get("batters") or []  # Now includes all roster players

                if not batters_full:
                    continue  # Skip if no players at all

                # Count confirmed vs waiting players
                confirmed_count = sum(1 for b in batters_full if b.get("status") == "confirmed")
                waiting_count = sum(1 for b in batters_full if b.get("status") == "waiting")

                # Statcast pitcher season stats
                sp_key  = sp.lower()
                sp_data = _find_best_name_match(sp, pitcher_stats)
                p_hr_fb     = sp_data.get("hr_flyball_rate") or "—"
                p_fb_pct    = sp_data.get("fb_percent") or "—"
                p_xfip      = sp_data.get("xfip") or "—"
                sp_barrel_allowed = _safe_float(sp_data.get("barrel_batted_rate"))

                # Pitch-type mix buckets
                _pct = lambda f: (_safe_float(sp_data.get(f)) or 0.0)
                sp_fb_pct       = round(_pct("n_ff_formatted") + _pct("n_si_formatted") + _pct("n_fc_formatted"), 1) or None
                sp_breaking_pct = round(_pct("n_sl_formatted") + _pct("n_cu_formatted") + _pct("n_sw_formatted"), 1) or None
                sp_offspeed_pct = round(_pct("n_ch_formatted") + _pct("n_fs_formatted"), 1) or None

                # Pitcher recent form (last 3 starts)
                pf       = pitcher_form.get(sp_id) or {}
                pcs      = (pitcher_career_splits or {}).get(sp_id) or {}
                pf_str   = (f"L3-starts: {pf['hr_per_9']:.1f}HR/9 over {pf['total_hr']}HR/{pf['starts_sampled']}GS"
                            if pf else "recent form: n/a")

                # Explicit matchup string so model never needs to infer opponent
                opp_team    = opp.get("team", "")
                matchup_str = f"{opp_team} @ {team}" if is_home else f"{team} @ {opp_team}"

                lines.append(f"\n=== {matchup_str} | {venue} | {time_} ===")
                lines.append(f"  {team} batters face SP: {sp} ({sp_throws})")
                lines.append(f"  Confirmed: {confirmed_count}, Waiting: {waiting_count}")
                lines.append(f"  Pitcher season: xFIP={p_xfip}  HR/FB={p_hr_fb}%  FB%={p_fb_pct}%")
                lines.append(f"  Pitcher recent: {pf_str}")

                for pos, batter_info in enumerate(batters_full[:12]):  # Show top 12 players per team
                    if isinstance(batter_info, dict):
                        batter_name = batter_info.get("name", "")
                        batter_id   = batter_info.get("id")
                        bat_side    = batter_info.get("bat_side", "?")
                        status      = batter_info.get("status", "unknown")
                    else:
                        batter_name = batter_info
                        batter_id   = None
                        bat_side    = "?"
                        status      = "unknown"

                    # Fall back to persistent DB if API didn't return handedness
                    if bat_side == "?" and batter_id:
                        bat_side = get_bat_side(batter_id)
                    if bat_side == "?":
                        bat_side = get_bat_side_by_name(batter_name)

                    b_key  = batter_name.lower()
                    b_data = (batter_stats.get(batter_id)
                              if batter_id and batter_id in batter_stats
                              else _find_best_name_match(batter_name, batter_stats))

                    barrel = b_data.get("barrel_batted_rate") or "—"
                    hh     = b_data.get("hard_hit_percent") or "—"
                    hr_fb  = b_data.get("hr_flyballs_rate_batter") or "—"
                    pull   = b_data.get("pull_percent") or "—"
                    ev_    = b_data.get("exit_velocity_avg") or "—"

                    # Recent form — match by player_id first to avoid name collisions
                    # (e.g. two active players named "Max Muncy" with different MLB IDs)
                    if batter_id:
                        form_hrs = next((str(p.get("hr_last_14d", ""))
                                         for p in recent_form
                                         if p.get("player_id") == batter_id), "—")
                    else:
                        form_hrs = next((str(p.get("hr_last_14d", ""))
                                         for p in recent_form
                                         if any(part in p.get("player","").lower()
                                                for part in b_key.split() if len(part) > 3)), "—")

                    # Platoon advantage (only for confirmed players with known bat_side)
                    platoon = ""
                    if status == "confirmed" and bat_side != "?":
                        platoon = _platoon_edge(bat_side, sp_throws)

                    # Home/away splits — use actual game context (only for confirmed players)
                    ha = home_away.get(batter_id) or {}
                    ha_key  = "home" if is_home else "away"
                    ha_data = ha.get(ha_key, {})
                    ha_str  = (f"{'home' if is_home else 'away'}: "
                               f"{ha_data.get('hr','—')}HR/{ha_data.get('pa','—')}PA "
                               f"SLG={ha_data.get('slg','—')}"
                               if ha_data else "splits: n/a")

                    # Head-to-head (only top 4 confirmed batters per side to limit API calls)
                    h2h     = {}
                    h2h_str = ""
                    if status == "confirmed" and pos < 4 and batter_id and sp_id:
                        h2h = _fetch_head_to_head(batter_id, sp_id)
                        if h2h:
                            h2h_str = (f"  h2h: {h2h['hr']}HR/{h2h['ab']}AB "
                                       f"avg={h2h['avg']} ops={h2h['ops']}")

                    status_marker = f"[{status.upper()}]"
                    lines.append(
                        f"  #{pos+1} {batter_name:<24} {status_marker:<12} {platoon:<10} "
                        f"barrel={barrel}%  hh={hh}%  HR/FB={hr_fb}%  "
                        f"pull={pull}%  EV={ev_}  "
                        f"form(14d)={form_hrs}HR  {ha_str}"
                        + (f"  {h2h_str.strip()}" if h2h_str.strip() else "")
                    )

                    # ── Store structured signals for performance tracking ──────
                    if batter_name:
                        _ck = f"{batter_name}||{game_pk_str}" if game_pk_str else batter_name
                        player_signals[_ck] = {
                            "player_name":      batter_name,
                            "team":             team,
                            "game_pk":          game_pk_str or None,
                            "game_label":       game_label,
                            "status":           status,  # "confirmed", "waiting", or "unknown"
                            "platoon":          platoon,
                            "matchup":          matchup_str,
                            "game_time":        time_raw, # full UTC ISO string e.g. "2026-04-23T17:05:00Z"
                            "venue":            venue,   # stadium name for park-factor lookup
                            "bat_side":         bat_side,           # L / R / S
                            "pitcher_name":     sp,                 # starting pitcher name
                            "pitcher_throws":   sp_throws,          # R / L
                            "batting_order":    pos + 1,            # 1–9 lineup position
                            "pa":               _safe_int(b_data.get("pa")),
                            "season_hr":        _safe_int(b_data.get("home_run")),
                            "barrel_rate":      _safe_float(barrel),
                            "hard_hit_pct":     _safe_float(hh),
                            "hr_fb_ratio":      _safe_float(hr_fb),
                            "xiso":             _safe_float(b_data.get("xiso")),
                            "xslg":             _safe_float(b_data.get("xslg")),
                            "xhr_rate":         _safe_float(b_data.get("xhrs")),  # populated mid-season
                            "fb_pct":           _safe_float(b_data.get("flyballs_percent")),
                            "launch_angle":     _safe_float(b_data.get("launch_angle_avg")),
                            "ev_avg":           _safe_float(b_data.get("exit_velocity_avg")),
                            "ev_max":           _safe_float(b_data.get("max_hit_speed")),
                            "sweet_spot_pct":   _safe_float(b_data.get("sweet_spot_percent")),
                            "pull_pct":         _safe_float(b_data.get("pull_percent")),
                            "recent_form_14d":  _safe_int(form_hrs),
                            "pitcher_hr_per_9":    round(pf["hr_per_9"], 2) if pf else None,
                            "pitcher_hr_vs_hand":  (
                                pcs.get("vs_lhb_hr9") if (
                                    bat_side == "L" or (bat_side == "S" and sp_throws == "R")
                                ) else pcs.get("vs_rhb_hr9") if (
                                    bat_side == "R" or (bat_side == "S" and sp_throws == "L")
                                ) else None
                            ),
                            "pitcher_career_hr_vs_hand": (
                                pcs.get("vs_lhb_hr9") if (
                                    bat_side == "L" or (bat_side == "S" and sp_throws == "R")
                                ) else pcs.get("vs_rhb_hr9") if (
                                    bat_side == "R" or (bat_side == "S" and sp_throws == "L")
                                ) else None
                            ),
                            "pitcher_fb_pct":        sp_fb_pct,
                            "pitcher_breaking_pct":  sp_breaking_pct,
                            "pitcher_offspeed_pct":  sp_offspeed_pct,
                            "pitcher_barrel_pct":    sp_barrel_allowed,
                            "h2h_hr":           h2h.get("hr") if h2h else None,
                            "h2h_ab":           h2h.get("ab") if h2h else None,
                            "is_home":          is_home,
                            "player_id":        batter_id,
                            "venue_id":         game.get("venue_id"),
                            "venue_slugging":   ha_data.get("slg") if ha_data else None,
                            "bpp_vs_grade":     None,  # BallparkPal matchup grade (0-10)
                            "bpp_proj_rank":    None,  # BallparkPal matchup table rank
                            "bpp_hr_pct":       None,  # BallparkPal game HR prob % (future: separate endpoint)
                            "park_hr_factor":   None,  # Stadium HR factor (%)
                            "temp_f":           None,  # Temperature in Fahrenheit
                            "wind_mph":         None,  # Wind speed in MPH
                            "batter_xslg_vs_fastball": None,  # populated in _gather_data()
                            "batter_xslg_vs_breaking": None,
                            "batter_xslg_vs_offspeed": None,
                        }

        text = "\n".join(lines) if lines else "No player data available."
        return text, player_signals

    def _gather_data(self) -> dict:
        """
        Fetch all data sources and build per-game player cards covering
        every hitter in today's confirmed lineups.
        Result is cached on the instance — safe to call twice in one session.
        """
        if self._context:
            return self._context

        today = date.today().isoformat()
        data  = {"date": today}

        print("  [1/9] Fetching confirmed lineups (with player IDs + handedness)...")
        lineups_json        = fetch_confirmed_lineups(today)
        data["lineups_raw"] = lineups_json

        # Diagnostic: show how many games have confirmed batting orders
        try:
            _lu = json.loads(lineups_json)
            _games = _lu.get("games", [])
            _confirmed = sum(
                1 for g in _games
                for side in ("away", "home")
                if g.get(side, {}).get("lineup_confirmed")
            )
            print(f"       {len(_games)} games found, {_confirmed} sides with confirmed lineups")
            if _confirmed == 0:
                print("       ** No batting orders posted yet — picks require confirmed lineups")
                print("       ** MLB posts lineups 2–4 hours before first pitch (typically 11am–noon ET)")
        except Exception:
            pass

        print("  [2/9] Checking lineup availability for pending bets...")
        data["availability"] = check_lineup_availability(today)

        print("  [3-6] Fetching Statcast, recent form, BPP, and odds in parallel...")
        batter_stats   = {}
        pitcher_stats  = {}
        recent_form    = []
        _bpp_matchups  = ""
        _bpp_parks     = ""
        _odds          = ""

        def _fetch_batters():
            return self._fetch_full_statcast(
                "batter",
                "pa,barrel_batted_rate,hard_hit_percent,hr_flyballs_rate_batter,"
                "pull_percent,exit_velocity_avg,max_hit_speed,sweet_spot_percent,"
                "xiso,xslg,xhrs,flyballs_percent,launch_angle_avg,home_run"
            )

        def _fetch_pitchers():
            return self._fetch_full_statcast(
                "pitcher",
                "hr_flyball_rate,fb_percent,xfip,hard_hit_percent,barrel_batted_rate,"
                "n_ff_formatted,n_si_formatted,n_fc_formatted,"
                "n_sl_formatted,n_cu_formatted,n_sw_formatted,"
                "n_ch_formatted,n_fs_formatted"
            )

        def _fetch_recent():
            raw = fetch_recent_hr_form(days=14)
            try:
                leaders = json.loads(raw).get("hr_leaders", [])
            except Exception:
                leaders = []
            return raw, leaders

        def _fetch_bpp():
            return fetch_pitcher_matchups(), fetch_park_factors()

        # Build confirmed team names for odds quota filter (full names match Odds API event format)
        confirmed_teams: set[str] = set()
        try:
            _lu = json.loads(lineups_json)
            for g in _lu.get("games", []):
                for side_key in ("away", "home"):
                    side = g.get(side_key, {})
                    if side.get("lineup_confirmed"):
                        team_name = side.get("team")
                        if team_name:
                            confirmed_teams.add(team_name)
        except Exception:
            pass

        def _fetch_odds_filtered():
            return fetch_odds_comparison(confirmed_teams=confirmed_teams or None)

        def _fetch_blast():
            return self._fetch_bat_tracking()

        with ThreadPoolExecutor(max_workers=7) as executor:
            fut_batters  = executor.submit(_fetch_batters)
            fut_pitchers = executor.submit(_fetch_pitchers)
            fut_recent   = executor.submit(_fetch_recent)
            fut_bpp      = executor.submit(_fetch_bpp)
            fut_odds     = executor.submit(_fetch_odds_filtered)
            fut_blast    = executor.submit(_fetch_blast)

            for fut in as_completed([fut_batters, fut_pitchers, fut_recent, fut_bpp, fut_odds, fut_blast]):
                if fut is fut_batters:
                    batter_stats = fut.result()
                elif fut is fut_pitchers:
                    pitcher_stats = fut.result()
                elif fut is fut_recent:
                    _recent_raw, recent_form = fut.result()
                    data["recent_form_raw"] = _recent_raw
                elif fut is fut_bpp:
                    _bpp_matchups, _bpp_parks = fut.result()
                    data["matchups"]     = _bpp_matchups
                    data["park_factors"] = _bpp_parks
                elif fut is fut_odds:
                    _odds = fut.result()
                    data["odds"] = _odds
                elif fut is fut_blast:
                    data["blast_tracking"] = fut.result()

        print("  [7] Fetching pitcher recent form (parallel)...")
        pitcher_ids: list[int] = []
        batter_ids:  list[int] = []
        try:
            lu = json.loads(lineups_json)
            for game in lu.get("games", []):
                for side_key in ("away", "home"):
                    side    = game.get(side_key, {})
                    opp_key = "home" if side_key == "away" else "away"
                    opp     = game.get(opp_key, {})
                    pid     = opp.get("pitcher_id")
                    if pid:
                        pitcher_ids.append(pid)
                    for b in (side.get("batters") or []):
                        bid = b.get("id") if isinstance(b, dict) else None
                        if bid:
                            batter_ids.append(bid)
        except Exception:
            pass

        pitcher_form: dict[int, dict] = {}
        pitcher_career_splits: dict[int, dict] = {}
        with ThreadPoolExecutor(max_workers=12) as executor:
            form_futs   = {executor.submit(_fetch_pitcher_recent_form,   pid): pid for pid in pitcher_ids}
            splits_futs = {executor.submit(_fetch_pitcher_career_splits, pid): pid for pid in pitcher_ids}
            for fut in as_completed(form_futs):
                pid  = form_futs[fut]
                form = fut.result()
                if form:
                    pitcher_form[pid] = form
            for fut in as_completed(splits_futs):
                pid    = splits_futs[fut]
                splits = fut.result()
                if splits:
                    pitcher_career_splits[pid] = splits

        print("  [8/9] Fetching home/away splits for confirmed batters...")
        home_away = _fetch_home_away_splits_batch(list(set(batter_ids)))

        print("  [9/9] Building per-game player cards...")
        cards_text, player_signals = self._build_game_cards(
            lineups_json, batter_stats, pitcher_stats,
            [], recent_form, pitcher_form, home_away,
            pitcher_career_splits
        )
        data["game_cards"]     = cards_text
        data["player_signals"] = player_signals

        # ── Backfill batting order for waiting players in partial lineups ─────
        player_signals = self._add_roster_fallback(lineups_json, player_signals, batter_stats,
                                                     pitcher_form=pitcher_form,
                                                     recent_form=recent_form)
        data["player_signals"] = player_signals

        # ── Career park HR batch fetch ────────────────────────────────────────
        try:
            pvp = [
                (int(sig["player_id"]), int(sig["venue_id"]))
                for sig in player_signals.values()
                if sig.get("player_id") and sig.get("venue_id")
            ]
            if pvp:
                print(f"  [CareerPark] Fetching career park HR for {len(pvp)} players…")
                career_park = _fetch_career_park_hrs_batch(pvp)
                for sig in player_signals.values():
                    pid = sig.get("player_id")
                    if pid and int(pid) in career_park:
                        sig["career_park_hr"] = career_park[int(pid)]
                hits = sum(1 for v in career_park.values() if v and v > 0)
                print(f"  [CareerPark] Done — {hits}/{len(career_park)} players have ≥1 career HR at today's venues")
        except Exception as e:
            print(f"  [CareerPark] Warning: fetch failed ({e}) — signal skipped")

        # ── Batter pitch-type splits (xSLG vs fastball/breaking/offspeed) ──────
        try:
            batter_ids = [
                int(sig["player_id"])
                for sig in player_signals.values()
                if sig.get("player_id") and sig.get("lineup_confirmed")
            ]
            if batter_ids:
                print(f"  [PitchSplits] Fetching batter pitch-type splits for {len(batter_ids)} players…")
                pitch_splits = _fetch_batter_pitch_splits(batter_ids)
                for sig in player_signals.values():
                    pid = sig.get("player_id")
                    if pid and int(pid) in pitch_splits:
                        ps = pitch_splits[int(pid)]
                        sig["batter_xslg_vs_fastball"] = ps.get("xslg_fastball")
                        sig["batter_xslg_vs_breaking"] = ps.get("xslg_breaking")
                        sig["batter_xslg_vs_offspeed"] = ps.get("xslg_offspeed")
                hits = len(pitch_splits)
                print(f"  [PitchSplits] Done — {hits}/{len(batter_ids)} players have pitch-type split data")
        except Exception as e:
            print(f"  [PitchSplits] Warning: fetch failed ({e}) — signal skipped")

        # ── Merge odds signals (EV, Kelly, value_edge, Pinnacle) ─────────────
        try:
            odds_data = json.loads(data["odds"])

            if odds_data.get("status") != "success":
                print(f"[ODDS] Warning: odds fetch returned status={odds_data.get('status')!r} — {odds_data.get('message', '')}")

            def _norm(s: str) -> str:
                return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().strip()

            def _name_part(sig_key: str) -> str:
                """Strip ||game_pk suffix — first segment before any pipe."""
                return sig_key.split("|")[0]

            def _team_in_matchup(team_abbrev: str, matchup: str) -> bool:
                """True if the team's full name fragment appears in the odds API matchup string."""
                venue = _TEAM_VENUE.get(team_abbrev.lower())
                if not venue:
                    return False
                matchup_lower = matchup.lower()
                return any(v == venue and frag in matchup_lower
                           for frag, v in _TEAM_VENUE.items())

            # Name-only index: strips ||game_pk suffix so composite keys work correctly.
            # DH players (same name, two game_pks) appear in the same list → odds applied to both.
            normed_signals: dict[str, list[str]] = {}
            for k in player_signals:
                normed_signals.setdefault(_norm(_name_part(k)), []).append(k)
            matched_count = 0

            for comp in odds_data.get("comparisons", []):
                pname        = comp.get("player", "")
                pname_norm   = _norm(pname)
                comp_matchup = comp.get("matchup", "")

                if pname_norm in normed_signals:
                    candidates = normed_signals[pname_norm]
                    if len(candidates) == 1:
                        matched_keys = candidates
                    else:
                        # Multiple candidates — try team-based disambiguation via signal dict.
                        # For DH (same player, same team, 2 game_pks): team_matched will have
                        # both or neither — apply odds to all. For same-name different teams:
                        # pick the team that matches the odds matchup string.
                        team_matched = [k for k in candidates
                                        if _team_in_matchup(player_signals[k].get("team", ""), comp_matchup)]
                        matched_keys = team_matched if team_matched else candidates
                else:
                    best_ratio, best_keys = 0.0, []
                    for nk, keys in normed_signals.items():
                        r = SequenceMatcher(None, pname_norm, nk).ratio()
                        if r > best_ratio:
                            best_ratio, best_keys = r, keys
                    matched_keys = best_keys if best_ratio >= 0.85 else []

                if matched_keys:
                    matched_count += 1
                    ev   = comp.get("ev_10")
                    pin  = comp.get("pinnacle")
                    best = comp.get("best_odds")
                    if ev is None:
                        print(f"[ODDS] {pname}: ev_10 is None (pinnacle={pin!r}, best_odds={best!r})")
                    if pin is None:
                        print(f"[ODDS] {pname}: pinnacle_odds missing — EV/Kelly unreliable")
                    for _mk in matched_keys:
                        player_signals[_mk]["ev_10"]         = ev
                        player_signals[_mk]["kelly_size"]    = comp.get("kelly_size")
                        player_signals[_mk]["value_edge"]    = comp.get("value_edge")
                        player_signals[_mk]["pinnacle_odds"] = pin
                        player_signals[_mk]["best_odds"]     = best
                else:
                    print(f"[ODDS] No signal match for odds player: {pname!r}")

            if matched_count == 0 and odds_data.get("comparisons"):
                print(f"[ODDS] Warning: {len(odds_data['comparisons'])} odds entries found but 0 matched to player signals")
            else:
                print(f"[ODDS] Merged odds signals for {matched_count} players")
        except Exception as e:
            print(f"[ODDS] Exception merging odds signals: {e}")

        # ── Upgrade waiting→confirmed when odds exist (books don't post props for scratched players) ──
        upgraded = []
        for sig_key, sig in player_signals.items():
            if sig.get("status") == "waiting" and sig.get("ev_10") is not None:
                sig["status"] = "confirmed"
                sig["lineup_confirmed"] = True
                upgraded.append(sig.get("player_name", sig_key.split("|")[0]))
        if upgraded:
            print(f"[ODDS] Confirmed via odds presence: {', '.join(upgraded)}")

        # ── Merge blast rate (bat-tracking leaderboard) ───────────────────────
        try:
            blast_tracking = data.get("blast_tracking", {})
            if blast_tracking:
                bt_normed: dict[str, list[str]] = {}
                for k in player_signals:
                    bt_normed.setdefault(_norm(_name_part(k)), []).append(k)
                for bt_key, blast_val in blast_tracking.items():
                    if bt_key in bt_normed:
                        bt_keys = bt_normed[bt_key]
                    else:
                        best_ratio, best_keys = 0.0, []
                        for nk, keys in bt_normed.items():
                            r = SequenceMatcher(None, bt_key, nk).ratio()
                            if r > best_ratio:
                                best_ratio, best_keys = r, keys
                        bt_keys = best_keys if best_ratio >= 0.85 else []
                    for _bk in bt_keys:
                        player_signals[_bk]["blast_rate"] = round(blast_val * 100, 2)
        except Exception:
            pass

        # ── Elite leaderboard boost — top 10 in HR or xSLG league-wide ─────────
        try:
            hr_vals   = sorted([float(v["home_run"]) for v in batter_stats.values()
                                 if v.get("home_run") not in (None, "", "null")], reverse=True)
            xslg_vals = sorted([float(v["xslg"])     for v in batter_stats.values()
                                 if v.get("xslg") not in (None, "", "null")],     reverse=True)
            hr_thresh   = hr_vals[9]   if len(hr_vals)   >= 10 else None
            xslg_thresh = xslg_vals[9] if len(xslg_vals) >= 10 else None
            for ps in player_signals.values():
                ps["is_top10_leaderboard"] = bool(
                    (hr_thresh   is not None and (ps.get("season_hr") or 0) >= hr_thresh) or
                    (xslg_thresh is not None and (ps.get("xslg")      or 0) >= xslg_thresh)
                )
        except Exception:
            pass

        # ── Merge BallparkPal matchup grades, projections rank, and park factors ──
        try:
            matchups_data = json.loads(data["matchups"])
            park_data     = json.loads(data["park_factors"])

            # BallparkPal matchup table columns (actual keys from scraped data):
            #   "batter" → player name
            #   "vs"     → batter-vs-pitcher matchup grade (0–10, higher = better for batter)
            #   "hr"     → career HRs vs this specific pitcher (H2H, not game probability)
            #   "b"      → batter handedness, "p" → pitcher handedness
            # NOTE: The "Prob %" column (18–26% game HR probability) shown on BallparkPal's
            #       UI is NOT present in the Matchups.php table data. It likely requires a
            #       separate endpoint. Do NOT use "hr" as a probability — it is career H2H HRs.
            matchup_lookup: dict[str, dict] = {}
            if matchups_data.get("status") == "success":
                for rank, m in enumerate(matchups_data.get("matchups", []), start=1):
                    batter = (m.get("batter") or m.get("player") or
                               m.get("name") or m.get("hitter") or "").strip()
                    if not batter:
                        continue
                    matchup_lookup[batter.lower()] = {
                        "bpp_vs_grade": _safe_float(m.get("vs")),   # 0-10 matchup grade
                        "bpp_h2h_hr":   _safe_float(m.get("hr")),   # career HR vs this pitcher
                        "bpp_rank":     rank,
                    }

            proj_rank_lookup: dict[str, int] = {
                k: v["bpp_rank"] for k, v in matchup_lookup.items()
            }

            # Park factor lookup: venue_lower → {park_hr_factor, temp_f, wind_mph}
            park_lookup: dict[str, dict] = {}
            if park_data.get("status") == "success":
                import re
                for g in park_data.get("games", []):
                    game_str = g.get("game", "")

                    hr_factor_str = g.get("homeruns", "")
                    hr_factor: float | None = None
                    if hr_factor_str:
                        try:
                            if hr_factor_str.startswith("+"):
                                hr_factor = 100 + float(hr_factor_str[1:-1])
                            elif hr_factor_str.startswith("-"):
                                hr_factor = 100 - float(hr_factor_str[1:-1])
                            else:
                                hr_factor = float(hr_factor_str)
                        except (ValueError, TypeError):
                            pass

                    temp_str = g.get("temperatureforecast1", "").replace("°", "")
                    wind_str = g.get("windforecast1", "")
                    wind_receptiveness = g.get("windreceptiveness", "")
                    homeruns_weather_pct = g.get("homerunsweather", "")
                    outfield_size = g.get("outfieldsize", "")
                    short_description = g.get("shortdescription", "")

                    # Air density signals
                    def _parse_num(s):
                        try: return float(str(s).replace(",", "").replace("%", "").replace("+", "").strip())
                        except (ValueError, TypeError): return None

                    altitude_ft  = _parse_num(g.get("altitude", ""))
                    humidity_pct = _parse_num(g.get("humidity", ""))
                    pressure_mb  = _parse_num(g.get("pressure", ""))
                    carry_ft     = _parse_num(g.get("carry", ""))

                    # BPP signals a closed retractable roof via windreceptiveness="Roof Closed"
                    # When closed, temp=0 and wind="Variable" — treat as indoor, discard weather.
                    roof_closed = "roof closed" in wind_receptiveness.lower()

                    # Parse weather HR factor
                    weather_hr_factor = None
                    if homeruns_weather_pct and not roof_closed:
                        try:
                            if homeruns_weather_pct.startswith("+"):
                                weather_hr_factor = 100 + float(homeruns_weather_pct[1:-1])
                            elif homeruns_weather_pct.startswith("-"):
                                weather_hr_factor = 100 - float(homeruns_weather_pct[1:-1])
                            else:
                                weather_hr_factor = float(homeruns_weather_pct)
                        except (ValueError, TypeError):
                            pass

                    park_entry = {
                        "park_hr_factor":     hr_factor,
                        "temp_f":             None if roof_closed else _safe_float(temp_str),
                        "wind_mph":           None if roof_closed else _safe_float(wind_str),
                        "wind_deg":           None,
                        "wind_direction_bpp": None if roof_closed else g.get("winddirection1"),
                        "wind_receptiveness": wind_receptiveness,
                        "weather_hr_factor":  weather_hr_factor,
                        "homerunsnumber":     None if roof_closed else _safe_float(g.get("homerunsnumber", "")),
                        "outfield_size":      outfield_size,
                        "stadium_description": short_description,
                        "roof_closed":        roof_closed,
                        "altitude_ft":        altitude_ft,
                        "humidity_pct":       None if roof_closed else humidity_pct,
                        "pressure_mb":        pressure_mb,
                        "carry_ft":           None if roof_closed else carry_ft,
                    }

                    # BPP game format: "Away @ Home HH:MM" — home team is the venue.
                    # Store under: the raw game string, both team parts, AND the
                    # mapped stadium name so the player venue lookup has the best chance.
                    keys_to_store = set()
                    if "@" in game_str:
                        away_raw = re.sub(r'\d+:\d+', '', game_str.split("@")[0]).strip()
                        home_raw = re.sub(r'\d+:\d+', '', game_str.split("@")[1]).strip()
                        for part in (away_raw, home_raw):
                            if part:
                                keys_to_store.add(part.lower())
                                mapped = _team_to_venue(part)
                                if mapped:
                                    keys_to_store.add(mapped)
                    else:
                        # Game string might already be the venue name
                        keys_to_store.add(game_str.strip().lower())

                    for k in keys_to_store:
                        if k:
                            park_lookup[k] = park_entry

            # Augment BPP park entries with OWM wind direction at game time.
            # BPP provides game-time wind speed but not degrees; OWM /forecast gives
            # 3-hour intervals — we pick the block closest to each game's start time.
            _weather_key = os.getenv("OPENWEATHER_API_KEY", "").strip()
            if _weather_key and park_lookup:
                # Build venue→game_time from lineups so we fetch forecast for the right hour
                _venue_game_ts: dict[str, float] = {}
                try:
                    _lu_games = json.loads(lineups_json).get("games", [])
                    for _lg in _lu_games:
                        _gt = _lg.get("game_time")
                        _venue = (_lg.get("venue") or "").lower()
                        if _gt and _venue:
                            from datetime import datetime, timezone
                            _dt = datetime.fromisoformat(_gt.replace("Z", "+00:00"))
                            _venue_game_ts[_venue] = _dt.timestamp()
                except Exception:
                    pass

                _city_to_deg: dict[str, float | None] = {}
                for _pe in park_lookup.values():
                    if _pe.get("wind_deg") is not None:
                        continue
                    _city = next(
                        (_VENUE_CITY[_k] for _k in park_lookup
                         if park_lookup[_k] is _pe and _k in _VENUE_CITY),
                        None
                    )
                    if _city and _city not in _city_to_deg:
                        try:
                            _fc = requests.get(
                                "https://api.openweathermap.org/data/2.5/forecast",
                                params={"q": _city, "appid": _weather_key, "units": "imperial", "cnt": 16},
                                timeout=8,
                            ).json()
                            # Find the venue key to get game time
                            _venue_key = next(
                                (_k for _k in park_lookup
                                 if park_lookup[_k] is _pe and _k in _venue_game_ts),
                                None
                            )
                            _target_ts = _venue_game_ts.get(_venue_key) if _venue_key else None
                            _entries = _fc.get("list", [])
                            if _entries and _target_ts:
                                _best = min(_entries, key=lambda e: abs(e["dt"] - _target_ts))
                            elif _entries:
                                _best = _entries[0]
                            else:
                                _best = None
                            _city_to_deg[_city] = (_best.get("wind") or {}).get("deg") if _best else None
                        except Exception:
                            _city_to_deg[_city] = None
                    if _city and _city in _city_to_deg:
                        _pe["wind_deg"] = _city_to_deg[_city]

            # Merge into player_signals
            for _, signals in player_signals.items():
                player_lower = signals.get("player_name", "").lower()

                # BallparkPal matchup signals
                if player_lower in matchup_lookup:
                    bm = matchup_lookup[player_lower]
                    signals["bpp_vs_grade"]  = bm["bpp_vs_grade"]   # 0-10 matchup grade
                    signals["bpp_proj_rank"] = bm["bpp_rank"]
                    # Use BPP H2H HR as supplemental H2H if MLB API didn't return one
                    if signals.get("h2h_hr") is None and bm["bpp_h2h_hr"] is not None:
                        signals["h2h_hr"] = int(bm["bpp_h2h_hr"])

                # Park factors — use stored venue name from _build_game_cards,
                # falling back to partial string matching if exact key not found
                venue_name = signals.get("venue", "").lower()
                if venue_name in park_lookup:
                    pk = park_lookup[venue_name]
                else:
                    pk = next((v for k, v in park_lookup.items()
                                if k and venue_name and
                                (k in venue_name or venue_name in k)), None)
                if pk:
                    signals["park_hr_factor"]     = pk["park_hr_factor"]
                    signals["temp_f"]             = pk["temp_f"]
                    signals["wind_mph"]           = pk["wind_mph"]
                    signals["wind_deg"]           = pk.get("wind_deg")
                    signals["wind_direction_bpp"] = pk.get("wind_direction_bpp")
                    signals["wind_receptiveness"] = pk.get("wind_receptiveness")
                    signals["homerunsnumber"]     = pk.get("homerunsnumber")
                    signals["weather_hr_factor"]  = pk.get("weather_hr_factor")
                    signals["outfield_size"]      = pk.get("outfield_size")
                    signals["stadium_description"] = pk.get("stadium_description")
                    signals["roof_closed"]        = pk.get("roof_closed", False)
                    signals["altitude_ft"]        = pk.get("altitude_ft")
                    signals["humidity_pct"]       = pk.get("humidity_pct")
                    signals["pressure_mb"]        = pk.get("pressure_mb")
                    signals["carry_ft"]           = pk.get("carry_ft")

        except Exception:
            pass

        self._context = data
        return data

    @staticmethod
    def _fetch_last_batting_order(team_id: int) -> list[dict]:
        """
        Fetch batting order from this team's most recent completed game.
        Uses boxscore battingOrder to preserve slot positions.
        Returns list of {name, batting_order, bat_side} dicts (pitchers excluded).
        Falls back to [] if no recent game found.
        """
        from datetime import date, timedelta
        today = date.today()
        start = (today - timedelta(days=7)).isoformat()
        end   = (today - timedelta(days=1)).isoformat()
        try:
            sched = requests.get(
                "https://statsapi.mlb.com/api/v1/schedule",
                params={"teamId": team_id, "sportId": 1,
                        "startDate": start, "endDate": end, "gameType": "R"},
                timeout=10,
            )
            sched.raise_for_status()
            sched_data = sched.json()
        except Exception:
            return []

        # Walk dates newest-first to find the last completed game
        game_pk = None
        team_side = None
        for date_entry in reversed(sched_data.get("dates", [])):
            for game in date_entry.get("games", []):
                state = game.get("status", {}).get("abstractGameState", "")
                if state not in ("Final", "Game Over"):
                    continue
                teams = game.get("teams", {})
                if teams.get("home", {}).get("team", {}).get("id") == team_id:
                    team_side = "home"
                elif teams.get("away", {}).get("team", {}).get("id") == team_id:
                    team_side = "away"
                else:
                    continue
                game_pk = game["gamePk"]
                break
            if game_pk:
                break

        if not game_pk:
            return []

        try:
            box = requests.get(
                f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore",
                timeout=10,
            )
            box.raise_for_status()
            boxscore = box.json()
        except Exception:
            return []

        side_data   = boxscore.get("teams", {}).get(team_side, {})
        players     = side_data.get("players", {})
        batting_ids = side_data.get("battingOrder", [])

        result = []
        for slot, pid in enumerate(batting_ids, start=1):
            pdata = players.get(f"ID{pid}", {})
            pos   = pdata.get("position", {}).get("abbreviation", "")
            if pos in ("P", "SP", "RP"):
                continue
            name     = pdata.get("person", {}).get("fullName", "")
            bat_side = pdata.get("person", {}).get("batSide", {}).get("code", "")
            if name:
                result.append({"name": name, "batting_order": slot, "bat_side": bat_side})
        return result

    def _add_roster_fallback(self, lineups_json: str, player_signals: dict,
                            batter_stats: dict, pitcher_form: dict | None = None,
                            recent_form: list | None = None) -> dict:
        """
        For each team/game where lineup is not confirmed, use the batting order
        from the team's most recent completed game as a fallback.
        Players are added with lineup_confirmed=False and get a -2 scoring penalty.
        """
        try:
            lineups = json.loads(lineups_json)
        except Exception:
            return player_signals

        for game in lineups.get("games", []):
            for side_key in ("away", "home"):
                side = game.get(side_key, {})
                lineup_confirmed = side.get("lineup_confirmed", False)

                team_id = side.get("team_id")
                if not team_id:
                    continue

                last_order = self._fetch_last_batting_order(team_id)
                if not last_order:
                    continue

                team_abbrev = side.get("team", "")
                game_pk_str = str(game.get("game_pk") or "")
                for entry in last_order:
                    player_name = entry["name"]
                    if not player_name:
                        continue
                    sig_key = f"{player_name}||{game_pk_str}" if game_pk_str else player_name
                    # Always backfill batting_order for waiting players — covers both
                    # fully-unconfirmed lineups AND partial lineups where some players
                    # are confirmed but others are still [WAITING] with a roster index.
                    if sig_key in player_signals:
                        if not player_signals[sig_key].get("lineup_confirmed"):
                            player_signals[sig_key]["batting_order"] = entry["batting_order"]
                        continue

                    # Only add brand-new players when the whole lineup is unconfirmed.
                    if lineup_confirmed:
                        continue

                    sc_data  = _find_best_name_match(player_name, batter_stats)
                    bat_side = entry.get("bat_side") or "R"
                    sp_throws = game.get(side_key, {}).get("pitcher_throws", "R")
                    platoon = (
                        "PLATOON+" if bat_side != sp_throws
                        else ("platoon-" if bat_side == sp_throws else "unknown")
                    )

                    # Pitcher recent form — opposing pitcher ID is in the game object
                    opp_key    = "home" if side_key == "away" else "away"
                    opp_pid    = game.get(opp_key, {}).get("pitcher_id")
                    pf_data    = (pitcher_form or {}).get(opp_pid) or {}
                    p_hr_per_9 = pf_data.get("hr_per_9")

                    # Recent batter form — match by last name fragment
                    p_name_lower = player_name.lower()
                    form_hrs = next(
                        (r.get("hr_last_14d") for r in (recent_form or [])
                         if any(part in r.get("player", "").lower()
                                for part in p_name_lower.split() if len(part) > 3)),
                        0,
                    )

                    # Static venue constants as baseline (overridden by live BPP data later)
                    venue_name = game.get("venue", "")
                    vc = _VENUE_PARK_CONSTANTS.get(venue_name.lower(), {})

                    player_signals[sig_key] = {
                        "player_name":      player_name,
                        "team":             team_abbrev,
                        "game_pk":          game_pk_str or None,
                        "game_label":       None,  # roster fallback players don't have DH label yet
                        "status":           "waiting",
                        "lineup_confirmed": False,
                        "batting_order":    entry["batting_order"],
                        "platoon":          platoon,
                        "matchup":          (f"{game.get('away',{}).get('team','')} @ "
                                             f"{game.get('home',{}).get('team','')}"),
                        "game_time":        game.get("game_time") or None,
                        "venue":            venue_name,
                        "pa":               _safe_int(sc_data.get("pa")),
                        "barrel_rate":      _safe_float(sc_data.get("barrel_batted_rate")),
                        "hard_hit_pct":     _safe_float(sc_data.get("hard_hit_percent")),
                        "hr_fb_ratio":      _safe_float(sc_data.get("hr_flyballs_rate_batter")),
                        "xiso":             _safe_float(sc_data.get("xiso")),
                        "xslg":             _safe_float(sc_data.get("xslg")),
                        "xhr_rate":         _safe_float(sc_data.get("xhrs")),
                        "fb_pct":           _safe_float(sc_data.get("flyballs_percent")),
                        "launch_angle":     _safe_float(sc_data.get("launch_angle_avg")),
                        "ev_avg":           _safe_float(sc_data.get("exit_velocity_avg")),
                        "ev_max":           _safe_float(sc_data.get("max_hit_speed")),
                        "sweet_spot_pct":   _safe_float(sc_data.get("sweet_spot_percent")),
                        "season_hr":        _safe_int(sc_data.get("home_run")),
                        "hr_luck":          None,   # filled below: actual − expected HRs
                        "recent_form_14d":  _safe_int(form_hrs),
                        "pitcher_hr_per_9": p_hr_per_9,
                        "pitcher_hr_vs_hand": None,
                        "pitcher_barrel_pct": None,
                        "h2h_hr":            None,
                        "h2h_ab":           None,
                        "is_home":          side_key == "home",
                        "venue_slugging":   None,
                        "bpp_hr_pct":       None,
                        "bpp_proj_rank":    None,
                        "park_hr_factor":   vc.get("park_hr_factor"),
                        "temp_f":           None,
                        "wind_mph":         None,
                        "wind_receptiveness": None,
                        "weather_hr_factor": None,
                        "outfield_size":    vc.get("outfield_size"),
                        "stadium_description": None,
                    }

                    # Luck metric: actual HRs minus expected HRs from xHR%.
                    # Requires >=50 PA for stable sample. Negative = unlucky (bounce-back),
                    # positive = lucky (regression risk).
                    _sig = player_signals[sig_key]
                    _xhr_r = _sig.get("xhr_rate")
                    _pa_v  = _sig.get("pa") or 0
                    _shr_v = _sig.get("season_hr") or 0
                    if _xhr_r is not None and _pa_v >= 50:
                        _sig["hr_luck"] = round(_shr_v - _pa_v * (_xhr_r / 100), 2)

        return player_signals

    # ── Python-based scoring & ranking ────────────────────────────────────────

    # Cached ML weights — loaded once per process from ml_weights.json
    _ml_weights: dict | None = None
    _ml_weights_loaded: bool = False
    _lgbm_booster = None  # lightgbm.Booster, loaded alongside ml_weights.json

    @classmethod
    def _load_ml_weights(cls) -> dict | None:
        """Load ml_weights.json (and lgbm_model.txt if model_type=lightgbm)."""
        if cls._ml_weights_loaded:
            return cls._ml_weights
        cls._ml_weights_loaded = True
        base = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
        weights_path = os.path.join(base, "ml_weights.json")
        if os.path.exists(weights_path):
            try:
                with open(weights_path) as f:
                    cls._ml_weights = json.load(f)
                model_type = cls._ml_weights.get("model_type", "logistic_regression")
                print(f"  [ML] Loaded weights from ml_weights.json "
                      f"(trained {cls._ml_weights.get('trained_on','?')}, "
                      f"AUC={cls._ml_weights.get('cv_auc_mean', 0):.3f}, "
                      f"model={model_type})")
                if model_type == "lightgbm":
                    lgbm_path = os.path.join(base, "lgbm_model.txt")
                    if os.path.exists(lgbm_path):
                        import lightgbm as lgb
                        cls._lgbm_booster = lgb.Booster(model_file=lgbm_path)
                    else:
                        print("  [ML] Warning: lgbm_model.txt not found — ML scoring disabled")
                        cls._ml_weights = None
            except Exception as e:
                print(f"  [ML] Warning: failed to load weights: {e}")
                cls._ml_weights = None
        return cls._ml_weights

    @classmethod
    def _ml_score(cls, sig: dict) -> float | None:
        """
        Score a player using the trained ML model (LightGBM or logistic regression).
        Returns a float in the same 0–20 range as the hand-tuned scorer,
        or None if no model is available yet.
        """
        weights = cls._load_ml_weights()
        if not weights:
            return None

        feature_order = weights.get("feature_order", [])
        PLATOON_MAP = {"PLATOON+": 1.0, "platoon-": -1.0}

        raw_vals = []
        for feat in feature_order:
            if feat == "platoon":
                raw_vals.append(PLATOON_MAP.get(sig.get("platoon", ""), 0.0))
            else:
                v = sig.get(feat)
                raw_vals.append(float(v) if v is not None else float("nan"))

        model_type = weights.get("model_type", "logistic_regression")

        if model_type == "lightgbm":
            if cls._lgbm_booster is None:
                return None
            import numpy as np
            X = np.array([raw_vals], dtype=float)
            prob = float(cls._lgbm_booster.predict(X)[0])
            return round(prob * 20.0, 1)

        # Legacy logistic regression path
        coeffs  = weights.get("coefficients", {})
        intercept = weights.get("intercept", 0.0)
        means   = weights.get("scaler_mean", [])
        scales  = weights.get("scaler_scale", [])

        scaled = []
        for i, v in enumerate(raw_vals):
            if means and scales and i < len(means):
                mean_i  = means[i]
                scale_i = scales[i] if scales[i] != 0 else 1.0
                scaled.append(0.0 if (v != v) else (v - mean_i) / scale_i)
            else:
                scaled.append(0.0 if (v != v) else v)

        log_odds = intercept + sum(scaled[i] * coeffs.get(feat, 0.0)
                                   for i, feat in enumerate(feature_order))
        import math
        prob = 1.0 / (1.0 + math.exp(-log_odds))
        return round(prob * 20.0, 1)

    @staticmethod
    def _score_player(sig: dict) -> float:
        """
        Score a player 0–∞ using deterministic signal weights.
        Higher = better HR pick today. No LLM involved.

        When ml_weights.json exists (after optimize_weights.py has run),
        the ML score is blended 50/50 with the hand-tuned score.
        This lets the model gradually take over as data accumulates.

        Applies status-based penalties:
        - confirmed: no penalty (in today's lineup)
        - waiting: -1 penalty (on roster, waiting for lineup confirmation)
        - unknown: -3 penalty (status unclear)
        """
        score = 0.0

        # Season HR leaderboard boost — set by _rank_picks_python() before scoring.
        # Rewards elite proven power hitters so they're not undervalued on neutral days.
        score += sig.pop("_elite_hr_boost", 0.0)

        # Status-based penalty — only applies when we can't verify the player is actually playing.
        # Odds presence is equivalent to lineup confirmation: books don't post props for scratched players.
        has_odds = sig.get("ev_10") is not None
        status_penalty = 0.0
        if not has_odds:
            status = sig.get("status", "unknown")
            if status == "waiting":
                status_penalty -= 0.5
            elif status == "unknown":
                status_penalty -= 1.5
            # Roster fallback with no odds: extra skepticism (truly unverifiable).
            if not sig.get("lineup_confirmed", True):
                status_penalty -= 1.5
        score += status_penalty

        # EV — most important (Pinnacle probability as ground truth)
        ev = sig.get("ev_10")
        if ev is not None:
            if ev > 3:    score += 5
            elif ev > 1:  score += 3
            elif ev > 0:  score += 1
            elif ev > -1: score -= 1
            else:         score -= 3

        # Value edge vs consensus
        ve = sig.get("value_edge")
        if ve is not None:
            if ve >= 5:   score += 3
            elif ve >= 3: score += 2
            elif ve >= 1: score += 1

        # Platoon advantage
        platoon = sig.get("platoon", "")
        if platoon == "PLATOON+":   score += 2
        elif platoon == "platoon-": score -= 1

        # Recent HR form (last 14 days) — ML gives this 0% importance (noise vs season metrics).
        # Kept as a max +1 tie-breaker only; not a primary ranking driver.
        form = sig.get("recent_form_14d")
        if form is not None:
            if form >= 2: score += 1

        # Pitcher recent vulnerability (last 3 starts HR/9), scaled by batter power AND park.
        # Two gates:
        #   Power gate: strong (xISO>=0.270 or barrel>=15%) → full; moderate → half; weak → 0
        #   Park gate:  suppressive parks (≤90%) cut the bonus — a vulnerable pitcher in a
        #               HR-suppressing park still won't give up as many HRs as elsewhere.
        #               HR-friendly parks (≥110%) amplify the bonus.
        p_hr9 = sig.get("pitcher_hr_per_9")
        if p_hr9 is not None:
            _raw_xiso   = sig.get("xiso")
            _raw_barrel = sig.get("barrel_rate")
            _raw_park   = sig.get("park_hr_factor")
            _season_hr  = sig.get("season_hr") or 0
            _strong  = ((_raw_xiso   is not None and _raw_xiso   >= 0.270)
                        or (_raw_barrel is not None and _raw_barrel >= 15))
            _moderate = ((_raw_xiso   is not None and _raw_xiso   >= 0.210)
                         or (_raw_barrel is not None and _raw_barrel >= 10))
            # Park multiplier: 0.5 in strong suppressors, 1.25 in strong HR parks
            if _raw_park is not None and _raw_park <= 92:   _park_mult = 0.5
            elif _raw_park is not None and _raw_park >= 110: _park_mult = 1.25
            else:                                             _park_mult = 1.0
            # Season HR gate: pitcher vulnerability only matters if batter can hit HRs.
            # Players with 0-1 season HRs get little/no pitcher bonus regardless of matchup.
            if   _season_hr == 0: _hr_gate = 0.0
            elif _season_hr == 1: _hr_gate = 0.25
            elif _season_hr <= 3: _hr_gate = 0.5
            else:                 _hr_gate = 1.0
            _combined = _park_mult * _hr_gate
            if _strong:
                if p_hr9 >= 2.0:   score += 3 * _combined
                elif p_hr9 >= 1.0: score += 2 * _combined
                elif p_hr9 >= 0.5: score += 0   # below-average pitcher — neutral
                else:              score -= 2   # elite suppressor — meaningful penalty
            elif _moderate:
                if p_hr9 >= 2.0:   score += 1 * _combined
                elif p_hr9 >= 1.0: score += 1 * _combined
                elif p_hr9 < 0.5:  score -= 1   # suppressor penalty for moderate batters too

        # Pitcher barrel rate allowed — % of batted balls vs this pitcher that are barrels.
        # Stronger predictor than HR/9 because it strips park and luck: a pitcher allowing
        # lots of barrels will keep giving up HRs regardless of whether they've been hit yet.
        p_barrel = sig.get("pitcher_barrel_pct")
        if p_barrel is not None:
            if p_barrel >= 12:    score += 3   # Elite vulnerability (top ~5% worst pitchers)
            elif p_barrel >= 9:   score += 2
            elif p_barrel >= 7:   score += 1
            elif p_barrel <= 3:   score -= 1   # Pitcher suppresses hard contact well

        # Career pitcher HR/9 vs this batter's handedness — structural vulnerability, not recency bias.
        # A pitcher who gives up HRs to lefties will keep doing so regardless of recent form.
        p_vs_hand = sig.get("pitcher_hr_vs_hand")
        if p_vs_hand is not None:
            if p_vs_hand >= 1.5:    score += 2   # Structurally vulnerable to this side
            elif p_vs_hand >= 1.0:  score += 1
            elif p_vs_hand <= 0.25: score -= 1   # Dominates this handedness historically

        # Career handedness split (explicit signal; mirrors pitcher_hr_vs_hand but tracked separately
        # as an ML feature for importance attribution).
        career_hr_vs_hand = sig.get("pitcher_career_hr_vs_hand")
        if career_hr_vs_hand is not None:
            if career_hr_vs_hand >= 1.5:    score += 2
            elif career_hr_vs_hand >= 1.0:  score += 1
            elif career_hr_vs_hand <= 0.25: score -= 1

        # Career HRs at today's specific venue — venue-specific power indicator.
        # No penalty for 0: can't distinguish 0-in-0-PA from 0-in-50-PA without PA data.
        career_park_hr = sig.get("career_park_hr")
        if career_park_hr is not None:
            if career_park_hr >= 5:    score += 2
            elif career_park_hr >= 3:  score += 1
            elif career_park_hr >= 1:  score += 0.5

        # Pitcher pitch mix (directional — fastball favors HR, breaking/offspeed suppresses)
        fb_pct       = sig.get("pitcher_fb_pct")
        breaking_pct = sig.get("pitcher_breaking_pct")
        offspeed_pct = sig.get("pitcher_offspeed_pct")

        if fb_pct is not None:
            if fb_pct >= 60:   score += 2
            elif fb_pct >= 50: score += 1

        if breaking_pct is not None:
            if breaking_pct >= 35: score -= 2
            elif breaking_pct >= 25: score -= 1

        if offspeed_pct is not None:
            if offspeed_pct >= 20: score -= 1

        # Pitch-type Phase 2: batter's xSLG vs pitcher's dominant pitch type.
        # Only triggers when pitcher has a clear dominant pitch (>=50%), otherwise
        # the signal is ambiguous and no adjustment is made.
        _DOMINANT_THRESHOLD = 50.0
        bxslg_fb   = sig.get("batter_xslg_vs_fastball")
        bxslg_br   = sig.get("batter_xslg_vs_breaking")
        bxslg_os   = sig.get("batter_xslg_vs_offspeed")
        _dominant_pitch = None
        _dominant_pct   = 0.0
        for _ptype, _pct in (("fb", fb_pct), ("br", breaking_pct), ("os", offspeed_pct)):
            if _pct is not None and _pct >= _DOMINANT_THRESHOLD and _pct > _dominant_pct:
                _dominant_pitch = _ptype
                _dominant_pct   = _pct

        if _dominant_pitch == "fb" and bxslg_fb is not None:
            if bxslg_fb >= 0.500:   score += 2
            elif bxslg_fb >= 0.420: score += 1
            elif bxslg_fb <= 0.280: score -= 1
        elif _dominant_pitch == "br" and bxslg_br is not None:
            if bxslg_br >= 0.500:   score += 2
            elif bxslg_br >= 0.420: score += 1
            elif bxslg_br <= 0.280: score -= 1
        elif _dominant_pitch == "os" and bxslg_os is not None:
            if bxslg_os >= 0.500:   score += 2
            elif bxslg_os >= 0.420: score += 1
            elif bxslg_os <= 0.280: score -= 1

        # Statcast rate stats require a minimum sample to be reliable.
        # pa_scale weights their contribution based on sample size:
        #   PA >= 50 (or unknown): full weight — reliable sample for rate stats
        #   PA 30–49: 60% weight — real signal but still early-season noise
        #   PA 15–29: 25% weight — hot/called-up player with thin sample
        #   PA < 15:  zero weight — 1–2 good swings can inflate every rate to elite
        # When Statcast is down-weighted, bpp_boost compensates by amplifying
        # BallparkPal matchup grades (which don't depend on sample size).
        pa = sig.get("pa")
        if pa is None or pa >= 50:
            pa_scale  = 1.0
            bpp_boost = 1.0
        elif pa >= 30:
            pa_scale  = 0.6
            bpp_boost = 1.3
        elif pa >= 15:
            pa_scale  = 0.25
            bpp_boost = 1.5
        else:
            pa_scale  = 0.0
            bpp_boost = 1.6

        barrel = sig.get("barrel_rate") if pa_scale > 0 else None
        hh     = sig.get("hard_hit_pct") if pa_scale > 0 else None
        xiso   = sig.get("xiso") if pa_scale > 0 else None
        xslg   = sig.get("xslg") if pa_scale > 0 else None
        xhr    = sig.get("xhr_rate") if pa_scale > 0 else None
        fb     = sig.get("fb_pct") if pa_scale > 0 else None
        la     = sig.get("launch_angle") if pa_scale > 0 else None
        ev     = sig.get("ev_avg") if pa_scale > 0 else None
        ss     = sig.get("sweet_spot_pct") if pa_scale > 0 else None

        sc_statcast = 0.0

        # Barrel rate
        if barrel is not None:
            if barrel >= 25:   sc_statcast += 4  # Elite barrel rate (Judge, Schwarber tier)
            elif barrel >= 15: sc_statcast += 3
            elif barrel >= 10: sc_statcast += 2
            elif barrel >= 7:  sc_statcast += 1
            elif barrel >= 4:  sc_statcast += 0
            else:              sc_statcast -= 1

        # Hard hit %
        if hh is not None:
            if hh >= 50:   sc_statcast += 3  # Elite hard hit (Judge, Schwarber)
            elif hh >= 45: sc_statcast += 2
            elif hh >= 40: sc_statcast += 1

        # xISO — expected isolated slugging (pure power metric, park/luck neutral)
        # League avg xISO ~0.160. Elite HR hitters (Judge, Schwarber) are 0.300+.
        # Threshold raised from 0.250 to 0.300 after observing all 5 Apr-16 HR hitters
        # had xISO >= 0.300; <0.280 players consistently missed.
        if xiso is not None:
            if xiso >= 0.300:   sc_statcast += 5  # True power tier — all Apr-16 winners
            elif xiso >= 0.250: sc_statcast += 4
            elif xiso >= 0.220: sc_statcast += 3
            elif xiso >= 0.190: sc_statcast += 2
            elif xiso >= 0.165: sc_statcast += 1
            elif xiso >= 0.140: sc_statcast += 0
            elif xiso >= 0.110: sc_statcast -= 1
            else:               sc_statcast -= 2
            # Extra floor penalty below the observed winner threshold
            if xiso < 0.280:
                sc_statcast -= 1

        # xSLG — expected slugging from exit velocity + launch angle distribution.
        # Per FanGraphs research, this is more predictive of future HRs than actual
        # HR rate (r²=0.465 vs 0.421). League avg ~0.400. Elite power hitters are 0.600+.
        # Only score if xiso is missing (they measure similar things; avoid double-counting).
        if xslg is not None and xiso is None:
            if xslg >= 0.600:   sc_statcast += 4  # Elite power (Judge/Schwarber tier)
            elif xslg >= 0.520: sc_statcast += 3
            elif xslg >= 0.460: sc_statcast += 2
            elif xslg >= 0.410: sc_statcast += 1  # Above average
            elif xslg >= 0.350: sc_statcast += 0  # League average
            elif xslg >= 0.280: sc_statcast -= 1
            else:               sc_statcast -= 2  # Contact hitter

        # xHR% — expected HR rate (Savant). Most direct predictor per FanGraphs research.
        # Empty early in season (<~100 PA); captured automatically once Savant populates it.
        if xhr is not None:
            if xhr >= 6.0:   sc_statcast += 4   # Elite — hits HR in >1 of every 17 PA
            elif xhr >= 4.5: sc_statcast += 3
            elif xhr >= 3.5: sc_statcast += 2
            elif xhr >= 2.5: sc_statcast += 1
            elif xhr >= 1.5: sc_statcast += 0
            else:            sc_statcast -= 1

        # Fly ball rate — per RotoGrinders: strong correlation with HR volume.
        # More fly balls = more HR opportunities. Ground ball hitters rarely homer.
        if fb is not None:
            if fb >= 45:   sc_statcast += 3   # Elite fly ball hitter (Schwarber tier)
            elif fb >= 38: sc_statcast += 2
            elif fb >= 30: sc_statcast += 1
            elif fb < 20:  sc_statcast -= 2   # Ground ball hitter — unlikely to HR

        # Launch angle — from Savant glossary research (predictive correlations):
        # - Launch Angle Average: r=0.42 predictive for HR%
        # - Launch Angle (38+%): r=0.43 predictive — DO NOT penalize high angles
        # Launch angle — HRs almost exclusively come from 15°–50°. Sub-10° is
        # ground ball territory; 10°–15° is flat line drives. High barrel/hard-hit
        # doesn't matter if the ball never gets in the air.
        if la is not None:
            if la >= 25:   sc_statcast += 2   # Optimal HR zone
            elif la >= 20: sc_statcast += 1
            elif la >= 15: sc_statcast += 0   # Neutral — line drive zone
            elif la >= 10: sc_statcast -= 2   # Flat contact — rarely clears the wall
            else:          sc_statcast -= 4   # Ground ball profile — almost no HR upside

        # Exit velocity average — r=0.57 predictive for HR% (2nd strongest after barrel%).
        # MLB average ~88.5 mph. Elite HR hitters consistently 92+ mph.
        if ev is not None:
            if ev >= 93:    sc_statcast += 3   # Elite (Ohtani/Schwarber/Yordan tier)
            elif ev >= 91:  sc_statcast += 2   # Very good (Trout, Judge tier)
            elif ev >= 89:  sc_statcast += 1   # Above average
            elif ev < 86:   sc_statcast -= 2   # Weak contact — unlikely HR candidate
            elif ev < 87.5: sc_statcast -= 1   # Below average

        # Sweet spot% — r=0.42 predictive. % of batted balls at 8–32° launch angle.
        # Combines both fly balls AND hard line drives in the HR corridor.
        # MLB average ~33%. Elite power hitters: 40%+.
        # Added >=50% tier after Pereira (63.6%) homered at rank #17 on Apr-16 —
        # signal was underweighted at max +2 for a player well above elite threshold.
        if ss is not None:
            if ss >= 50:   sc_statcast += 3   # Exceptional (Pereira 63.6% homered at #17)
            elif ss >= 42: sc_statcast += 2   # Elite (Schwarber 45.5%, Yordan 42.3%)
            elif ss >= 37: sc_statcast += 1   # Good (Trout 37%)
            elif ss < 28:  sc_statcast -= 1   # Below average — poor contact profile

        # Max exit velocity — peak power ceiling. Avg EV can be dragged down by weak contact,
        # but max EV reveals the batter's true power when they square one up.
        # MLB avg max EV ~108 mph. True HR threats consistently 110+.
        ev_max = sig.get("ev_max") if pa_scale > 0 else None
        if ev_max is not None:
            if ev_max >= 115:  sc_statcast += 3   # Elite ceiling (Giancarlo/Judge tier)
            elif ev_max >= 112: sc_statcast += 2
            elif ev_max >= 109: sc_statcast += 1
            elif ev_max < 104:  sc_statcast -= 1   # Weak ceiling — HR unlikely even on best contact

        # Pull% — pull hitters have shorter paths to the wall and generate more bat speed
        # through the zone. Strong independent HR predictor, especially combined with high FB%.
        # Score as standalone signal (separate from the park-context pull×porch bonus).
        pull_pct_raw = sig.get("pull_pct") if pa_scale > 0 else None
        if pull_pct_raw is not None:
            if pull_pct_raw >= 52:   sc_statcast += 2   # Strong pull profile — HR machinery
            elif pull_pct_raw >= 44: sc_statcast += 1   # Above-average pull tendency
            elif pull_pct_raw < 32:  sc_statcast -= 1   # Opposite-field / spray hitter

        # Derived HR/FB rate — when Savant doesn't provide it directly, compute from
        # season HRs ÷ estimated fly balls (pa × fb%). Confirms power is translating.
        # Stored back into sig so the sustainability check below can use it.
        if sig.get("hr_fb_ratio") is None and fb is not None and fb > 0:
            _season_hr = sig.get("season_hr") or 0
            _pa        = sig.get("pa") or 0
            if _pa > 0:
                _est_fb = _pa * fb / 100
                if _est_fb >= 5:   # Need at least 5 fly balls for meaningful rate
                    sig["hr_fb_ratio"] = (_season_hr / _est_fb) * 100

        # Blast rate — % of swings qualifying as a Blast (bat speed + squared-up contact).
        # Formula: (percent_squared_up × 100) + bat_speed ≥ 164. ~7% is league average.
        # Extremely high correlation with HR/power outcomes per MLB bat-tracking research.
        blast = sig.get("blast_rate") if pa_scale > 0 else None
        if blast is not None:
            if blast >= 14:   sc_statcast += 4   # Elite (top ~5% of hitters)
            elif blast >= 11: sc_statcast += 3   # Great
            elif blast >= 8:  sc_statcast += 2   # Above average
            elif blast >= 6:  sc_statcast += 1   # Average+
            elif blast < 4:   sc_statcast -= 1   # Weak contact profile

        # HR/FB sustainability check — per RotoGrinders:
        # A high HR/FB rate on a low fly ball base is likely to regress.
        hr_fb_ratio = sig.get("hr_fb_ratio")
        if hr_fb_ratio is not None and fb is not None:
            implied_hr_rate = fb * hr_fb_ratio / 100
            if hr_fb_ratio > 20 and fb < 25:
                sc_statcast -= 2  # Unsustainable: high HR/FB on ground-ball profile
            elif implied_hr_rate >= 6.0:
                sc_statcast += 1  # FB% × HR/FB confirms genuine power production

        # Conflicting signal penalty — high contact quality metrics but poor outcome
        # metrics (low xiso + groundball profile) suggest the player hits it hard
        # but on the ground. These profiles score well on barrel/HH/EV but rarely HR.
        la = sig.get("launch_angle")
        if xiso is not None and la is not None:
            if xiso < 0.200 and la < 12:
                sc_statcast -= 2  # Hits hard but into the ground — unlikely HR

        # Contact hitter penalty — if BOTH barrel and xISO indicate low power,
        # penalize regardless of contextual bonuses (platoon, pitcher, EV).
        if barrel is not None and xiso is not None:
            if barrel < 8 and xiso < 0.160:
                sc_statcast -= 2  # Contact hitter — unlikely HR candidate
        elif barrel is not None and barrel < 4:
            sc_statcast -= 1
        elif xiso is not None and xiso < 0.110:
            sc_statcast -= 1

        # If a player has 75+ PA but low HR pace, their contact quality metrics
        # aren't translating to HRs — gap/line drive hitter profile. Scale down
        # the Statcast contribution so these players don't overrank on contact alone.
        _sc_pa  = sig.get("pa") or 0
        _sc_shr = sig.get("season_hr") or 0
        if _sc_pa >= 75:
            _hr_pace_sc = (_sc_shr / _sc_pa) * 500
            if   _hr_pace_sc < 5:  _sc_scale = 0.35
            elif _hr_pace_sc < 8:  _sc_scale = 0.55
            elif _hr_pace_sc < 15: _sc_scale = 0.80
            else:                  _sc_scale = 1.0
        else:
            _sc_scale = 1.0
        score += sc_statcast * pa_scale * _sc_scale

        # HR luck: actual − expected HRs. Negative = unlucky → bounce-back candidate.
        # Positive = overperforming → regression risk.
        # Only meaningful at 50+ PA (computed during signal build).
        hr_luck = sig.get("hr_luck")
        if hr_luck is not None:
            if   hr_luck <= -4: score += 2.0   # Massively unlucky — strong bounce-back signal
            elif hr_luck <= -2: score += 1.0   # Noticeably unlucky
            elif hr_luck <= -1: score += 0.5   # Slightly unlucky
            elif hr_luck >= 4:  score -= 2.0   # Massively lucky — regression risk
            elif hr_luck >= 2:  score -= 1.0   # Overperforming
            elif hr_luck >= 1:  score -= 0.5   # Slightly lucky


        # BallparkPal batter-vs-pitcher matchup grade (0–10, higher = better for batter)
        # This grades the specific batter against today's opposing pitcher.
        # 10 = excellent matchup (weak pitcher vs this batter's profile)
        # 0-2 = tough matchup (pitcher dominates this type of batter)
        bpp_vs = sig.get("bpp_vs_grade")
        if bpp_vs is not None:
            if bpp_vs >= 9:    score += 4 * bpp_boost   # Elite matchup
            elif bpp_vs >= 7:  score += 2 * bpp_boost   # Good matchup
            elif bpp_vs >= 5:  score += 1 * bpp_boost   # Neutral/slight advantage
            elif bpp_vs <= 2:  score -= 1 * bpp_boost   # Pitcher advantage

        # Park HR factor (stadium conduciveness to HRs)
        # Added <=85 tier after T-Mobile (82%) was only getting -1 (fell in <=90 bucket)
        # while suppressing picks like Canzone/Raley/Seager who all missed on Apr-16.
        park_hr = sig.get("park_hr_factor")
        if park_hr is None:
            venue_const = _VENUE_PARK_CONSTANTS.get((sig.get("venue") or "").lower())
            if venue_const:
                park_hr = venue_const["park_hr_factor"]
        if park_hr is not None:
            if park_hr >= 120:   score += 2
            elif park_hr >= 110: score += 1
            elif park_hr <= 75:  score -= 4  # Severe suppressor (Oracle Park tier)
            elif park_hr <= 80:  score -= 3  # Extreme suppressor
            elif park_hr <= 85:  score -= 2  # Strong suppressor (T-Mobile 82%)
            elif park_hr <= 90:  score -= 1  # Mild suppressor

        # Enhanced weather and wind factors
        temp = sig.get("temp_f")
        wind_mph = sig.get("wind_mph")
        wind_receptiveness = (sig.get("wind_receptiveness") or "").lower()
        weather_hr_factor = sig.get("weather_hr_factor")
        outfield_size = (sig.get("outfield_size") or "").lower()
        if not outfield_size:
            venue_const = _VENUE_PARK_CONSTANTS.get((sig.get("venue") or "").lower())
            if venue_const:
                outfield_size = (venue_const.get("outfield_size") or "").lower()
        stadium_desc = (sig.get("stadium_description") or "").lower()
        carry_ft = sig.get("carry_ft")

        # True if indoor: fixed dome OR retractable with roof currently closed.
        _wr = sig.get("wind_receptiveness", "") or ""
        is_dome_venue = (sig.get("venue", "").lower() in _FIXED_DOMES
                         or sig.get("roof_closed", False)
                         or "roof closed" in _wr.lower())

        if temp is not None and not is_dome_venue:
            if temp >= 85: score += 1
            elif temp <= 50: score -= 1

        # Wind scoring — two complementary signals:
        #
        # 1. BPP weather_hr_factor: combined weather effect (temp + humidity + wind).
        #    Direction-aware because BPP knows the stadium orientation.
        #    Primary signal for extreme weather (strong tail/headwind days).
        #
        # 2. wind_direction_bpp + speed + receptiveness: direct in/out signal.
        #    Supplements weather_hr_factor for cases where temperature partially
        #    offsets wind direction, keeping the combined number near neutral (92-108)
        #    even though wind is definitively blowing in or out.
        wind_dir_bpp = sig.get("wind_direction_bpp")
        recep_hi = "extreme" in wind_receptiveness or "high" in wind_receptiveness

        if not is_dome_venue:
            # Signal 1: BPP combined weather factor
            if weather_hr_factor is not None:
                if weather_hr_factor >= 115:   score += 2
                elif weather_hr_factor >= 108: score += 1
                elif weather_hr_factor <= 85:  score -= 2
                elif weather_hr_factor <= 92:  score -= 1

                # Receptiveness and strong wind amplify the combined effect
                if recep_hi:
                    if weather_hr_factor >= 108:   score += 0.5
                    elif weather_hr_factor <= 92:  score -= 0.5
                if wind_mph is not None and wind_mph >= 15:
                    if weather_hr_factor >= 108:   score += 0.5
                    elif weather_hr_factor <= 92:  score -= 0.5

            # Signal 2: direct wind direction (in vs out), scaled by speed + receptiveness.
            # Intentional partial overlap with Signal 1 — both are valid direction signals.
            # Threshold ≥10mph: below that, wind direction has negligible HR effect.
            if wind_dir_bpp and wind_mph is not None and wind_mph >= 10:
                if wind_dir_bpp == "out":
                    if wind_mph >= 15:   score += 1.0 if recep_hi else 0.5
                    else:                score += 0.5 if recep_hi else 0.0
                elif wind_dir_bpp == "in":
                    if wind_mph >= 15:   score -= 1.0 if recep_hi else 0.5
                    else:                score -= 0.5 if recep_hi else 0.0

        # BPP game environment: combined park + weather HR adjustment vs league average.
        # Positive = HR-friendly game environment; negative = suppressive.
        # Thresholds calibrated to today's range: Wrigley +0.56, Comerica -0.21, Fenway -0.58.
        # Scores separately from park_hr_factor — this captures the combined park+weather signal.
        # HR pace penalty: penalize batters not on track to hit meaningful HRs this season.
        # Uses projected pace (season_hr / PA * 500) so the threshold is season-invariant —
        # 1 HR in 20 PA = 25 HR pace (no penalty), 1 HR in 100 PA = 5 HR pace (heavy penalty).
        # Falls back to raw count with reduced penalties when PA < 30 (small sample).
        _shr = sig.get("season_hr") or 0
        _pa  = sig.get("pa") or 0
        if _pa >= 30:
            _hr_pace = (_shr / _pa) * 500   # projected HRs over 500 PA season
            if   _hr_pace <  8: score -= 2.5
            elif _hr_pace < 15: score -= 1.0
            elif _hr_pace < 22: score -= 0.5
        else:
            # Small sample: penalize only zero-HR players; 1+ HR in <30 PA is plausibly elite pace
            if _shr == 0: score -= 1.5

        hrn = sig.get("homerunsnumber")
        if hrn is not None and not is_dome_venue:
            if hrn >= 0.35:    score += 1.5   # Strongly favorable (Wrigley w/ tailwind)
            elif hrn >= 0.15:  score += 0.5   # Mildly favorable
            elif hrn <= -0.40: score -= 1.5   # Strongly suppressive (Fenway w/ headwind)
            elif hrn <= -0.20: score -= 1.0   # Suppressive (Comerica, Oracle)
            elif hrn <= -0.05: score -= 0.5   # Mildly suppressive

        # Ball carry (air density: altitude + humidity + pressure combined).
        # carry_ft = extra feet of ball distance vs sea-level neutral conditions.
        # Coors = +29ft; most sea-level parks = 0 to +5ft.
        if carry_ft is not None and not is_dome_venue:
            if carry_ft >= 25:   score += 2   # Extreme carry (Coors tier)
            elif carry_ft >= 15: score += 1   # Meaningful carry boost
            elif carry_ft <= -10: score -= 1  # Dense air suppresses distance

        # Outfield size impact
        if outfield_size:
            if outfield_size in ["small", "short"]:
                score += 1  # Small outfields help HRs
            elif outfield_size in ["large", "deep"]:
                score -= 1  # Large outfields hurt HRs

        # Stadium description analysis (basic keyword matching)
        # This is a framework - could be enhanced with more sophisticated analysis
        stadium_score = 0
        bat_side = sig.get("bat_side", "?").upper()

        if stadium_desc:
            # Positive factors
            if any(word in stadium_desc for word in ["short", "close", "friendly", "small"]):
                stadium_score += 0.5
            if "right field" in stadium_desc and "short" in stadium_desc:
                stadium_score += 0.5  # Right-handed power hitters benefit
            if "left field" in stadium_desc and "short" in stadium_desc:
                stadium_score += 0.5  # Left-handed power hitters benefit

            # Add a small batter-specific park boost for short porch directions
            if bat_side == "R" and "short left" in stadium_desc:
                stadium_score += 0.75
            if bat_side == "L" and "short right" in stadium_desc:
                stadium_score += 0.75
            if bat_side == "R" and "short porch" in stadium_desc and "left" in stadium_desc:
                stadium_score += 0.5
            if bat_side == "L" and "short porch" in stadium_desc and "right" in stadium_desc:
                stadium_score += 0.5

            # Pull tendency × short porch alignment
            # High pull% + short porch on the pull side = independent HR edge.
            pull_pct = sig.get("pull_pct") if pa_scale > 0 else None
            if pull_pct is not None:
                has_short_lf = "short left" in stadium_desc or ("short porch" in stadium_desc and "left" in stadium_desc)
                has_short_rf = "short right" in stadium_desc or ("short porch" in stadium_desc and "right" in stadium_desc)
                if bat_side == "R" and has_short_lf:
                    if pull_pct >= 48:   stadium_score += 2   # Strong pull hitter + short LF
                    elif pull_pct >= 40: stadium_score += 1
                elif bat_side == "L" and has_short_rf:
                    if pull_pct >= 48:   stadium_score += 2   # Strong pull hitter + short RF
                    elif pull_pct >= 40: stadium_score += 1

            # Negative factors
            if any(word in stadium_desc for word in ["deep", "large", "vast", "huge"]):
                stadium_score -= 0.5
            if "fence" in stadium_desc and any(word in stadium_desc for word in ["tall", "high"]):
                stadium_score -= 0.5  # High fences hurt HRs

        score += stadium_score

        # Elite leaderboard boost — top 10 in HR or xSLG league-wide.
        # Prevents model from undervaluing proven power hitters with moderate day signals.
        if sig.get("is_top10_leaderboard"):
            score += 2

        # ── Power floor gate ──────────────────────────────────────────────────
        # Conditions help power hitters hit HRs; they don't turn contact hitters
        # into power hitters. If Statcast says weak power (sc_statcast <= -2),
        # scale down the contextual portion (EV, platoon, pitcher, park, etc.)
        # so favorable conditions can't override a genuinely weak power profile.
        # Gate only applies when we have real Statcast data (pa_scale > 0).
        if pa_scale > 0 and sc_statcast <= -2:
            statcast_component = sc_statcast * pa_scale
            ctx_component = score - status_penalty - statcast_component
            # ctx_scale: 0.75 at sc=-2, 0.625 at sc=-3, 0.5 at sc<=-4
            ctx_scale = max(0.5, 1.0 + (sc_statcast + 2) * 0.125)
            score = status_penalty + ctx_component * ctx_scale + statcast_component

        # ── ML blend (active only when ml_weights.json exists) ────────────────
        # Blends the hand-tuned heuristic score with the logistic regression score.
        # Weight shifts toward ML as AUC improves (low AUC → trust heuristic more).
        ml = Homer._ml_score(sig)
        if ml is not None:
            weights = Homer._load_ml_weights()
            auc = weights.get("cv_auc_mean", 0.5) if weights else 0.5
            # ml_weight: 0 at AUC=0.5, ~56% at AUC=0.64, capped at 0.8
            # Multiplier raised from 2.5→4.0: feature importance shows ML correctly
            # prioritizes xISO/barrel/hard-hit over recency signals the heuristic over-weighted.
            ml_weight = min(0.8, max(0.0, (auc - 0.5) * 4.0))
            score = (1.0 - ml_weight) * score + ml_weight * ml

        return round(score, 1)

    @staticmethod
    def _deg_to_arrow(deg: float) -> str:
        """Convert meteorological wind degrees to the direction the wind blows TO.
        OWM wind.deg is the direction FROM which the wind blows (0=from N, 90=from E).
        We add 4 slots (180°) to get the TO direction, which is what matters for HR context."""
        arrows = ["↑", "↗", "→", "↘", "↓", "↙", "←", "↖"]
        idx = (round(deg / 45) + 4) % 8
        return arrows[idx]

    @staticmethod
    def _star_rating(score: float, auc: float) -> str:
        """
        Combine absolute score and model accuracy (AUC) into a 1–5 star rating.

        AUC ceiling (see GRADING.md):
          >= 0.65 → max 5 stars  (model reliable)
          >= 0.55 → max 4 stars  (model developing)
          <  0.55 → max 3 stars  (model near random)

        Score thresholds (not rank-bound):
          >= 19 → 5 stars (Elite Picks, capped by ceiling)
          >= 16 → 4 stars (Strong Plays)
          >= 14 → 3 stars (Solid Looks)
          >= 13 → 2 stars (Worth Watching)
          <  13 → 1 star  (Speculative)
        """
        if auc >= 0.65:
            ceiling = 5
        elif auc >= 0.55:
            ceiling = 4
        else:
            ceiling = 3

        if score >= 19:
            base = 5
        elif score >= 16:
            base = 4
        elif score >= 14:
            base = 3
        elif score >= 13:
            base = 2
        else:
            base = 1

        stars = min(base, ceiling)
        return "★" * stars + "☆" * (5 - stars)

    @staticmethod
    def _load_score_percentiles(max_stars: int, lookback_days: int = 30, min_samples: int = 20) -> list:
        """
        Compute score percentile thresholds from recent pick_factors history.
        Returns a list of (min_score, star_count) pairs sorted descending — first
        match wins. Falls back to legacy absolute thresholds if insufficient data.

        Percentile cutpoints by AUC ceiling:
          5-star: p92, p78, p62, p45  → 5, 4, 3, 2 stars
          4-star: p90, p75, p50       → 4, 3, 2 stars
          3-star: p75, p50            → 3, 2 stars
        """
        CUTPOINTS = {
            5: [(92, 5), (78, 4), (62, 3), (45, 2)],
            4: [(90, 4), (75, 3), (50, 2)],
            3: [(75, 3), (50, 2)],
        }
        cutpoints = CUTPOINTS.get(max_stars, CUTPOINTS[4])

        try:
            conn = get_db_conn()
            cur = conn.execute(
                "SELECT score FROM pick_factors WHERE score IS NOT NULL "
                "AND bet_date >= date('now', ?)",
                (f"-{lookback_days} days",),
            )
            scores = sorted(r[0] for r in cur.fetchall())
            conn.close()
        except Exception:
            scores = []

        if len(scores) < min_samples:
            # Fallback: legacy absolute thresholds (capped by max_stars)
            legacy = [(25, 5), (19, 4), (16, 3), (13, 2)]
            return [(t, min(s, max_stars)) for t, s in legacy if min(s, max_stars) >= 1]

        n = len(scores)
        result = []
        for pct, star_count in cutpoints:
            idx = max(0, int(n * pct / 100) - 1)
            result.append((scores[idx], star_count))
        return result  # already sorted descending by star_count (highest first)

    def _rank_picks_python(self, player_signals: dict, top_n: int = 8, verbose: bool = False, scratched: set = None) -> list:
        """
        Score every player (confirmed and roster) and return the top_n as a list of dicts.
        Each dict has: player, matchup, confidence, score, reasoning, signals.
        100% Python — no LLM, no hallucination.
        
        Status-based penalties are applied in _score_player():
        - confirmed: no penalty (in today's lineup)
        - waiting: -1 penalty (on roster, waiting for lineup confirmation)
        - unknown: -3 penalty (status unclear)
        """
        _scratched = {s.lower() for s in (scratched or [])}

        # Season leaderboard boost: top-10 in HR or xSLG get +2 bonus.
        # Prevents undervaluing elite proven power hitters on neutral signal days.
        _batter_stats = {
            k: {"home_run": v.get("season_hr") or 0, "xslg": v.get("xslg")}
            for k, v in player_signals.items()
        }
        _thresholds = self._compute_elite_boost_thresholds(_batter_stats)
        _hr_thr   = _thresholds["hr_threshold"]
        _xslg_thr = _thresholds["xslg_threshold"]

        def _is_elite(sg: dict) -> bool:
            if _hr_thr is not None and (sg.get("season_hr") or 0) >= _hr_thr > 0:
                return True
            if _xslg_thr is not None:
                try:
                    if float(sg.get("xslg") or 0) >= _xslg_thr > 0:
                        return True
                except (TypeError, ValueError):
                    pass
            return False

        scored = []
        for sig_key, sig in player_signals.items():
            if _is_elite(sig):
                sig["_elite_hr_boost"] = 2.0
            player = sig.get("player_name", sig_key.split("|")[0])
            if player.lower() in _scratched:
                continue
            # Minimum PA gate: Statcast metrics are unreliable below 50 PA.
            # Only exclude when PA is known — missing PA data is not penalized.
            _pa = sig.get("pa")
            if _pa is not None and _pa < 50:
                continue
            if sig.get("bat_side", "?") == "?":
                resolved = get_bat_side_by_name(player)
                if resolved != "?":
                    sig["bat_side"] = resolved
            sc = self._score_player(sig)
            
            if sc >= 10:   confidence = "HIGH"
            elif sc >= 5:  confidence = "MEDIUM"
            else:          confidence = "LOW"

            # Build a one-line reasoning string — ordered by ML feature importance.
            # Top 3 ML features: xISO (52.6%), hard_hit_pct (20.8%), barrel_rate (18.7%)
            reasons = []
            xiso_val = sig.get("xiso")
            if xiso_val is not None and xiso_val >= 0.200:
                reasons.append(f"xISO {xiso_val:.3f}")
            barrel = sig.get("barrel_rate")
            if barrel is not None and barrel >= 10:
                reasons.append(f"barrel {barrel:.1f}%")
            hh_pct = sig.get("hard_hit_pct")
            if hh_pct is not None and hh_pct >= 45:
                reasons.append(f"hard hit {hh_pct:.1f}%")
            ve = sig.get("value_edge")
            if ve is not None and ve >= 3:
                reasons.append(f"VALUE +{ve:.1f}pp")
            p_hr9 = sig.get("pitcher_hr_per_9")
            if p_hr9 is not None and p_hr9 >= 1.0:
                reasons.append(f"pitcher L3: {p_hr9:.1f}HR/9")
            h2h_hr = sig.get("h2h_hr")
            h2h_ab = sig.get("h2h_ab")
            if h2h_hr is not None and h2h_hr >= 1:
                reasons.append(f"h2h {h2h_hr}HR/{h2h_ab}AB")
            career_pk = sig.get("career_park_hr")
            if career_pk:
                venue_short = (sig.get("venue") or "park")[:22]
                reasons.append(f"career {venue_short}: {career_pk}HR")
            bpp_rank = sig.get("bpp_proj_rank")
            if bpp_rank is not None and bpp_rank <= 15:
                reasons.append(f"BPP rank #{bpp_rank}")
            form = sig.get("recent_form_14d")
            if form is not None and form >= 3:
                reasons.append(f"{form}HR last 14d")
            bpp_hr_pct = sig.get("bpp_hr_pct")
            if bpp_hr_pct is not None and bpp_hr_pct >= 16:
                reasons.append(f"BPP {bpp_hr_pct:.1f}%")
            park_hr = sig.get("park_hr_factor")
            if park_hr is not None and (park_hr >= 115 or park_hr <= 85):
                reasons.append(f"park {park_hr:.0f}%")
            _wr_r = sig.get("wind_receptiveness", "") or ""
            _is_dome = (sig.get("venue", "").lower() in _FIXED_DOMES
                        or sig.get("roof_closed", False)
                        or "roof closed" in _wr_r.lower())
            if not _is_dome:
                temp = sig.get("temp_f")
                if temp is not None and (temp >= 80 or temp <= 55):
                    reasons.append(f"{temp:.0f}°F")
                wind_mph = sig.get("wind_mph")
                wind_receptiveness = sig.get("wind_receptiveness")
                if wind_mph is not None and wind_mph <= 5:
                    wind_desc = f"calm {wind_mph:.0f}mph"
                    if wind_receptiveness and "high" in wind_receptiveness.lower():
                        wind_desc += " (wind receptive)"
                    reasons.append(wind_desc)
                weather_hr_factor = sig.get("weather_hr_factor")
                if weather_hr_factor is not None and (weather_hr_factor >= 110 or weather_hr_factor <= 90):
                    reasons.append(f"weather {weather_hr_factor:.0f}% HR factor")
            outfield_size = sig.get("outfield_size")
            if outfield_size and outfield_size.lower() in ["small", "short"]:
                reasons.append(f"{outfield_size} outfield")
            stadium_desc = sig.get("stadium_description") or ""
            bat_side = sig.get("bat_side", "?").upper()
            if bat_side == "?" :
                bat_side = get_bat_side_by_name(player).upper()
            if bat_side == "R" and "short left" in stadium_desc.lower():
                reasons.append("short left field")
            if bat_side == "L" and "short right" in stadium_desc.lower():
                reasons.append("short right field")
            if bat_side == "R" and "short porch" in stadium_desc.lower() and "left" in stadium_desc.lower():
                reasons.append("short left porch")
            if bat_side == "L" and "short porch" in stadium_desc.lower() and "right" in stadium_desc.lower():
                reasons.append("short right porch")

            # HR probability estimate: prefer BallparkPal park-adjusted HR%,
            # fall back to Pinnacle implied probability (already vig-stripped).
            hr_prob: float | None = None
            bpp_val = sig.get("bpp_hr_pct")
            if bpp_val is not None:
                hr_prob = round(float(bpp_val), 2)
            else:
                pin_odds = sig.get("pinnacle_odds")
                if pin_odds:
                    try:
                        o = int(pin_odds)
                        if o > 0:
                            hr_prob = round(100 / (o + 100) * 100, 2)
                        else:
                            hr_prob = round(abs(o) / (abs(o) + 100) * 100, 2)
                    except (ValueError, ZeroDivisionError):
                        pass

            scored.append({
                "player":     player,
                "matchup":    sig.get("matchup", ""),
                "confidence": confidence,
                "score":      sc,
                "hr_prob":    hr_prob,
                "reasoning":  ", ".join(reasons) if reasons else "Statcast/park signals",
                "dh_label":   sig.get("game_label"),
                "signals":    sig,
            })

        # Pure score sort — best picks first regardless of game
        scored.sort(key=lambda x: x["score"], reverse=True)

        # Assign star ratings by fixed rank bands within the top-15:
        #   #1–5   → ★★★★☆  Strong
        #   #6–10  → ★★★☆☆  Solid
        #   #11–15 → ★★☆☆☆  Speculative
        # Picks beyond top_n (roster fallback overflows) get 1 star.
        def _stars_from_rank(rank_1based: int) -> int:
            if rank_1based <= 5:
                return 4
            if rank_1based <= 10:
                return 3
            if rank_1based <= 15:
                return 2
            return 1

        for i, pick in enumerate(scored[:top_n], 1):
            s = _stars_from_rank(i)
            pick["stars"] = "★" * s + "☆" * (5 - s)

        for i, pick in enumerate(scored[top_n:], top_n + 1):
            pick["stars"] = "★☆☆☆☆"

        if verbose:
            print(f"\n  [SCORING DEBUG] Total players scored: {len(player_signals)}")
            print("  [SCORING DEBUG] Top 20 players by score:")
            for i, pick in enumerate(scored[:15], 1):
                status = pick["signals"].get("status", "unknown").upper()
                print(f"    {i:2}. [{status}] {pick['player']:<24} {pick['matchup']:<25} score={pick['score']:6.1f}")

            status_counts = {}
            for p in scored[:top_n]:
                status = p["signals"].get("status", "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1

            games_in_top8 = len(set(p["matchup"] for p in scored[:top_n]))
            status_summary = ", ".join(f"{count} {status}" for status, count in status_counts.items())
            print(f"\n  Top {top_n}: {status_summary}, {games_in_top8} different games")
        
        return scored[:top_n]

    @staticmethod
    def _format_narrative(ranked: list, date_str: str, availability: str) -> str:
        """Generate a card-per-player pick report from Python-ranked picks."""
        DIVIDER = "─" * 62
        lines = [f"TOP HR PICKS — {date_str}", "=" * 62]

        # Lineup alerts
        try:
            av = json.loads(availability)
            if av.get("alerts"):
                lines.append("\nLINEUP ALERTS:")
                for p in av["alerts"]:
                    lines.append(f"  !! {p} — not in confirmed lineup")
        except Exception:
            pass

        if not ranked:
            lines.append("\nNo confirmed lineups yet — batting orders haven't been posted.")
            lines.append("MLB submits lineups 2–4 hours before first pitch.")
            lines.append("Best time to run: 11am–noon ET on game days.")
            return "\n".join(lines)

        def _fmt(val, fmt=".1f", suffix="", fallback="—"):
            return f"{val:{fmt}}{suffix}" if val is not None else fallback

        # Pre-compute hit rates by star tier so section headers show historical rates.
        # Derive rank ranges from the actual picks so AUC shifts don't break the mapping.
        from .bet_tracker import score_bucket_hit_rate, STAR_SCORE_RANGES

        def _tier_label(stars: str) -> str:
            sc = stars.count("★")
            if sc not in STAR_SCORE_RANGES:
                return f"  {stars}"
            n, h = score_bucket_hit_rate(*STAR_SCORE_RANGES[sc])
            rate_str = f"{h/n*100:.0f}% HR rate  ({n} picks)" if n else "no history yet"
            return f"  {stars}  —  {rate_str}"

        current_stars = None
        for i, pick in enumerate(ranked, 1):
            sig    = pick["signals"]
            name   = pick["player"]
            stars  = pick.get("stars", "")
            status = sig.get("status", "unknown")
            status_tag = f"  [{status.upper()}]" if status != "confirmed" else ""

            # ── Section break when star tier changes ──────────────────
            if stars != current_stars:
                current_stars = stars
                lines.append(f"\n{'─'*62}")
                lines.append(_tier_label(stars))

            # ── Header row ────────────────────────────────────────────
            dh_label = pick.get("dh_label")
            dh_tag   = f"  [DH {dh_label}]" if dh_label else ""
            lines.append(f"\n{DIVIDER}")
            lines.append(f"#{i}  {name}{dh_tag}{status_tag}  {stars}")

            # ── Matchup / context row ──────────────────────────────────
            mu            = sig.get("matchup", "—")
            venue         = sig.get("venue", "")
            is_home       = sig.get("is_home")
            home_away_str = "Home" if is_home else "Away" if is_home is not None else "—"
            bat_side      = sig.get("bat_side", "?")
            if bat_side == "?":
                bat_side = get_bat_side_by_name(name)
            bat_label     = {"L": "LHB", "R": "RHB", "S": "SHB"}.get(bat_side, f"?HB")
            p_name        = sig.get("pitcher_name") or "TBD"
            p_throws      = sig.get("pitcher_throws", "?")
            platoon       = sig.get("platoon", "")
            # Parse game_time into a readable local time (ET)
            _gt = sig.get("game_time") or ""
            _time_str = ""
            if _gt:
                try:
                    from datetime import datetime as _dt
                    from zoneinfo import ZoneInfo
                    _utc = _dt.fromisoformat(_gt.replace("Z", "+00:00"))
                    _et  = _utc.astimezone(ZoneInfo("America/New_York"))
                    _time_str = f"  •  {_et.strftime('%-I:%M %p ET')}"
                except Exception:
                    _time_str = f"  •  {_gt[11:16]}"  # fallback: raw HH:MM
            lines.append(f"   {mu}{_time_str}")
            lines.append(f"   {bat_label} vs {p_name} ({p_throws})  •  {venue}  •  {home_away_str}")

            # ── Stats grid ────────────────────────────────────────────
            season_hr  = sig.get("season_hr")
            bat_order  = sig.get("batting_order")
            hr_str     = f"{season_hr} HR" if season_hr is not None else "— HR"
            order_str  = f"#{bat_order} in order" if bat_order else "—"
            lines.append(f"")
            lines.append(f"   Season:  {hr_str:<12}  Lineup:  {order_str}")

            barrel   = sig.get("barrel_rate")
            hh       = sig.get("hard_hit_pct")
            xiso_val = sig.get("xiso")
            ev_avg   = sig.get("ev_avg")
            sweet    = sig.get("sweet_spot_pct")
            fb_pct   = sig.get("fb_pct")
            hr_fb    = sig.get("hr_fb_ratio")
            form     = sig.get("recent_form_14d")

            lines.append(f"   Barrel:  {_fmt(barrel, suffix='%'):<12}  Hard Hit: {_fmt(hh, suffix='%'):<12}  xISO: {_fmt(xiso_val, '.3f')}")
            lines.append(f"   EV avg:  {_fmt(ev_avg, '.1f'):<12}  Sweet Sp: {_fmt(sweet, suffix='%'):<12}  FB%:  {_fmt(fb_pct, suffix='%')}")
            if hr_fb is not None or form is not None:
                lines.append(f"   HR/FB:   {_fmt(hr_fb, suffix='%'):<12}  Form 14d: {_fmt(form, 'd', ' HR')}")

            # ── Park / weather ─────────────────────────────────────────
            park_hr    = sig.get("park_hr_factor")
            temp       = sig.get("temp_f")
            wind       = sig.get("wind_mph")
            whr        = sig.get("weather_hr_factor")
            _wr_d = sig.get("wind_receptiveness", "") or ""
            is_dome    = (venue.lower() in _FIXED_DOMES
                          or sig.get("roof_closed", False)
                          or "roof closed" in _wr_d.lower())
            env_parts  = []
            if park_hr is not None:
                park_label = "HR-friendly" if park_hr >= 110 else ("HR-suppressor" if park_hr <= 90 else "neutral")
                env_parts.append(f"Park {park_hr:.0f}% ({park_label})")
            if is_dome:
                env_parts.append("Dome")
            else:
                if temp is not None: env_parts.append(f"{temp:.0f}°F")
                if wind is not None:
                    bpp_dir = sig.get("wind_direction_bpp")
                    dir_label = f" ({bpp_dir})" if bpp_dir else ""
                    env_parts.append(f"wind {wind:.0f}mph{dir_label}")
                if whr is not None and whr != 100: env_parts.append(f"weather {whr:.0f}% HR factor")
            if env_parts:
                lines.append(f"   {' | '.join(env_parts)}")

            # ── Pitcher / matchup intelligence ─────────────────────────
            p_hr9  = sig.get("pitcher_hr_per_9")
            h2h_hr = sig.get("h2h_hr")
            h2h_ab = sig.get("h2h_ab")
            v_slg  = sig.get("venue_slugging")
            bpp_r  = sig.get("bpp_proj_rank")
            intel  = []
            if p_hr9  is not None:             intel.append(f"Pitcher L3: {p_hr9:.1f} HR/9")
            if h2h_hr is not None:             intel.append(f"H2H: {h2h_hr} HR / {h2h_ab or '—'} AB")
            if v_slg:                          intel.append(f"{home_away_str} SLG: {v_slg}")
            if bpp_r  is not None:             intel.append(f"BPP rank #{bpp_r}")
            if intel:
                lines.append(f"   {' | '.join(intel)}")

            # ── Odds / value ───────────────────────────────────────────
            ev    = sig.get("ev_10")
            kelly = sig.get("kelly_size")
            ve    = sig.get("value_edge")
            pin   = sig.get("pinnacle_odds")
            odds_parts = []
            if pin:                            odds_parts.append(f"Pinnacle {pin}")
            if ev  is not None:                odds_parts.append(f"EV ${ev:+.2f}")
            if kelly is not None and kelly > 0: odds_parts.append(f"Kelly ${kelly:.2f}")
            if ve  is not None and ve >= 3:    odds_parts.append(f"VALUE +{ve:.1f}pp")
            if odds_parts:
                lines.append(f"   {' | '.join(odds_parts)}")

            # ── Why ────────────────────────────────────────────────────
            if pick.get("reasoning"):
                lines.append(f"   Why: {pick['reasoning']}")

        lines.append(f"\n{DIVIDER}")
        lines.append("\n  Verify scratches before betting: https://www.mlb.com/starting-lineups")
        return "\n".join(lines)

    # ── Public interface ───────────────────────────────────────────────────────

    def run(self, user_message: str, scratched: set = None) -> str:
        today = date.today().isoformat()

        # Step 1 — gather all real data via Python (cached on instance)
        context = self._gather_data()

        # Step 2 — rank with deterministic Python scorer (no LLM)
        player_signals = context.get("player_signals", {})
        ranked         = self._rank_picks_python(player_signals, top_n=15, scratched=scratched)

        # Step 3 — format into a readable narrative
        return self._format_narrative(ranked, today, context.get("availability", "{}"))

    def get_picks_json(self, top_n: int = 8, scratched: set = None) -> list:
        """
        Return today's top picks as a structured list of dicts for auto-logging.

        Each dict has:
          player      : full player name (from confirmed MLB lineup)
          matchup     : "AWAY @ HOME" from live MLB schedule
          confidence  : "HIGH" / "MEDIUM" / "LOW"
          reasoning   : one-line justification built from real signals
          score       : raw Python score
          signals     : full signal dict for performance tracking

        Args:
            top_n: Maximum number of picks to return (default 8).
        """
        context        = self._gather_data()
        player_signals = context.get("player_signals", {})
        return self._rank_picks_python(player_signals, top_n=top_n, scratched=scratched)

