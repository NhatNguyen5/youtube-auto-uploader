import csv
import io
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlparse

import pytest

from youtube_auto_uploader import google_auth
from youtube_auto_uploader.cli import main
from youtube_auto_uploader.drive import STATS_EXPORTER, Drive
from youtube_auto_uploader.models import Settings
from youtube_auto_uploader.stats import (
    PACIFIC,
    aggregate_window,
    analytics_rows,
    apply_reach_report,
    canonical_week,
    export_stats,
    publication_time,
    reach_summary,
    report_is_newer,
    stats_targets,
    week_key,
    window_days,
)

MOMENT = datetime(2026, 6, 10, 18, tzinfo=UTC)


def json_response(data, content=None):
    result = Mock(status_code=200, headers={})
    result.json.return_value = data
    result.content = content if content is not None else json.dumps(data).encode()
    return result


def test_week_key_uses_pacific_date_not_utc():
    moment = datetime(2026, 6, 8, 6, tzinfo=UTC)
    pacific = moment.astimezone(PACIFIC)
    assert pacific.date().isoformat() == "2026-06-07"
    assert week_key(moment) == "2026-W23"
    assert moment.isocalendar().week == 24


def test_window_days_cover_72_absolute_hours():
    midnight = datetime(2026, 6, 1, tzinfo=PACIFIC)
    slots = window_days(midnight)
    assert [slot["date"] for slot in slots] == [
        "2026-06-01",
        "2026-06-02",
        "2026-06-03",
    ]
    assert [slot["fraction"] for slot in slots] == [1, 1, 1]

    midday = datetime(2026, 6, 1, 15, tzinfo=PACIFIC)
    slots = {slot["date"]: slot["fraction"] for slot in window_days(midday)}
    assert list(slots) == ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04"]
    assert slots["2026-06-01"] == 1
    assert slots["2026-06-04"] == pytest.approx(0.625)

    # 2026-03-08 is 23 absolute hours. The window ends at 04:00 PDT, 3 hours
    # after Pacific midnight, so the trailing fraction is 3/23 rather than 4/24.
    spring = datetime(2026, 3, 5, 3, tzinfo=PACIFIC)
    slots = {slot["date"]: slot["fraction"] for slot in window_days(spring)}
    assert slots["2026-03-05"] == 1
    assert slots["2026-03-08"] == pytest.approx(3 / 23, abs=1e-5)

    fall = datetime(2026, 10, 29, 1, tzinfo=PACIFIC)
    slots = {slot["date"]: slot["fraction"] for slot in window_days(fall)}
    assert slots["2026-10-29"] == 1
    assert slots["2026-11-01"] == pytest.approx(1 / 25, abs=1e-5)


def test_publication_time_uses_schedule_only_while_private():
    private = {
        "status": {"privacyStatus": "private", "publishAt": "2026-06-02T00:00:00Z"},
        "snippet": {"publishedAt": "2026-06-01T00:00:00Z"},
    }
    public = {
        "status": {"privacyStatus": "public", "publishAt": "2026-06-02T00:00:00Z"},
        "snippet": {"publishedAt": "2026-06-03T00:00:00Z"},
    }
    assert publication_time(private) == datetime(2026, 6, 2, tzinfo=UTC)
    assert publication_time(public) == datetime(2026, 6, 3, tzinfo=UTC)


def test_aggregate_window_prorates_trailing_day_and_skips_unready_days():
    window = window_days(datetime(2026, 6, 1, 15, tzinfo=PACIFIC))
    rows = {
        "2026-06-01": {"views": 100, "averageViewDuration": 10, "subscribersGained": 2},
        "2026-06-02": {"views": 50, "averageViewDuration": 20, "subscribersGained": 1},
        "2026-06-04": {"views": 80, "averageViewDuration": 5, "subscribersGained": 4},
    }
    full = aggregate_window(window, rows, "2026-06-10")
    assert full["complete"] is True
    assert full["views"] == pytest.approx(200)
    assert full["subscribers_gained"] == pytest.approx(5.5)
    assert full["average_view_duration"] == pytest.approx(11.25)
    partial = aggregate_window(window, rows, "2026-06-02")
    assert partial["complete"] is False
    assert partial["views"] == pytest.approx(150)
    assert partial["days"][-1]["included"] is False
    assert aggregate_window(window, rows, None)["views"] is None


def test_reach_ingest_replaces_a_day_instead_of_adding():
    state = {"schema_version": 1, "reach_job_id": "job", "days": {}}
    report = {
        "id": "r1",
        "startTime": "2026-06-01T07:00:00Z",
        "createTime": "2026-06-03T00:00:00Z",
    }
    text = (
        "date,channel_id,video_id,video_thumbnail_impressions,"
        "video_thumbnail_impressions_ctr\n"
        "2026-06-01,UCchannel,video1,10,0.2\n"
        "2026-06-01,UCchannel,video1,99,0.2\n"
    )
    apply_reach_report(state, report, text)
    apply_reach_report(state, report, text)
    assert state["days"]["2026-06-01"]["videos"]["video1"]["impressions"] == 99
    newer = {**report, "id": "r2", "createTime": "2026-06-04T00:00:00Z"}
    assert report_is_newer(state["days"]["2026-06-01"], newer)
    apply_reach_report(state, newer, text.replace(",99,", ",15,"))
    assert state["days"]["2026-06-01"]["videos"]["video1"]["impressions"] == 15
    assert state["days"]["2026-06-01"]["report_id"] == "r2"
    assert not report_is_newer(state["days"]["2026-06-01"], newer)
    with pytest.raises(ValueError, match="does not match"):
        apply_reach_report(
            state,
            newer,
            text.replace("2026-06-01", "2026-06-02"),
        )


def test_reach_summary_weights_ctr_and_counts_missing_days():
    state = {"days": {}}

    def add(day, impressions, ctr):
        apply_reach_report(
            state,
            {
                "id": "r-" + day,
                "startTime": f"{day}T07:00:00Z",
                "createTime": "2026-06-10T00:00:00Z",
            },
            "date,channel_id,video_id,video_thumbnail_impressions,"
            "video_thumbnail_impressions_ctr\n"
            f"{day},UCchannel,video1,{impressions},{ctr}\n",
        )

    add("2026-06-01", 100, 0.1)
    add("2026-06-03", 10, 0)
    published = datetime(2026, 6, 1, 15, tzinfo=PACIFIC)
    summary = reach_summary(state["days"], "video1", "UCchannel", published)
    assert summary["impressions_lifetime"] == pytest.approx(110)
    assert summary["impressions_ctr_lifetime"] == pytest.approx(10 / 110)
    assert summary["impressions_missing_days"] == 1
    assert summary["first_72h_reach_complete"] is False
    other = reach_summary(state["days"], "video1", "UCother", published)
    assert other["impressions_lifetime"] is None


def test_settings_stats_targets_and_example_config():
    example = Settings.model_validate(
        json.loads(Path("examples/config.example.json").read_text(encoding="utf-8"))
    )
    assert example.stats_state_path == "data/stats-reach.json"
    assert stats_targets(example)[0].youtube_channel_id == example.youtube_channel_id
    single = Settings(youtube_channel_id="channel", stats_drive_folder_id="folderone")
    assert stats_targets(single)[0].drive_folder_id == "folderone"
    listed = Settings(
        youtube_channel_id="channel",
        stats_drive_folder_id="folderone",
        stats_channels=[
            {
                "youtube_channel_id": "UCkids",
                "drive_folder_id": "folderkids",
                "label": "Kids",
            }
        ],
    )
    assert [target.youtube_channel_id for target in stats_targets(listed)] == ["UCkids"]
    with pytest.raises(ValueError):
        Settings(youtube_channel_id="channel", stats_drive_folder_id="bad id")
    with pytest.raises(ValueError):
        Settings(youtube_channel_id="channel", stats_state_path="../secrets/token.json")
    with pytest.raises(ValueError, match="2026-W44"):
        canonical_week("next-week", MOMENT)


def test_missing_scopes_lists_analytics_and_drive_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", "token.json")
    (tmp_path / "token.json").write_text(
        json.dumps(
            {
                "scopes": [
                    "https://www.googleapis.com/auth/drive.readonly",
                    "https://www.googleapis.com/auth/youtube.force-ssl",
                ]
            }
        ),
        encoding="utf-8",
    )
    missing = google_auth.missing_scopes(tmp_path)
    assert "https://www.googleapis.com/auth/yt-analytics.readonly" in missing
    assert "https://www.googleapis.com/auth/drive.file" in missing
    (tmp_path / "token.json").write_text(
        json.dumps({"scopes": list(google_auth.SCOPES)}), encoding="utf-8"
    )
    assert google_auth.missing_scopes(tmp_path) == []


def test_analytics_pages_follow_start_index():
    class PageHttp:
        def get(self, url, params=None, timeout=None):
            assert "youtubeanalytics.googleapis.com" in url
            start = int(params["startIndex"])
            if start == 1:
                rows = [[str(index)] for index in range(200)]
            else:
                assert start == 201
                rows = [["tail"]]
            return json_response({"columnHeaders": [{"name": "day"}], "rows": rows})

    rows = analytics_rows(
        PageHttp(),
        "UCchannel",
        startDate="2008-01-01",
        endDate="2026-06-10",
        metrics="views",
        dimensions="day",
    )
    assert len(rows) == 201
    assert rows[-1]["day"] == "tail"


def multipart_parts(data, headers):
    boundary = headers["Content-Type"].split("boundary=", 1)[1]
    parts = {}
    for part in data.split(("--" + boundary).encode()):
        head, _, body = part.partition(b"\r\n\r\n")
        if b"application/json" in head:
            start, end = body.find(b"{"), body.rfind(b"}")
            parts["meta"] = json.loads(body[start : end + 1])
        elif b"text/csv" in head:
            parts["csv"] = body.strip(b"\r\n")
    return parts


class DriveHttp:
    def __init__(self, files):
        self.files = {item["id"]: item for item in files}
        self.created = []
        self.updated = []

    def get(self, url, params=None, timeout=None):
        name = re.search(r"name = '([^']+)'", params["q"]).group(1)
        matches = [item for item in self.files.values() if item["name"] == name]
        return json_response(
            {
                "files": [
                    {
                        "id": item["id"],
                        "name": item["name"],
                        "appProperties": item.get("appProperties") or {},
                    }
                    for item in matches
                ]
            }
        )

    def post(self, url, params=None, data=None, headers=None, timeout=None):
        parts = multipart_parts(data, headers)
        file_id = "created"
        self.files[file_id] = {
            "id": file_id,
            "name": parts["meta"]["name"],
            "appProperties": parts["meta"]["appProperties"],
            "csv": parts["csv"],
        }
        self.created.append(file_id)
        return json_response({"id": file_id})

    def patch(self, url, params=None, data=None, headers=None, timeout=None):
        file_id = url.rstrip("/").rsplit("/", 1)[-1]
        parts = multipart_parts(data, headers)
        self.files[file_id]["csv"] = parts["csv"]
        self.files[file_id]["appProperties"] = parts["meta"]["appProperties"]
        self.updated.append(file_id)
        return json_response({"id": file_id})


def test_drive_upsert_overwrites_the_same_week_file():
    http = DriveHttp([])
    drive = Drive(http)
    name = "youtube-stats-UCchannel-2026-W24.csv"
    first = drive.upsert_stats_file("statsfolder", name, b"week-one")
    second = drive.upsert_stats_file("statsfolder", name, b"week-two")
    assert first == second == ["created"]
    assert http.created == ["created"]
    assert http.updated == ["created"]
    assert http.files["created"]["csv"] == b"week-two"
    assert http.files["created"]["appProperties"]["exporter"] == STATS_EXPORTER


def test_drive_upsert_refuses_a_foreign_file_with_the_same_name():
    name = "youtube-stats-UCchannel-2026-W24.csv"
    http = DriveHttp(
        [{"id": "user-file", "name": name, "appProperties": {}, "csv": b"keep"}]
    )
    with pytest.raises(ValueError, match="not created"):
        Drive(http).upsert_stats_file("statsfolder", name, b"new")
    assert http.created == []
    assert http.files["user-file"]["csv"] == b"keep"


REACH_DAYS = (
    ("2026-06-01", 100, 0.1),
    ("2026-06-02", 40, 0.5),
    ("2026-06-03", 10, 0),
    ("2026-06-04", 8, 0.25),
)


class Api:
    def __init__(self):
        self.jobs = []
        self.created_jobs = 0
        self.downloads = []
        self.retention = []
        self.files = {}
        self.created = []
        self.updated = []
        self.reports = []
        self.csv_by_id = {}
        for day, impressions, ctr in REACH_DAYS:
            report_id = "report-" + day
            self.reports.append(
                {
                    "id": report_id,
                    "startTime": f"{day}T07:00:00Z",
                    "createTime": "2026-06-06T00:00:00Z",
                    "downloadUrl": (
                        "https://youtubereporting.googleapis.com/v1/media/" + report_id
                    ),
                }
            )
            self.csv_by_id[report_id] = (
                "date,channel_id,video_id,video_thumbnail_impressions,"
                "video_thumbnail_impressions_ctr\n"
                f"{day},UCchannel,video1,{impressions},{ctr}\n"
            )

    def get(self, url, params=None, timeout=None, headers=None):
        path = urlparse(url).path
        if path.endswith("/channels") and params.get("mine") == "true":
            return json_response({"items": [{"id": "UCchannel"}]})
        if path.endswith("/channels"):
            return json_response(
                {
                    "items": [
                        {
                            "id": "UCchannel",
                            "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}},
                        }
                    ]
                }
            )
        if path.endswith("/playlistItems"):
            return json_response(
                {
                    "items": [
                        {"contentDetails": {"videoId": "video1"}},
                        {"contentDetails": {"videoId": "video1"}},
                    ]
                }
            )
        if path.endswith("/videos"):
            assert params["id"] == "video1"
            return json_response(
                {
                    "items": [
                        {
                            "id": "video1",
                            "snippet": {
                                "title": "Night story",
                                "publishedAt": "2026-06-01T22:00:00Z",
                            },
                            "status": {"privacyStatus": "public"},
                        }
                    ]
                }
            )
        if "youtubeanalytics.googleapis.com" in url:
            return json_response(self.analytics(params))
        if path == "/v1/jobs":
            return json_response({"jobs": self.jobs})
        if path.endswith("/reports"):
            return json_response({"reports": self.reports})
        if path.startswith("/v1/media/"):
            report_id = path.rsplit("/", 1)[-1]
            self.downloads.append(report_id)
            return json_response({}, content=self.csv_by_id[report_id].encode())
        if path == "/drive/v3/files":
            name = re.search(r"name = '([^']+)'", params["q"]).group(1)
            matches = [item for item in self.files.values() if item["name"] == name]
            return json_response(
                {
                    "files": [
                        {
                            "id": item["id"],
                            "name": item["name"],
                            "appProperties": item["appProperties"],
                        }
                        for item in matches
                    ]
                }
            )
        raise AssertionError(url)

    def analytics(self, params):
        dimensions = params.get("dimensions")
        if dimensions == "day":
            if int(params.get("startIndex", 1)) > 1:
                return {
                    "columnHeaders": [{"name": "day"}],
                    "rows": [],
                }
            return {
                "columnHeaders": [
                    {"name": "day"},
                    {"name": "views"},
                    {"name": "averageViewDuration"},
                    {"name": "subscribersGained"},
                ],
                "rows": [["2026-06-10", 1, 1, 0]],
            }
        if dimensions == "video":
            return {
                "columnHeaders": [
                    {"name": "video"},
                    {"name": "views"},
                    {"name": "averageViewDuration"},
                    {"name": "subscribersGained"},
                ],
                "rows": [["video1", 1000, 30, 7]],
            }
        if dimensions == "day,video":
            return {
                "columnHeaders": [
                    {"name": "day"},
                    {"name": "video"},
                    {"name": "views"},
                    {"name": "averageViewDuration"},
                    {"name": "subscribersGained"},
                ],
                "rows": [
                    ["2026-06-01", "video1", 100, 10, 2],
                    ["2026-06-02", "video1", 50, 20, 1],
                    ["2026-06-04", "video1", 80, 5, 4],
                ],
            }
        if dimensions == "elapsedVideoTimeRatio":
            self.retention.append((params["startDate"], params["endDate"]))
            return {
                "columnHeaders": [
                    {"name": "elapsedVideoTimeRatio"},
                    {"name": "audienceWatchRatio"},
                    {"name": "relativeRetentionPerformance"},
                ],
                "rows": [[0.5, 0.5, 0.6], [0.25, 0.8, 0.4]],
            }
        raise AssertionError(params)

    def post(self, url, params=None, data=None, headers=None, json=None, timeout=None):
        path = urlparse(url).path
        if path == "/v1/jobs":
            self.created_jobs += 1
            job = {
                "id": "job1",
                "reportTypeId": json["reportTypeId"],
                "createTime": "2026-06-01T00:00:00Z",
            }
            self.jobs.append(job)
            return json_response(job)
        if path == "/upload/drive/v3/files":
            parts = multipart_parts(data, headers)
            file_id = "stats-file"
            self.files[file_id] = {
                "id": file_id,
                "name": parts["meta"]["name"],
                "appProperties": parts["meta"]["appProperties"],
                "csv": parts["csv"],
            }
            self.created.append(file_id)
            return json_response({"id": file_id})
        raise AssertionError(url)

    def patch(self, url, params=None, data=None, headers=None, timeout=None):
        file_id = url.rstrip("/").rsplit("/", 1)[-1]
        parts = multipart_parts(data, headers)
        self.files[file_id]["csv"] = parts["csv"]
        self.updated.append(file_id)
        return json_response({"id": file_id})


def test_export_stats_rerun_keeps_one_file_and_one_row(tmp_path):
    http = Api()
    settings = Settings(
        youtube_channel_id="UCchannel",
        stats_drive_folder_id="statsfolder",
        stats_state_path="reach.json",
    )
    first = export_stats(settings, tmp_path, http, clock=lambda: MOMENT)
    downloads = len(http.downloads)
    second = export_stats(settings, tmp_path, http, clock=lambda: MOMENT)
    assert http.created_jobs == 1
    assert http.created == ["stats-file"]
    assert http.updated == ["stats-file"]
    assert len(http.downloads) == downloads == 4
    assert first[0]["file_ids"] == second[0]["file_ids"] == ["stats-file"]
    assert first[0]["videos"] == second[0]["videos"] == 1
    stored = json.loads((tmp_path / "reach.json").read_text(encoding="utf-8"))
    assert stored["days"]["2026-06-01"]["videos"]["video1"]["impressions"] == 100
    assert len(stored["days"]) == 4
    parsed = list(csv.DictReader(io.StringIO(http.files["stats-file"]["csv"].decode())))
    assert len(parsed) == 1
    row = parsed[0]
    assert row["video_id"] == "video1"
    assert row["week"] == "2026-W24"
    assert row["views_lifetime"] == "1000"
    assert row["average_view_duration_seconds_lifetime"] == "30"
    assert row["subscribers_gained_lifetime"] == "7"
    assert row["views_first_72h"] == "200"
    assert row["average_view_duration_seconds_first_72h"] == "11.25"
    assert row["subscribers_gained_first_72h"] == "5.5"
    assert row["first_72h_analytics_complete"] == "true"
    assert row["impressions_lifetime"] == "158"
    assert row["impressions_ctr_lifetime"] == "0.202532"
    assert row["impressions_first_72h"] == "155"
    assert row["impressions_ctr_first_72h"] == "0.201613"
    assert row["impressions_missing_days"] == "0"
    assert row["first_72h_reach_complete"] == "true"
    assert "channel_reach_basic_a1" in row["reach_api"]
    assert "reports.query" in row["activity_api"]
    curve = json.loads(row["audience_retention_first_72h"])
    assert [point["elapsed_video_time_ratio"] for point in curve] == [0.25, 0.5]
    assert row["audience_retention_first_72h_end"] == "2026-06-04"
    assert ("2026-06-01", "2026-06-10") in http.retention
    assert ("2026-06-01", "2026-06-04") in http.retention
    # A second pass of the same report must not be appended to the CSV.
    assert http.files["stats-file"]["csv"].decode().count("video1") == 1


def test_export_stats_cli_requires_configuration(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"schema_version": 1, "youtube_channel_id": "channel"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["youtube-auto-uploader", "--config", str(config), "export-stats"],
    )
    assert main() == 1
    assert "stats_drive_folder_id" in capsys.readouterr().out
