from datetime import datetime, timedelta, timezone

from claude_rc_prune import verdict

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
DAY = timedelta(hours=24)


def test_deletes_a_session_idle_for_over_a_day():
    assert verdict({"updated_at": "2026-10-06T12:00:00Z"}, NOW, DAY) == "DELETE"


def test_keeps_a_session_active_today():
    assert verdict({"updated_at": "2026-10-08T09:00:00.123Z"}, NOW, DAY) == "keep"


def test_deletes_an_old_session_still_marked_connected():
    session = {"updated_at": "2026-09-01T00:00:00Z", "connection_status": "connected"}
    assert verdict(session, NOW, DAY) == "DELETE"


def test_falls_back_to_created_at():
    assert verdict({"created_at": "2026-10-01T00:00:00Z"}, NOW, DAY) == "DELETE"
