"""Milestone 7: the whole day in one command (this is what the schedule runs).

    python -m pipeline.run_daily                 # fetch -> classify -> digest -> dashboard -> email
    python -m pipeline.run_daily --dry-run       # everything, but the email is saved to data/outbox/ instead of sent
    python -m pipeline.run_daily --resend        # send again even if today's digest was already sent

Rules:
  - A connector that fails does not stop the run (the digest's freshness section flags it).
  - Classify failing (e.g. Claude sign-in expired) does not stop the run: the digest uses what is already labelled.
  - No digest (or a blocked one) -> nothing is sent except an alert.
  - Any problem -> one alert email with the list. The digest is never sent twice for the same day.
Log: data/logs/<day>.log and a row in daily_runs.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import traceback
from datetime import date
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.base import PROJECT_ROOT, utc_now_z
from storage.db import connect

LOG_DIR = PROJECT_ROOT / "data" / "logs"
log = logging.getLogger("daily")


def _setup_log(day: date) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{day.isoformat()}.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    console.setLevel(logging.INFO)
    log.addHandler(console)
    log.propagate = True
    return path


def _step(name: str, fn, steps: dict, issues: list[str]):
    started = time.monotonic()
    log.info("== %s ==", name)
    try:
        result = fn()
        steps[name] = {"status": "ok", "seconds": round(time.monotonic() - started, 1), "summary": result}
        log.info("%s done in %.1fs: %s", name, steps[name]["seconds"], json.dumps(result, default=str)[:600])
        return result
    except BaseException as exc:                     # SystemExit included (e.g. "run run_digest first")
        steps[name] = {"status": "failed", "seconds": round(time.monotonic() - started, 1), "error": str(exc)[:500]}
        issues.append(f"{name} failed: {type(exc).__name__}: {str(exc)[:200]}")
        log.error("%s failed:\n%s", name, traceback.format_exc())
        return None


def run(day: date, *, dry_run: bool = False, resend: bool = False) -> dict:
    from delivery import send_email
    from pipeline import run_classify, run_dashboard, run_digest, run_fetch

    log_path = _setup_log(day)
    conn = connect()
    run_id = conn.execute("INSERT INTO daily_runs (day, started_at, log_path) VALUES (?, ?, ?)",
                          (day.isoformat(), utc_now_z(), str(log_path))).lastrowid
    conn.commit()
    steps: dict = {}
    issues: list[str] = []

    fetched = _step("fetch", lambda: run_fetch.run(day), steps, issues)
    for r in fetched or []:
        if r["status"] == "failed":
            issues.append(f"connector {r['connector']} failed: {'; '.join(r['errors'])[:200]}")

    cls = _step("classify", lambda: run_classify.run(day), steps, issues)
    if cls:
        if cls.get("quarantined"):
            issues.append(f"classify: {cls['quarantined']} item(s) quarantined ({cls.get('quarantine_folder')})")
        if cls["items"] and not cls["labelled"]:
            issues.append("classify: no item could be labelled (Claude sign-in or service problem?)")

    dig = _step("digest", lambda: run_digest.run(day), steps, issues)
    if dig and dig.get("status") != "ok":
        issues.append(f"digest status {dig.get('status')}")

    dash = None
    if dig:
        dash = _step("dashboard", lambda: run_dashboard.run(day), steps, issues)
        if dash and dash.get("status") != "ok":
            issues.append(f"dashboard status {dash.get('status')}")

    sent = None
    if dig and dig.get("status") in ("ok", "partial"):
        sent = _step("email", lambda: send_email.send_digest(day, dry_run=dry_run, resend=resend), steps, issues)
        if sent and sent.get("status") == "failed":
            issues.append(f"email failed: {sent.get('error')}")
    else:
        issues.append("no digest to send today")

    if issues:
        log.warning("issues: %s", issues)
        alert = send_email.send_alert(day, issues, dry_run=dry_run)
        steps["alert"] = {"status": alert.get("status"), "summary": alert}

    status = "failed" if not dig else ("warnings" if issues else "ok")
    conn.execute("UPDATE daily_runs SET finished_at = ?, status = ?, steps = ?, issues = ? WHERE id = ?",
                 (utc_now_z(), status, json.dumps(steps, default=str), json.dumps(issues), run_id))
    conn.commit()
    conn.close()
    log.info("daily run %s: %s (%d issue(s)); log %s", day, status, len(issues), log_path)
    return {"status": status, "issues": issues, "email": sent, "log": str(log_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the whole day: fetch, classify, digest, dashboard, email")
    parser.add_argument("--day", default="today", help="YYYY-MM-DD (default: today)")
    parser.add_argument("--dry-run", action="store_true", help="Save the email to data/outbox/ instead of sending it")
    parser.add_argument("--resend", action="store_true", help="Send the digest again even if already sent")
    args = parser.parse_args()
    day = date.today() if args.day == "today" else date.fromisoformat(args.day)
    r = run(day, dry_run=args.dry_run, resend=args.resend)
    print(f"\nDaily run {day}: {r['status']}")
    for i in r["issues"]:
        print(f"  ! {i}")
    if r["email"]:
        print(f"  email: {r['email'].get('status')} {r['email'].get('subject', '')}")
    print(f"  log: {r['log']}")
    sys.exit(1 if r["status"] == "failed" else 0)


if __name__ == "__main__":
    main()
