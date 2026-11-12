import subprocess
import sys
from datetime import UTC, date, datetime

from youtube_auto_uploader.stats import day_bounds, pacific_date, week_key


def test_package_imports_when_zoneinfo_is_missing():
    # Fresh interpreter so a reload cannot replace exception classes this
    # process already imported. ZoneInfo fails for every key before import.
    script = """
import importlib
import pkgutil
import zoneinfo

def explode(key):
    raise zoneinfo.ZoneInfoNotFoundError(key)

zoneinfo.ZoneInfo = explode

import youtube_auto_uploader

names = [youtube_auto_uploader.__name__]
names.extend(
    module.name
    for module in pkgutil.walk_packages(
        youtube_auto_uploader.__path__,
        youtube_auto_uploader.__name__ + ".",
    )
)
required = {
    "youtube_auto_uploader",
    "youtube_auto_uploader.cli",
    "youtube_auto_uploader.desktop",
    "youtube_auto_uploader.gui",
    "youtube_auto_uploader.runner",
    "youtube_auto_uploader.stats",
}
missing = required.difference(names)
if missing:
    raise SystemExit("missing modules: " + ", ".join(sorted(missing)))
for name in names:
    importlib.reload(importlib.import_module(name))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_stats_pacific_fallback_for_pdt_and_pst(monkeypatch):
    import zoneinfo

    import youtube_auto_uploader.tz as pacific

    calls = []

    def explode(key):
        calls.append(key)
        raise zoneinfo.ZoneInfoNotFoundError(key)

    monkeypatch.setattr(pacific, "ZoneInfo", explode)
    # 07:30 UTC is 00:30 PDT the same day, and 23:30 PST the day before.
    pdt = datetime(2026, 7, 15, 7, 30, tzinfo=UTC)
    pst = datetime(2026, 1, 15, 7, 30, tzinfo=UTC)
    assert pacific_date(pdt) == date(2026, 7, 15)
    assert week_key(pdt) == "2026-W29"
    assert pacific_date(pst) == date(2026, 1, 14)
    assert week_key(pst) == "2026-W03"
    july_start, july_end = day_bounds(date(2026, 7, 15))
    january_start, january_end = day_bounds(date(2026, 1, 15))
    assert july_start.astimezone(UTC) == datetime(2026, 7, 15, 7, tzinfo=UTC)
    assert january_start.astimezone(UTC) == datetime(2026, 1, 15, 8, tzinfo=UTC)
    assert july_end.astimezone(UTC) == datetime(2026, 7, 16, 7, tzinfo=UTC)
    assert january_end.astimezone(UTC) == datetime(2026, 1, 16, 8, tzinfo=UTC)
    assert calls
