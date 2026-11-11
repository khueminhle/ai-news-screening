"""Time format and headline cleaning: the shared rules every source goes through."""
from datetime import date, datetime, timezone

from connectors.base import clean_headline, to_utc_z


def test_finnhub_unix_seconds_become_utc():
    assert to_utc_z(1793984525) == "2026-11-06T17:02:05Z"


def test_naive_time_is_treated_as_utc():
    assert to_utc_z("2026-11-08 17:30:00") == "2026-11-08T17:30:00Z"


def test_sec_and_offsets_are_converted():
    assert to_utc_z("2026-11-06T12:10:12.000Z") == "2026-11-06T12:10:12Z"
    assert to_utc_z("2026-11-06 16:05:42-06") == "2026-11-06T22:05:42Z"  # FRED, US Central
    assert to_utc_z(datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)) == "2026-10-28T18:00:00Z"


def test_calendar_dates_are_never_shifted():
    assert to_utc_z("2026-11-06") == "2026-11-06"
    assert to_utc_z(date(2026, 11, 6)) == "2026-11-06"


def test_empty_values():
    assert to_utc_z(None) is None
    assert to_utc_z("") is None


def test_clean_headline_rules():
    assert clean_headline("Ubiquiti Soars 15% on Record $1 Billion Quarter - Yahoo Finance") == \
        "ubiquiti soars 15 on record 1 billion quarter"
    assert clean_headline("Apple is racing to make Siri more agentic - The Verge") == \
        clean_headline("Apple is racing to make Siri more agentic")
    assert clean_headline(None) == ""
