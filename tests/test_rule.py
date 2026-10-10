from datetime import date

from examples.weather_deadbucket.rule import death_time, event_day, kill_threshold, safe_f


def test_safe_f_lower_bounds_whole_celsius_reports():
    assert safe_f(72.0) == 72
    assert safe_f(69.8) == 68                  # 21°C may be 68.9°F


def test_kill_thresholds():
    assert kill_threshold("between", 72, 73) == 74
    assert kill_threshold("less", None, 80) == 80
    assert kill_threshold("greater", 79, None) is None


def test_single_spike_does_not_kill():
    # NYC 2026-08-27: one bogus 80°F between 70s; official high was 77°F
    obs = [(0, 70.0), (1, 80.0), (2, 71.0), (3, 71.0)]
    assert death_time(obs, 0, 10, "less", None, 80, confirm=2) == (None, None)
    assert death_time(obs, 0, 10, "less", None, 80, confirm=1)[0] == 1


def test_confirmed_rise_kills():
    obs = [(0, 70.0), (1, 74.0), (2, 75.0), (3, 73.0)]
    assert death_time(obs, 0, 10, "between", 72, 73, confirm=2) == (2, 74)


def test_window_bounds():
    obs = [(0, 90.0), (1, 90.0), (5, 60.0), (6, 60.0)]
    assert death_time(obs, 2, 10, "less", None, 80, confirm=2) == (None, None)


def test_event_day():
    assert event_day("KXHIGHNY-26OCT08") == date(2026, 10, 8)
