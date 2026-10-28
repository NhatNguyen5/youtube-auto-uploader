import csv
import io
import json
import math
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import portalocker
from portalocker.exceptions import LockException

from .drive import Drive, checked
from .models import StatsChannel, require_drive_id
from .youtube import ROOT as YOUTUBE_DATA
from .youtube import YouTube

PACIFIC = ZoneInfo("America/Los_Angeles")
ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"
# Append later metric groups here. The writer emits COLUMNS in order.
ACTIVITY_API = (
    "YouTube Analytics API reports.query: views, averageViewDuration, "
    "subscribersGained (watch-page subscriptions only on a per-video query), "
    "audienceWatchRatio and relativeRetentionPerformance by elapsedVideoTimeRatio"
)
FIRST_72H_METHOD = (
    "Days are America/Los_Angeles midnight to midnight, including 23- or 25-hour "
    "daylight-saving days. The window is [published_at, published_at + 72 hours). "
    "views, averageViewDuration, and subscribersGained use Analytics day rows: the "
    "publish day counts in full because playback cannot precede publication; later "
    "days entirely inside the window count in full; the trailing day is multiplied "
    "by the share of that Pacific day inside the window. Days after analytics_last_day "
    "are left out instead of stored as zero. The 72-hour average view duration is the "
    "view-weighted averageViewDuration of the included days. Audience retention is the "
    "Analytics curve for those overlapping Pacific dates and is not scaled down to the "
    "partial trailing day. Analytics omits the most recent days, so a video's "
    "72-hour cells stay incomplete until a later run."
)
COLUMNS = [
    "week",
    "channel_id",
    "channel_label",
    "video_id",
    "title",
    "published_at",
    "privacy_status",
    "analytics_last_day",
    "views_lifetime",
    "average_view_duration_seconds_lifetime",
    "subscribers_gained_lifetime",
    "audience_retention_lifetime",
    "views_first_72h",
    "average_view_duration_seconds_first_72h",
    "subscribers_gained_first_72h",
    "audience_retention_first_72h",
    "audience_retention_first_72h_end",
    "first_72h_analytics_complete",
    "first_72h_days",
    "activity_api",
    "first_72h_method",
    "exported_at",
]


def parse_timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp requires a timezone")
    return parsed


def week_key(moment):
    iso = moment.astimezone(PACIFIC).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def canonical_week(value, moment):
    if not value:
        return week_key(moment)
    if not re.fullmatch(r"\d{4}-W\d{2}", value):
        raise ValueError("Week must look like 2026-W44")
    if not 1 <= int(value[-2:]) <= 53:
        raise ValueError("Week must look like 2026-W44")
    return value


def publication_time(video):
    """Public time for a public video; scheduled public time while it is still private."""
    status = video.get("status") or {}
    snippet = video["snippet"]
    if status.get("privacyStatus") == "private" and status.get("publishAt"):
        return parse_timestamp(status["publishAt"])
    return parse_timestamp(snippet["publishedAt"])


def pacific_date(moment):
    return moment.astimezone(PACIFIC).date()


def day_bounds(day):
    # Local midnights, not 24-hour steps, so a daylight-saving day is 23 or 25 hours.
    nxt = day.fromordinal(day.toordinal() + 1)
    start = datetime(day.year, day.month, day.day, tzinfo=PACIFIC)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=PACIFIC)
    return start, end


def absolute_seconds(start, end):
    # Aware datetimes that share a tzinfo subtract wall time. Convert first so a
    # 23- or 25-hour Pacific day keeps its real length.
    return (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds()


def window_days(published_at):
    start = published_at.astimezone(PACIFIC)
    end = (published_at.astimezone(UTC) + timedelta(hours=72)).astimezone(PACIFIC)
    day = start.date()
    last = end.date()
    slots = []
    while day <= last:
        day_start, day_end = day_bounds(day)
        overlap_start = max(day_start, start)
        overlap_end = min(day_end, end)
        if absolute_seconds(overlap_start, overlap_end) > 0:
            fraction = absolute_seconds(overlap_start, overlap_end) / absolute_seconds(
                day_start, day_end
            )
            if day == start.date():
                fraction = 1.0
            slots.append({"date": day.isoformat(), "fraction": round(fraction, 6)})
        day = day.fromordinal(day.toordinal() + 1)
    return slots


def stats_targets(settings):
    if settings.stats_channels:
        return list(settings.stats_channels)
    if settings.stats_drive_folder_id:
        return [
            StatsChannel(
                youtube_channel_id=settings.youtube_channel_id,
                drive_folder_id=settings.stats_drive_folder_id,
            )
        ]
    return []


def aggregate_window(window, rows_by_date, available_through):
    annotated = []
    usable = []
    missing = False
    for slot in window:
        included = available_through is not None and slot["date"] <= available_through
        annotated.append({**slot, "included": included})
        if not included:
            missing = True
            continue
        row = rows_by_date.get(slot["date"]) or {
            "views": 0,
            "averageViewDuration": 0,
            "subscribersGained": 0,
        }
        usable.append((slot["fraction"], row))
    if not usable:
        return {
            "views": None,
            "average_view_duration": None,
            "subscribers_gained": None,
            "complete": False,
            "days": annotated,
        }
    views = sum(float(row["views"]) * fraction for fraction, row in usable)
    subscribers = sum(
        float(row["subscribersGained"]) * fraction for fraction, row in usable
    )
    watched = sum(
        float(row["averageViewDuration"]) * float(row["views"]) * fraction
        for fraction, row in usable
    )
    return {
        "views": views,
        "average_view_duration": (watched / views) if views else 0,
        "subscribers_gained": subscribers,
        "complete": not missing,
        "days": annotated,
    }


def analytics_rows(http, channel_id, **params):
    query = {
        "ids": f"channel=={require_drive_id(channel_id, 'YouTube channel ID')}",
        "startIndex": 1,
        "maxResults": 200,
    }
    query.update(params)
    rows = []
    for _ in range(100):
        data = checked(http.get(ANALYTICS, params=query, timeout=60)).json()
        headers = [item["name"] for item in data.get("columnHeaders") or []]
        batch = data.get("rows") or []
        if batch and not headers:
            raise ValueError("Analytics response missing column headers")
        for raw in batch:
            if len(raw) != len(headers):
                raise ValueError("Analytics row does not match column headers")
            rows.append(dict(zip(headers, raw)))
        if len(batch) < int(query["maxResults"]):
            return rows
        query["startIndex"] = int(query["startIndex"]) + int(query["maxResults"])
    raise ValueError("Analytics report exceeded page limit")


def video_filter(video_ids):
    safe = [require_drive_id(video_id, "YouTube video ID") for video_id in video_ids]
    return "video==" + ",".join(safe)


def chunks(items, size):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def uploaded_videos(http, channel_id):
    require_drive_id(channel_id, "YouTube channel ID")
    channel = checked(
        http.get(
            f"{YOUTUBE_DATA}/channels",
            params={"part": "contentDetails", "id": channel_id},
            timeout=60,
        )
    ).json()
    items = channel.get("items") or []
    if not items:
        raise ValueError("YouTube channel was not found for this login")
    playlist = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    params = {"part": "contentDetails", "playlistId": playlist, "maxResults": 50}
    ids = []
    while True:
        data = checked(
            http.get(f"{YOUTUBE_DATA}/playlistItems", params=params, timeout=60)
        ).json()
        for item in data.get("items") or []:
            video_id = item["contentDetails"]["videoId"]
            require_drive_id(video_id, "YouTube video ID")
            if video_id not in ids:
                ids.append(video_id)
        token = data.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token
    videos = []
    for batch in chunks(ids, 50):
        data = checked(
            http.get(
                f"{YOUTUBE_DATA}/videos",
                params={
                    "part": "snippet,status",
                    "id": ",".join(batch),
                    "maxResults": 50,
                },
                timeout=60,
            )
        ).json()
        found = {item["id"]: item for item in data.get("items") or []}
        videos.extend(found[video_id] for video_id in batch if video_id in found)
    return videos


def analytics_last_day(http, channel_id, today):
    # A day with no channel activity is omitted, so this is the newest returned
    # day, not a guarantee that later quiet days are unpublished.
    rows = analytics_rows(
        http,
        channel_id,
        startDate=(today - timedelta(days=28)).isoformat(),
        endDate=today.isoformat(),
        metrics="views,averageViewDuration,subscribersGained",
        dimensions="day",
    )
    days = [row["day"] for row in rows if row.get("day")]
    return max(days) if days else None


def retention_curve(http, channel_id, video_id, start, end):
    rows = analytics_rows(
        http,
        channel_id,
        startDate=start,
        endDate=end,
        metrics="audienceWatchRatio,relativeRetentionPerformance",
        dimensions="elapsedVideoTimeRatio",
        filters=video_filter([video_id]),
        maxResults=200,
    )
    points = [
        {
            "elapsed_video_time_ratio": float(row["elapsedVideoTimeRatio"]),
            "audience_watch_ratio": float(row["audienceWatchRatio"]),
            "relative_retention_performance": float(
                row["relativeRetentionPerformance"]
            ),
        }
        for row in rows
    ]
    points.sort(key=lambda point: point["elapsed_video_time_ratio"])
    return points


def activity_for_channel(http, channel_id, videos, available_through, today):
    # Basic user activity is dimensions omitted and filters=video== one id.
    # The day report is dimensions=day with that same single-video filter.
    # dimensions=video is the top-videos report (sort required, no video filter).
    # dimensions=day,video is not a listed channel report. One call per video.
    lifetime = {}
    daily = {}
    end = available_through or today.isoformat()
    metrics = "views,averageViewDuration,subscribersGained"
    for video in videos:
        video_id = video["id"]
        published = publication_time(video)
        publish_day = pacific_date(published).isoformat()
        if publish_day <= end:
            rows = analytics_rows(
                http,
                channel_id,
                startDate=publish_day,
                endDate=end,
                metrics=metrics,
                filters=video_filter([video_id]),
            )
            if rows:
                lifetime[video_id] = rows[0]
        window = window_days(published)
        if (
            not window
            or available_through is None
            or window[0]["date"] > available_through
        ):
            continue
        for row in analytics_rows(
            http,
            channel_id,
            startDate=window[0]["date"],
            endDate=min(window[-1]["date"], available_through),
            metrics=metrics,
            dimensions="day",
            filters=video_filter([video_id]),
        ):
            daily[(video_id, row["day"])] = row
    return lifetime, daily


def cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, float):
        if not math.isfinite(value):
            return ""
        text = f"{value:.6f}".rstrip("0").rstrip(".") or "0"
    else:
        text = str(value)
    # Spreadsheets treat a leading =, +, -, or @ as a formula.
    if text[:1] in "=+-@":
        return "'" + text
    return text


def json_cell(value):
    if value is None:
        return ""
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def render_csv(rows):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: cell(row[column]) for column in COLUMNS})
    return buffer.getvalue().encode("utf-8")


def build_rows(http, channel, videos, week, moment, available_through, lifetime, daily):
    rows = []
    exported_at = moment.astimezone(UTC).isoformat()
    seen = set()
    ordered = []
    for video in videos:
        if video["id"] in seen:
            continue
        seen.add(video["id"])
        ordered.append(video)
    for video in ordered:
        published = publication_time(video)
        video_id = video["id"]
        window = window_days(published)
        rows_by_date = {
            slot["date"]: daily[(video_id, slot["date"])]
            for slot in window
            if (video_id, slot["date"]) in daily
        }
        first = aggregate_window(window, rows_by_date, available_through)
        totals = lifetime.get(video_id)
        if totals:
            views = float(totals["views"])
            average = float(totals["averageViewDuration"])
            subscribers = float(totals["subscribersGained"])
        else:
            views, average, subscribers = 0, 0, 0
        publish_day = pacific_date(published).isoformat()
        retention_end = available_through or pacific_date(moment).isoformat()
        lifetime_curve = None
        first_curve = None
        first_curve_end = ""
        if publish_day <= retention_end:
            lifetime_curve = retention_curve(
                http, channel.youtube_channel_id, video_id, publish_day, retention_end
            )
        if window and window[0]["date"] <= retention_end:
            first_curve_end = min(window[-1]["date"], retention_end)
            first_curve = retention_curve(
                http,
                channel.youtube_channel_id,
                video_id,
                window[0]["date"],
                first_curve_end,
            )
        rows.append(
            {
                "week": week,
                "channel_id": channel.youtube_channel_id,
                "channel_label": channel.label,
                "video_id": video_id,
                "title": video["snippet"].get("title") or "",
                "published_at": published.isoformat(),
                "privacy_status": (video.get("status") or {}).get("privacyStatus")
                or "",
                "analytics_last_day": available_through or "",
                "views_lifetime": views,
                "average_view_duration_seconds_lifetime": average,
                "subscribers_gained_lifetime": subscribers,
                "audience_retention_lifetime": json_cell(lifetime_curve),
                "views_first_72h": first["views"],
                "average_view_duration_seconds_first_72h": first[
                    "average_view_duration"
                ],
                "subscribers_gained_first_72h": first["subscribers_gained"],
                "audience_retention_first_72h": json_cell(first_curve),
                "audience_retention_first_72h_end": first_curve_end,
                "first_72h_analytics_complete": first["complete"],
                "first_72h_days": json_cell(first["days"]),
                "activity_api": ACTIVITY_API,
                "first_72h_method": FIRST_72H_METHOD,
                "exported_at": exported_at,
            }
        )
    rows.sort(key=lambda row: (row["published_at"], row["video_id"]))
    return rows


def file_name(channel_id, week):
    return (
        f"youtube-stats-{require_drive_id(channel_id, 'YouTube channel ID')}-{week}.csv"
    )


def export_stats(settings, base, http, *, clock=None, week=None):
    targets = stats_targets(settings)
    if not targets:
        raise ValueError(
            "Set stats_drive_folder_id or stats_channels before export-stats"
        )
    moment = (clock or (lambda: datetime.now(UTC)))()
    week = canonical_week(week, moment)
    lock_path = base / "data" / "export-stats.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with portalocker.Lock(str(lock_path), timeout=0):
            return export_locked(targets, http, moment, week)
    except LockException as error:
        raise ValueError("Stats export is already running") from error


def export_locked(targets, http, moment, week):
    # Each target is queried with its own channel id. verify_channel fails if
    # this login cannot see that channel, so a second channel is not left blank.
    youtube = YouTube(http)
    for target in targets:
        youtube.verify_channel(target.youtube_channel_id)
    drive = Drive(http)
    today = pacific_date(moment)
    written = []
    for target in targets:
        available = analytics_last_day(http, target.youtube_channel_id, today)
        videos = uploaded_videos(http, target.youtube_channel_id)
        lifetime, daily = activity_for_channel(
            http, target.youtube_channel_id, videos, available, today
        )
        rows = build_rows(
            http, target, videos, week, moment, available, lifetime, daily
        )
        file_ids = drive.upsert_stats_file(
            target.drive_folder_id,
            file_name(target.youtube_channel_id, week),
            render_csv(rows),
        )
        written.append(
            {
                "channel_id": target.youtube_channel_id,
                "week": week,
                "videos": len(rows),
                "file_ids": file_ids,
            }
        )
    return written
