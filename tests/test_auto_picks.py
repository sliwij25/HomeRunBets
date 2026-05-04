import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch, MagicMock
import json
import sqlite3
import tempfile
import pathlib
from datetime import datetime, timezone, timedelta


def _make_mlb_schedule(minutes_from_now: int):
    """Return a fake MLB Stats API schedule response."""
    game_time = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
    return {
        "dates": [{
            "games": [
                {"gameDate": game_time.strftime("%Y-%m-%dT%H:%M:%SZ")},
            ]
        }]
    }


def test_get_first_game_time_returns_minutes():
    """get_first_game_time() returns integer minutes until first game."""
    import importlib
    fake_resp = MagicMock()
    fake_resp.json.return_value = _make_mlb_schedule(90)

    with patch("requests.get", return_value=fake_resp):
        import scripts.get_first_game_time as m
        importlib.reload(m)
        result = m.minutes_to_first_game()

    assert 85 <= result <= 95


def test_get_first_game_time_no_games():
    """Returns 9999 when no games are scheduled."""
    import importlib
    fake_resp = MagicMock()
    fake_resp.json.return_value = {"dates": []}

    with patch("requests.get", return_value=fake_resp):
        import scripts.get_first_game_time as m
        importlib.reload(m)
        result = m.minutes_to_first_game()

    assert result == 9999


def test_get_first_game_time_game_already_started():
    """Returns negative minutes when first game is in the past."""
    import importlib
    fake_resp = MagicMock()
    fake_resp.json.return_value = _make_mlb_schedule(-20)

    with patch("requests.get", return_value=fake_resp):
        import scripts.get_first_game_time as m
        importlib.reload(m)
        result = m.minutes_to_first_game()

    assert result < 0


def test_schedule_wake_calls_pmset():
    """schedule_wake() calls pmset with a time 70 min before first game."""
    import scripts.schedule_wake as sw
    import importlib; importlib.reload(sw)

    with patch("scripts.schedule_wake.minutes_to_first_game", return_value=120), \
         patch("subprocess.run") as mock_run:
        sw.schedule_wake()

    assert mock_run.called
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "pmset"
    assert cmd[1] == "schedule"
    assert cmd[2] == "wake"
    assert len(cmd[3]) > 0


def test_schedule_wake_skips_when_past():
    """schedule_wake() does nothing if computed wake time is already past."""
    import scripts.schedule_wake as sw
    import importlib; importlib.reload(sw)

    with patch("scripts.schedule_wake.minutes_to_first_game", return_value=60), \
         patch("subprocess.run") as mock_run:
        sw.schedule_wake()

    assert not mock_run.called


def test_schedule_wake_skips_no_games():
    """schedule_wake() does nothing when no games today."""
    import scripts.schedule_wake as sw
    import importlib; importlib.reload(sw)

    with patch("scripts.schedule_wake.minutes_to_first_game", return_value=9999), \
         patch("subprocess.run") as mock_run:
        sw.schedule_wake()

    assert not mock_run.called


# ────────────────────────────────────────────────────────────────────────────────
# detect_scratches tests
# ────────────────────────────────────────────────────────────────────────────────

def _make_test_db(players_and_teams: list[tuple[str, str]]) -> pathlib.Path:
    """Create a temp DB with today's pick_factors rows."""
    from datetime import date
    today = date.today().isoformat()
    db_path = pathlib.Path(tempfile.mktemp(suffix=".db"))
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE pick_factors (
            bet_date TEXT, player TEXT, team TEXT, rank INTEGER
        )
    """)
    for rank, (player, team) in enumerate(players_and_teams, start=1):
        conn.execute(
            "INSERT INTO pick_factors VALUES (?, ?, ?, ?)",
            (today, player, team, rank)
        )
    conn.commit()
    conn.close()
    return db_path


def _make_lineup_response(team_name: str, confirmed_players: list[str], confirmed: bool = True):
    """Return fake MLB schedule JSON with one game."""
    return {
        "dates": [{
            "games": [{
                "lineups": {
                    "homePlayers": [
                        {"fullName": p, "batSide": {"code": "R"}, "id": i}
                        for i, p in enumerate(confirmed_players, start=1)
                    ] if confirmed else []
                },
                "teams": {
                    "home": {"team": {"name": team_name, "id": 1}},
                    "away": {"team": {"name": "Other Team", "id": 2}},
                }
            }]
        }]
    }


def test_detect_scratches_player_absent_from_confirmed_lineup(tmp_path):
    """Player in top-20 but absent from confirmed lineup → scratched, exits 1."""
    import scripts.detect_scratches as ds
    import importlib; importlib.reload(ds)

    db = _make_test_db([("Aaron Judge", "New York Yankees")])
    scratched_file = tmp_path / "scratched.json"

    lineup_resp = MagicMock()
    lineup_resp.json.return_value = _make_lineup_response(
        "New York Yankees", ["Giancarlo Stanton", "Anthony Volpe"]
    )

    with patch("scripts.detect_scratches.DB_PATH", db), \
         patch("scripts.detect_scratches.SCRATCHED_FILE", scratched_file), \
         patch("requests.get", return_value=lineup_resp):
        result = ds.detect_and_update_scratches()

    assert result == ["Aaron Judge"]
    saved = json.loads(scratched_file.read_text())
    assert "Aaron Judge" in saved["players"]


def test_detect_scratches_player_in_confirmed_lineup(tmp_path):
    """Player in top-20 and present in confirmed lineup → no scratch, returns []."""
    import scripts.detect_scratches as ds
    import importlib; importlib.reload(ds)

    db = _make_test_db([("Aaron Judge", "New York Yankees")])
    scratched_file = tmp_path / "scratched.json"

    lineup_resp = MagicMock()
    lineup_resp.json.return_value = _make_lineup_response(
        "New York Yankees", ["Aaron Judge", "Anthony Volpe"]
    )

    with patch("scripts.detect_scratches.DB_PATH", db), \
         patch("scripts.detect_scratches.SCRATCHED_FILE", scratched_file), \
         patch("requests.get", return_value=lineup_resp):
        result = ds.detect_and_update_scratches()

    assert result == []


def test_detect_scratches_unconfirmed_lineup_skips(tmp_path):
    """Player in top-20 but lineup not confirmed → no scratch (too early to tell)."""
    import scripts.detect_scratches as ds
    import importlib; importlib.reload(ds)

    db = _make_test_db([("Aaron Judge", "New York Yankees")])
    scratched_file = tmp_path / "scratched.json"

    lineup_resp = MagicMock()
    lineup_resp.json.return_value = _make_lineup_response(
        "New York Yankees", [], confirmed=False
    )

    with patch("scripts.detect_scratches.DB_PATH", db), \
         patch("scripts.detect_scratches.SCRATCHED_FILE", scratched_file), \
         patch("requests.get", return_value=lineup_resp):
        result = ds.detect_and_update_scratches()

    assert result == []


def test_detect_scratches_fuzzy_name_match(tmp_path):
    """Fuzzy matching handles accented characters / slight name variations."""
    import scripts.detect_scratches as ds
    import importlib; importlib.reload(ds)

    db = _make_test_db([("Yordan Alvarez", "Houston Astros")])
    scratched_file = tmp_path / "scratched.json"

    lineup_resp = MagicMock()
    lineup_resp.json.return_value = _make_lineup_response(
        "Houston Astros", ["Yordan Álvarez", "Jose Altuve"]
    )

    with patch("scripts.detect_scratches.DB_PATH", db), \
         patch("scripts.detect_scratches.SCRATCHED_FILE", scratched_file), \
         patch("requests.get", return_value=lineup_resp):
        result = ds.detect_and_update_scratches()

    assert result == []  # should NOT be scratched — fuzzy match found him


def test_detect_scratches_no_picks_in_db(tmp_path):
    """Returns [] immediately when no picks exist in DB for today."""
    import scripts.detect_scratches as ds
    import importlib; importlib.reload(ds)

    db = _make_test_db([])
    scratched_file = tmp_path / "scratched.json"

    with patch("scripts.detect_scratches.DB_PATH", db), \
         patch("scripts.detect_scratches.SCRATCHED_FILE", scratched_file), \
         patch("requests.get") as mock_get:
        result = ds.detect_and_update_scratches()

    assert result == []
    assert not mock_get.called


def test_detect_scratches_mlb_api_error(tmp_path):
    """Returns [] (safe) when MLB API errors — never false-scratch on network failure."""
    import scripts.detect_scratches as ds
    import importlib; importlib.reload(ds)

    db = _make_test_db([("Aaron Judge", "New York Yankees")])
    scratched_file = tmp_path / "scratched.json"

    with patch("scripts.detect_scratches.DB_PATH", db), \
         patch("scripts.detect_scratches.SCRATCHED_FILE", scratched_file), \
         patch("requests.get", side_effect=Exception("timeout")):
        result = ds.detect_and_update_scratches()

    assert result == []
