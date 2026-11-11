"""Send the daily digest (and alerts) by email through Gmail's SMTP server.

    python -m delivery.send_email --day 2026-11-10 --dry-run   # build the email, save it as .eml, do not send
    python -m delivery.send_email --day 2026-11-10             # send (once per day; --resend to send again)

Email: subject with the status, body = the checked digest (digest.md as HTML), attachment = dashboard.html.
Settings from .env (names only here): SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, DIGEST_TO.
SMTP_PASSWORD is a Gmail app password (Google Account -> Security -> App passwords). It is never logged.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import ssl
import sys
from datetime import date
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import markdown
from dotenv import load_dotenv

from connectors.base import PROJECT_ROOT, utc_now_z
from storage.db import connect

DIGEST_DIR = PROJECT_ROOT / "data" / "digests"
DASH_DIR = PROJECT_ROOT / "data" / "dashboards"
OUTBOX = PROJECT_ROOT / "data" / "outbox"
MAX_ATTACHMENT = 20 * 1024 * 1024       # Gmail's limit is 25 MB per email (after encoding)

ORANGE, WHITE, BLACK = "#ffa028", "#ffffff", "#000000"


def settings() -> dict:
    load_dotenv(PROJECT_ROOT / ".env")
    s = {"host": os.environ.get("SMTP_HOST", "smtp.gmail.com"), "port": int(os.environ.get("SMTP_PORT", "465")),
         "user": os.environ.get("SMTP_USER", "").strip(), "password": os.environ.get("SMTP_PASSWORD", "").replace(" ", ""),
         "to": os.environ.get("DIGEST_TO", "").strip()}
    s["to"] = s["to"] or s["user"]
    return s


def _scrub(text: str, s: dict) -> str:
    """Never let the password reach a log or the database."""
    return text.replace(s["password"], "***") if s.get("password") else text


# ---------- building the email ----------

def digest_html(md_text: str) -> str:
    """digest.md -> HTML with inline styles (email programs ignore most <style> blocks)."""
    html = markdown.markdown(md_text, extensions=["tables", "sane_lists"])
    styles = {
        "h1": f"color:{WHITE};font-size:20px;margin:0 0 8px",
        "h2": f"color:{WHITE};font-size:16px;border-bottom:1px solid {ORANGE};padding-bottom:3px;margin:22px 0 8px",
        "h3": f"color:{WHITE};font-size:14px;margin:16px 0 6px",
        "p": f"color:{ORANGE};margin:0 0 10px",
        "li": f"color:{ORANGE};margin:0 0 6px",
        "table": "border-collapse:collapse;font-size:12px;margin:6px 0 12px",
        "th": f"color:{WHITE};text-align:left;border-bottom:1px solid {ORANGE};padding:4px 8px",
        "td": f"color:{ORANGE};border-bottom:1px solid #262626;padding:4px 8px",
        "a": "color:#5ec8ff",
        "strong": f"color:{WHITE}",
        "em": "color:#bbbbbb",
        "hr": "border:0;border-top:1px solid #333",
    }
    for tag, css in styles.items():
        html = re.sub(rf"<{tag}(\s[^>]*)?>", lambda m: f"<{tag}{m.group(1) or ''} style=\"{css}\">", html)
    return (f'<div style="background:{BLACK};color:{ORANGE};font:14px/1.5 Arial,Helvetica,sans-serif;'
            f'padding:18px 22px;max-width:1100px">{html}</div>')


def subject_for(day: date, digest_json: dict | None, status: str) -> str:
    kept = sum(len(s.get("kept", [])) for s in (digest_json or {}).get("sections", {}).values())
    tag = {"ok": "", "partial": "⚠ partial · ", "blocked": "⛔ blocked · "}.get(status, f"{status} · ")
    return f"Daily digest {day.isoformat()} · {tag}{kept} items · SIMULATION"


def build_digest_email(day: date, s: dict) -> tuple[EmailMessage, dict]:
    d_dir = DIGEST_DIR / day.isoformat()
    md_path = d_dir / "digest.md"
    if not md_path.exists():
        raise FileNotFoundError(f"No digest for {day}: run `python -m pipeline.run_digest --day {day}` first.")
    md = md_path.read_text(encoding="utf-8")
    dj_path = d_dir / "digest.json"
    dj = json.loads(dj_path.read_text(encoding="utf-8")) if dj_path.exists() else None
    status = (dj or {}).get("status", "ok")

    msg = EmailMessage()
    msg["Subject"] = subject_for(day, dj, status)
    msg["From"] = s["user"] or "signal-digest@localhost"
    msg["To"] = s["to"] or "you@localhost"
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="signal-digest.local")

    dash = DASH_DIR / day.isoformat() / "dashboard.html"
    live = os.environ.get("DASHBOARD_URL", "").strip()          # set in the GitHub workflow (GitHub Pages)
    note = ("The interactive dashboard is attached (dashboard.html): download it and open it in a browser."
            if dash.exists() else "No dashboard was built today.")
    if live and dash.exists():
        note = f"Live dashboard: {live}  (also attached as dashboard.html)"
    msg.set_content(f"{note}\n\n{md}")                                   # plain-text version
    msg.add_alternative(digest_html(f"*{note}*\n\n" + md), subtype="html")
    info = {"attachment": None}
    if dash.exists():
        size = dash.stat().st_size
        if size <= MAX_ATTACHMENT:
            msg.add_attachment(dash.read_bytes(), maintype="text", subtype="html",
                               filename=f"dashboard_{day.isoformat()}.html")
            info["attachment"] = size
        else:
            info["attachment_skipped"] = f"dashboard is {size / 1e6:.1f} MB, over the {MAX_ATTACHMENT / 1e6:.0f} MB limit"
    return msg, info


def build_alert_email(day: date, issues: list[str], s: dict) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"⛔ Pipeline alert {day.isoformat()}: {issues[0][:80] if issues else 'problem'}"
    msg["From"] = s["user"] or "signal-digest@localhost"
    msg["To"] = s["to"] or "you@localhost"
    msg["Date"] = formatdate(localtime=True)
    body = "The daily run had problems:\n\n" + "\n".join(f"- {i}" for i in issues) + \
           f"\n\nDetails: data/logs/{day.isoformat()}.log and the daily_runs table."
    msg.set_content(body)
    return msg


# ---------- sending ----------

def _record(conn, day: date, kind: str, msg: EmailMessage, status: str, error: str | None) -> None:
    conn.execute("""INSERT INTO deliveries (day, kind, subject, recipient, status, error, size_bytes, sent_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                 (day.isoformat(), kind, msg["Subject"], msg["To"], status, error, len(msg.as_bytes()), utc_now_z()))
    conn.commit()


def already_sent(conn, day: date) -> bool:
    return conn.execute("SELECT 1 FROM deliveries WHERE day = ? AND kind = 'digest' AND status = 'sent'",
                        (day.isoformat(),)).fetchone() is not None


def deliver(msg: EmailMessage, day: date, kind: str, *, dry_run: bool = False, conn=None) -> dict:
    s = settings()
    own = conn is None
    conn = conn or connect()
    try:
        if dry_run:
            OUTBOX.mkdir(parents=True, exist_ok=True)
            path = OUTBOX / f"{day.isoformat()}_{kind}.eml"
            path.write_bytes(msg.as_bytes())
            _record(conn, day, kind, msg, "dry_run", None)
            return {"status": "dry_run", "path": str(path), "size_mb": round(len(msg.as_bytes()) / 1e6, 2)}
        missing = [k for k in ("SMTP_USER", "SMTP_PASSWORD") if not s[k.split("_")[1].lower()]]
        if missing:
            raise RuntimeError(f"{', '.join(missing)} not set in .env (see delivery/send_email.py)")
        try:
            with smtplib.SMTP_SSL(s["host"], s["port"], context=ssl.create_default_context(), timeout=60) as smtp:
                smtp.login(s["user"], s["password"])
                smtp.send_message(msg)
        except Exception as exc:
            err = _scrub(f"{type(exc).__name__}: {exc}", s)
            _record(conn, day, kind, msg, "failed", err)
            return {"status": "failed", "error": err}
        _record(conn, day, kind, msg, "sent", None)
        return {"status": "sent", "to": msg["To"], "size_mb": round(len(msg.as_bytes()) / 1e6, 2)}
    finally:
        if own:
            conn.close()


def send_digest(day: date, *, dry_run: bool = False, resend: bool = False) -> dict:
    conn = connect()
    try:
        if not dry_run and not resend and already_sent(conn, day):
            return {"status": "skipped", "reason": f"digest for {day} already sent (use --resend)"}
        msg, info = build_digest_email(day, settings())
        return {**deliver(msg, day, "digest", dry_run=dry_run, conn=conn), **info, "subject": msg["Subject"]}
    finally:
        conn.close()


def send_alert(day: date, issues: list[str], *, dry_run: bool = False) -> dict:
    return deliver(build_alert_email(day, issues, settings()), day, "alert", dry_run=dry_run)


def main() -> None:
    parser = argparse.ArgumentParser(description="Email the daily digest")
    parser.add_argument("--day", default="today")
    parser.add_argument("--dry-run", action="store_true", help="Save the email as data/outbox/<day>_digest.eml, do not send")
    parser.add_argument("--resend", action="store_true", help="Send again even if already sent today")
    parser.add_argument("--test-alert", action="store_true", help="Send a test alert email instead")
    args = parser.parse_args()
    day = date.today() if args.day == "today" else date.fromisoformat(args.day)
    r = send_alert(day, ["test alert: email delivery works"], dry_run=args.dry_run) if args.test_alert else \
        send_digest(day, dry_run=args.dry_run, resend=args.resend)
    for k, v in r.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
