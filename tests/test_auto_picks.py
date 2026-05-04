import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch, MagicMock
import json
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
