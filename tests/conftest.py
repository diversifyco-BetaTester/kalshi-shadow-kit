import pytest


@pytest.fixture(autouse=True)
def _never_touch_real_downloads(tmp_path_factory, monkeypatch):
    """ops.auto_refresh writes boards into DOWNLOADS (~/Downloads by default): keep every test out of it."""
    from ops import auto_refresh
    monkeypatch.setattr(auto_refresh, "DOWNLOADS", tmp_path_factory.mktemp("downloads"))


@pytest.fixture(autouse=True)
def _never_touch_real_chrome(monkeypatch):
    """Every AppleScript call goes through auto_refresh.osa_raw: a test that reaches it without its own
    fake would drive the real Chrome, so fail loudly instead."""
    from ops import auto_refresh

    def blocked(*args):
        raise AssertionError(f"test tried to run osascript against real Chrome: {args[:3]}")
    monkeypatch.setattr(auto_refresh, "osa_raw", blocked)


@pytest.fixture(autouse=True)
def _never_call_espn_from_tests(monkeypatch):
    """local.enricher's default HTTP client hits ESPN: tests pass get= or fail here."""
    from local import enricher

    async def blocked(url, params):
        raise AssertionError(f"test tried to call ESPN for real: {url}")
    monkeypatch.setattr(enricher, "http_get", blocked)
    enricher._cache.clear()


@pytest.fixture(autouse=True)
def _never_fetch_espn_cfb_teams(monkeypatch):
    """fetch_odds loads ESPN's CFB team list on first use: tests set fetch_odds.CFB_TEAMS themselves."""
    from local import fetch_odds

    def blocked():
        raise AssertionError("test tried to fetch ESPN's CFB team list for real")
    monkeypatch.setattr(fetch_odds, "_espn_teams_get", blocked)
    monkeypatch.setattr(fetch_odds, "CFB_TEAMS", None)


@pytest.fixture(autouse=True)
def _never_call_espn_injuries_from_tests(monkeypatch):
    """local.injury_guard's default HTTP client hits ESPN: tests pass get= or fail here."""
    from local import injury_guard

    async def blocked(url):
        raise AssertionError(f"test tried to call ESPN for real: {url}")
    monkeypatch.setattr(injury_guard, "http_get", blocked)
    injury_guard._cache.clear()


@pytest.fixture(autouse=True)
def _heartbeats_go_to_tmp(tmp_path_factory, monkeypatch):
    """Sentinel cycles in tests must not overwrite the real local/.heartbeat_<sport>.json that /status reads."""
    from local import sentinel
    monkeypatch.setattr(sentinel, "HEARTBEAT", str(tmp_path_factory.mktemp("hb") / ".heartbeat_{sport}.json"))


@pytest.fixture
def stacks_allowed(monkeypatch):
    """Stacks are off by default (pipeline_config optimizer.allow_stacks); tests of the stack model turn them on."""
    from local import pipeline_config
    monkeypatch.setitem(pipeline_config.CONFIG["optimizer"], "allow_stacks", True)


@pytest.fixture(autouse=True)
def _backtest_never_grades_for_real(tmp_path_factory, monkeypatch):
    """The backtest grades legs through grade_slips (ESPN) and caches them in data/: tests inject a grader
    and write their outcome cache to tmp."""
    from local import backtest, grade_slips

    def blocked(url):
        raise AssertionError(f"test tried to call ESPN for real: {url}")
    monkeypatch.setattr(grade_slips, "get_json", blocked)
    monkeypatch.setattr(backtest, "OUTCOMES", tmp_path_factory.mktemp("bt") / "outcomes.sqlite3")


@pytest.fixture(autouse=True)
def _never_call_the_odds_api_for_real(monkeypatch):
    """Every Odds API request goes through OddsBook._http_get unless a test injects get=: fail loudly."""
    from local import fetch_odds

    async def blocked(self, url, params):
        raise AssertionError(f"test tried to call The Odds API for real: {url}")
    monkeypatch.setattr(fetch_odds.OddsBook, "_http_get", blocked)


@pytest.fixture(autouse=True)
def _tests_use_default_pipeline_config(monkeypatch):
    """local/pipeline_config.json is the user's live tuning (e.g. a 57% NHL floor): tests run on the built-in
    DEFAULTS, so a config edit never changes what a test checks. Tests that need a setting set it themselves."""
    from local import pipeline_config
    defaults = pipeline_config.load(pipeline_config.PATH.with_name("__no_such_config__.json"))
    for k, v in defaults.items():
        monkeypatch.setitem(pipeline_config.CONFIG, k, v)
    # shading is ON in production (base haircut on every Kalshi leg); the tests that predate it check exact
    # probabilities and EVs, so it is off here and the shading tests switch it on (shading_on)
    monkeypatch.setitem(pipeline_config.CONFIG["shading"], "enabled", False)


@pytest.fixture
def shading_on(monkeypatch):
    """The production shading (pipeline_config "shading" DEFAULTS), for the tests of it."""
    from local import pipeline_config
    monkeypatch.setitem(pipeline_config.CONFIG["shading"], "enabled", True)


@pytest.fixture
def books_ladder_full(monkeypatch):
    """Sportsbook ladder interpolation and whole-line pricing are OFF by default (pipeline_config books, since
    2026-10-23); tests of those mechanisms switch them back on."""
    from local import pipeline_config
    monkeypatch.setitem(pipeline_config.CONFIG, "books", {"ladder_interp": True, "whole_lines": True, "alt_ladder": True})


@pytest.fixture
def books_alt_exact(monkeypatch):
    """Alternate-ladder fetches on, exact rungs only (interpolation and whole lines still off)."""
    from local import pipeline_config
    monkeypatch.setitem(pipeline_config.CONFIG, "books", {"ladder_interp": False, "whole_lines": False, "alt_ladder": True})
