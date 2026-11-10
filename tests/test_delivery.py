"""Milestone 7: digest email (fake SMTP server, nothing is really sent) and the daily run's alert rules."""
import json
from datetime import date
from pathlib import Path

import pytest

from delivery import send_email
from storage import db

DAY = date(2026, 11, 10)


class FakeSMTP:
    sent: list = []
    fail: Exception | None = None

    def __init__(self, host, port, context=None, timeout=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        if FakeSMTP.fail:
            raise FakeSMTP.fail
        self.user = user

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(send_email, "connect", lambda: db.connect(path))
    monkeypatch.setattr(send_email, "load_dotenv", lambda *a, **k: None)     # never read the real .env in tests
    monkeypatch.setattr(send_email, "DIGEST_DIR", tmp_path / "digests")
    monkeypatch.setattr(send_email, "DASH_DIR", tmp_path / "dash")
    monkeypatch.setattr(send_email, "OUTBOX", tmp_path / "outbox")
    monkeypatch.setattr(send_email.smtplib, "SMTP_SSL", FakeSMTP)
    for k, v in {"SMTP_USER": "me@example.com", "SMTP_PASSWORD": "abcd efgh ijkl mnop", "DIGEST_TO": "me@example.com"}.items():
        monkeypatch.setenv(k, v)
    FakeSMTP.sent, FakeSMTP.fail = [], None
    d = tmp_path / "digests" / DAY.isoformat()
    d.mkdir(parents=True)
    (d / "digest.md").write_text("# Daily digest\n\n## Company news\n\n- UI revenue $1,026.2 million [1]\n\n"
                                 "| Ticker | Close |\n|---|---|\n| UI | 675.15 |\n", encoding="utf-8")
    (d / "digest.json").write_text(json.dumps({"status": "ok", "sections": {"UI": {"kept": [{}, {}]}}}))
    (tmp_path / "dash" / DAY.isoformat()).mkdir(parents=True)
    (tmp_path / "dash" / DAY.isoformat() / "dashboard.html").write_text("<html>dash</html>")
    return path


def test_email_has_digest_body_and_dashboard_attachment(env):
    r = send_email.send_digest(DAY)
    assert r["status"] == "sent" and len(FakeSMTP.sent) == 1
    msg = FakeSMTP.sent[0]
    assert msg["Subject"] == "Daily digest 2026-11-10 · 2 items · SIMULATION"
    html = msg.get_body(("html",)).get_content()
    assert "<table" in html and "style=" in html and "$1,026.2 million" in html
    files = [p.get_filename() for p in msg.iter_attachments()]
    assert files == ["dashboard_2026-11-10.html"]


def test_digest_is_not_sent_twice(env):
    assert send_email.send_digest(DAY)["status"] == "sent"
    assert send_email.send_digest(DAY)["status"] == "skipped"
    assert send_email.send_digest(DAY, resend=True)["status"] == "sent"
    assert len(FakeSMTP.sent) == 2


def test_dry_run_saves_eml_and_sends_nothing(env):
    r = send_email.send_digest(DAY, dry_run=True)
    assert r["status"] == "dry_run" and Path(r["path"]).exists() and FakeSMTP.sent == []


def test_failure_is_recorded_without_the_password(env):
    FakeSMTP.fail = RuntimeError("bad login for abcdefghijklmnop")
    r = send_email.send_digest(DAY)
    assert r["status"] == "failed" and "abcdefghijklmnop" not in r["error"] and "***" in r["error"]
    conn = db.connect(env)
    row = conn.execute("SELECT status, error FROM deliveries").fetchone()
    assert row["status"] == "failed" and "abcdefghijklmnop" not in row["error"]


def test_missing_settings_give_a_clear_error(env, monkeypatch):
    monkeypatch.delenv("SMTP_PASSWORD")
    with pytest.raises(RuntimeError, match="SMTP_PASSWORD not set"):
        send_email.send_digest(DAY)


def test_alert_email(env):
    r = send_email.send_alert(DAY, ["classify failed: Claude sign-in"])
    assert r["status"] == "sent"
    assert FakeSMTP.sent[0]["Subject"].startswith("⛔ Pipeline alert 2026-11-10: classify failed")


def test_daily_run_alerts_and_keeps_going(env, monkeypatch, tmp_path):
    from pipeline import run_classify, run_daily, run_dashboard, run_digest, run_fetch
    monkeypatch.setattr(run_daily, "connect", lambda: db.connect(env))
    monkeypatch.setattr(run_daily, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(run_fetch, "run", lambda day: [{"connector": "fred", "status": "failed", "errors": ["timeout"]}])

    def broken_classify(day):
        raise RuntimeError("Claude sign-in expired")
    monkeypatch.setattr(run_classify, "run", broken_classify)
    monkeypatch.setattr(run_digest, "run", lambda day: {"status": "ok", "kept": 2})
    monkeypatch.setattr(run_dashboard, "run", lambda day: {"status": "ok"})

    r = run_daily.run(DAY)
    assert r["status"] == "warnings"
    assert any("fred failed" in i for i in r["issues"]) and any("classify failed" in i for i in r["issues"])
    subjects = [m["Subject"] for m in FakeSMTP.sent]
    assert subjects[0].startswith("Daily digest") and subjects[1].startswith("⛔ Pipeline alert")   # digest still sent
    row = db.connect(env).execute("SELECT status, issues FROM daily_runs").fetchone()
    assert row["status"] == "warnings" and "classify failed" in row["issues"]


def test_daily_run_without_digest_sends_only_an_alert(env, monkeypatch, tmp_path):
    from pipeline import run_classify, run_daily, run_digest, run_fetch
    monkeypatch.setattr(run_daily, "connect", lambda: db.connect(env))
    monkeypatch.setattr(run_daily, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(run_fetch, "run", lambda day: [])
    monkeypatch.setattr(run_classify, "run", lambda day: {"items": 0, "labelled": 0, "quarantined": 0})

    def broken_digest(day):
        raise RuntimeError("digest blocked: table broken")
    monkeypatch.setattr(run_digest, "run", broken_digest)
    r = run_daily.run(DAY)
    assert r["status"] == "failed"
    assert len(FakeSMTP.sent) == 1 and FakeSMTP.sent[0]["Subject"].startswith("⛔ Pipeline alert")
