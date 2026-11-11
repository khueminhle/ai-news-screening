"""Build the GitHub Pages site from the dashboards in data/dashboards/.

    python -m pipeline.build_site            # -> site/index.html (latest) + site/<day>/index.html + site/archive.html

The site is PUBLIC (anyone with the link). It contains only the dashboards; nothing else from data/.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.base import PROJECT_ROOT

DASH_DIR = PROJECT_ROOT / "data" / "dashboards"
SITE = PROJECT_ROOT / "site"
KEEP_DAYS = 14                      # older dashboards stay in data/ but are not published


def build(dash_dir: Path = DASH_DIR, site: Path = SITE, keep: int = KEEP_DAYS) -> dict:
    days = sorted(p.name for p in dash_dir.iterdir() if (p / "dashboard.html").exists()) if dash_dir.exists() else []
    if not days:
        raise SystemExit("No dashboards to publish.")
    if site.exists():
        shutil.rmtree(site)
    site.mkdir(parents=True)
    shown = days[-keep:]
    for d in shown:
        (site / d).mkdir()
        shutil.copy2(dash_dir / d / "dashboard.html", site / d / "index.html")
    shutil.copy2(dash_dir / shown[-1] / "dashboard.html", site / "index.html")
    links = "\n".join(f'<li><a href="{d}/">{d}</a></li>' for d in reversed(shown))
    (site / "archive.html").write_text(
        '<!doctype html><html><head><meta charset="utf-8"><meta name="robots" content="noindex, nofollow">'
        '<title>Dashboard archive</title><style>body{background:#000;color:#ffa028;font:15px Arial,sans-serif;'
        'padding:24px}a{color:#5ec8ff}h1{color:#fff;font-size:18px}</style></head><body>'
        f'<h1>Daily dashboards (last {len(shown)} days) · SIMULATION, NOT INVESTMENT ADVICE</h1>'
        f'<p><a href="./">Latest</a></p><ul>{links}</ul></body></html>', encoding="utf-8")
    (site / ".nojekyll").write_text("")
    (site / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
    return {"latest": shown[-1], "days": len(shown)}


if __name__ == "__main__":
    print(build())
