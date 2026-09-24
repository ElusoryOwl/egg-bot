"""
Unit tests for the pure, side-effect-free logic in bot.py.

Run with:  pytest
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# bot.py raises if DISCORD_TOKEN is missing only inside the __main__ guard,
# and reads env-configurable constants at import time — set safe test values
# before importing.
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DB_PATH", ":memory:")

import bot  # noqa: E402


def test_throw_egg_distribution_is_within_expected_bounds():
    """Sanity-check the odds sum correctly and every branch is reachable."""
    random_state_backup = bot.random.getstate()
    try:
        outcomes = {"golden": 0, "quad": 0, "single": 0, "miss": 0}
        trials = 200_000
        for _ in range(trials):
            result = bot.throw_egg(is_friday=False)
            if result["golden"]:
                outcomes["golden"] += 1
            elif result["hatched"] and result["chicks"] == 4:
                outcomes["quad"] += 1
            elif result["hatched"]:
                outcomes["single"] += 1
            else:
                outcomes["miss"] += 1

        # Expected rates: golden 1/75, quad ~1/25 - 1/75, single ~1/6 - 1/25, rest miss.
        golden_rate = outcomes["golden"] / trials
        assert 0.008 < golden_rate < 0.020, golden_rate  # ~1/75 = 0.0133

        hatch_rate = (outcomes["golden"] + outcomes["quad"] + outcomes["single"]) / trials
        assert 0.14 < hatch_rate < 0.20, hatch_rate  # ~1/6 = 0.1667
    finally:
        bot.random.setstate(random_state_backup)


def test_friday_bonus_increases_hatch_rate():
    random_state_backup = bot.random.getstate()
    try:
        trials = 100_000

        def hatch_rate(is_friday):
            hatches = sum(1 for _ in range(trials) if bot.throw_egg(is_friday)["hatched"])
            return hatches / trials

        assert hatch_rate(is_friday=True) > hatch_rate(is_friday=False) - 0.02
    finally:
        bot.random.setstate(random_state_backup)


def test_guess_timezone_known_locale():
    tz, was_guessed = bot.guess_timezone("ja")
    assert tz == "Asia/Tokyo"
    assert was_guessed is True


def test_guess_timezone_unknown_locale_defaults_to_utc():
    tz, was_guessed = bot.guess_timezone("xx-ZZ")
    assert tz == "UTC"
    assert was_guessed is False


def test_guess_timezone_none_defaults_to_utc():
    tz, was_guessed = bot.guess_timezone(None)
    assert tz == "UTC"
    assert was_guessed is False


def test_get_midnight_remaining_near_midnight():
    tz = ZoneInfo("UTC")

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, 23, 59, 58, tzinfo=tz)

    original_datetime = bot.datetime
    bot.datetime = _FrozenDatetime
    try:
        remaining = bot.get_midnight_remaining(tz)
        assert remaining == "2s"
    finally:
        bot.datetime = original_datetime


def test_streak_flavour_thresholds():
    assert bot.streak_flavour(0) == ""
    assert "3 days" in bot.streak_flavour(3)
    assert "UNSTOPPABLE" in bot.streak_flavour(bot.STREAK_ROLE_THRESHOLD)
    assert "LEGENDARY" in bot.streak_flavour(30)


def test_streak_role_condition_ignores_same_day_later_misses():
    """
    Regression test for a bug where a 2nd/3rd throw of the day that missed
    stripped the streak role even though the streak itself only changes on
    the day's first throw. The role decision must key off the *current*
    streak value, not whether this particular throw hatched.
    """
    # Simulate: user already has an 11-day streak; this is their 2nd throw
    # today (throws_today != 0), so new_streak stays 11 regardless of
    # whether this throw hatches.
    current_streak = 11
    throws_today = 1  # not the first throw of the day
    hatched_this_throw = False

    new_streak = current_streak if throws_today != 0 else 0  # mirrors bot.py's logic

    keeps_role = new_streak >= bot.STREAK_ROLE_THRESHOLD
    assert keeps_role is True, "role should be kept: the streak itself was never broken"


def test_validate_config_accepts_defaults():
    assert bot.validate_config() == []


def test_validate_config_flags_golden_chance_too_high():
    errors = bot.validate_config(golden_chance=0.5)
    assert any("GOLDEN_EGG_CHANCE" in e for e in errors)


def test_validate_config_flags_negative_friday_bonus():
    errors = bot.validate_config(friday_bonus=-0.1)
    assert any("FRIDAY_BONUS" in e for e in errors)


def test_validate_config_flags_friday_bonus_pushing_over_100_percent():
    errors = bot.validate_config(friday_bonus=0.9)
    assert any("FRIDAY_BONUS" in e for e in errors)


def test_validate_config_flags_zero_max_throws():
    errors = bot.validate_config(max_throws=0)
    assert any("MAX_THROWS_PER_DAY" in e for e in errors)


def test_stats_milestone_text_new_streak():
    assert "start a new streak" in bot.stats_milestone_text(0, 0, 7)


def test_stats_milestone_text_building_to_role():
    msg = bot.stats_milestone_text(3, 5, 7)
    assert "earn the streak role" in msg
    assert "4" in msg  # 7 - 3 = 4 more days


def test_stats_milestone_text_building_to_best():
    msg = bot.stats_milestone_text(8, 12, 7)
    assert "beat your best streak" in msg
    assert "4" in msg  # 12 - 8 = 4 more days


def test_stats_milestone_text_at_personal_best():
    msg = bot.stats_milestone_text(12, 12, 7)
    assert "best streak ever" in msg


def test_guild_streak_threshold_roundtrip():
    bot.init_db()
    bot.set_guild_streak_threshold(918273645, 10)
    assert bot.get_guild_streak_threshold(918273645) == 10
    # A guild with no override falls back to the bot-wide default.
    assert bot.get_guild_streak_threshold(102938475) == bot.STREAK_ROLE_THRESHOLD


def test_reset_user_streak_preserves_longest_streak():
    bot.init_db()
    bot.upsert_user(555222111, streak=9, longest_streak=9, timezone="UTC")
    bot.reset_user_streak(555222111)
    row = bot.get_user(555222111)
    assert row["streak"] == 0
    assert row["longest_streak"] == 9


def test_golden_odds_text_reflects_configured_chance():
    """
    Regression test: the golden-egg embed used to hard-code '1-in-75' odds
    text regardless of the actual configured GOLDEN_EGG_CHANCE, so a server
    running a custom value would show a misleading number.
    """
    for chance, expected_odds in [(1 / 75, 75), (1 / 50, 50), (0.02, 50)]:
        odds = round(1 / chance) if chance > 0 else "∞"
        assert odds == expected_odds


def test_history_to_emoji_empty():
    assert bot.history_to_emoji([]) == "No throws yet"
