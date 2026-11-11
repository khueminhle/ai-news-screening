"""Live dashboard site (Cloudflare Pages): latest dashboard at /, last N days under /<day>/, nothing else from data/."""
import pytest

from pipeline.build_site import build


def _dash(root, day, text):
    d = root / day
    d.mkdir(parents=True)
    (d / "dashboard.html").write_text(text, encoding="utf-8")
    (d / "dashboard.json").write_text("{}")          # must NOT be published


def test_site_has_latest_history_and_nothing_else(tmp_path):
    dash, site = tmp_path / "dash", tmp_path / "site"
    for i, day in enumerate(["2026-11-08", "2026-11-09", "2026-11-10"]):
        _dash(dash, day, f"dashboard {i}")
    r = build(dash, site, keep=2)
    assert r == {"latest": "2026-11-10", "days": 2}
    assert (site / "index.html").read_text(encoding="utf-8") == "dashboard 2"
    assert (site / "2026-11-09" / "index.html").exists() and not (site / "2026-11-08").exists()
    assert "noindex" in (site / "archive.html").read_text(encoding="utf-8")
    assert (site / "robots.txt").read_text() == "User-agent: *\nDisallow: /\n"
    published = {p.name for p in site.rglob("*") if p.is_file()}
    assert "dashboard.json" not in published


def test_rebuild_replaces_old_site(tmp_path):
    dash, site = tmp_path / "dash", tmp_path / "site"
    site.mkdir()
    (site / "old.txt").write_text("stale")
    _dash(dash, "2026-11-10", "x")
    build(dash, site)
    assert not (site / "old.txt").exists()


def test_no_dashboards_stops_clearly(tmp_path):
    with pytest.raises(SystemExit, match="No dashboards"):
        build(tmp_path / "missing", tmp_path / "site")
