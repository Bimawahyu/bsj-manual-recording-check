from __future__ import annotations

import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Iterable

import requests

from .engine import EngineApiError, VehicleMappingError, fetch_engine_data, resolve_bsj_vehicle_ids
from .jobs import JobContext
from .settings import settings


class FileListApiError(RuntimeError):
    pass


_official_rate_lock = threading.Lock()
_official_last_request = 0.0


def _pace_official_api() -> None:
    """Apply a process-wide minimum interval between official BSJ requests."""
    global _official_last_request
    interval = max(0.0, settings.bsj_official_min_interval_seconds)
    if interval <= 0:
        return
    with _official_rate_lock:
        now = time.monotonic()
        wait = interval - (now - _official_last_request)
        if wait > 0:
            time.sleep(wait)
        _official_last_request = time.monotonic()


def _retry_delay(attempt: int) -> float:
    base = max(0.0, settings.bsj_api_retry_backoff_seconds)
    cap = max(base, settings.bsj_api_retry_backoff_max_seconds)
    return min(cap, base * (2 ** max(0, attempt - 1)))


def _clean_device_id_text(text: str) -> str:
    """Normalize pasted device IDs without changing visible identifier content.

    Excel/Teams/browser copy-paste can introduce invisible Unicode format
    characters (for example U+200B zero-width space or U+FEFF BOM). Those
    characters are visually indistinguishable in the textarea but used to make
    an otherwise valid numeric terminalNo fail validation.
    """
    text = unicodedata.normalize("NFKC", str(text))
    # Drop Unicode format/control marks that can be invisibly embedded in IDs.
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return text


def parse_imeis(value: str | Iterable[str]) -> list[str]:
    if isinstance(value, str):
        normalized = _clean_device_id_text(value)
        # \s handles Unicode whitespace too (NBSP, etc.).
        pieces = re.split(r"[,;\s]+", normalized)
    else:
        pieces = [_clean_device_id_text(str(x)) for x in value]

    out: list[str] = []
    seen: set[str] = set()
    for raw in pieces:
        imei = raw.strip()
        if not imei:
            continue
        if not re.fullmatch(r"\d{8,20}", imei):
            # repr makes hidden/stray visible characters diagnosable if an
            # actually invalid identifier is supplied.
            raise ValueError(f"Invalid IMEI/device ID: {imei!r}")
        if imei not in seen:
            seen.add(imei)
            out.append(imei)
    if not out:
        raise ValueError("Enter at least one IMEI")
    if len(out) > 300:
        raise ValueError("Maximum 300 IMEIs per run")
    return out


def _dt(value: Any) -> datetime | None:
    if value in (None, "", "-"):
        return None
    text = str(value).strip().replace("T", " ")
    text = re.sub(r"([+-]\d\d:\d\d|Z)$", "", text).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def _fmt(seconds: float | int) -> str:
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def _official_headers() -> dict[str, str]:
    if not settings.bsj_api_key:
        raise FileListApiError("BSJ_API_KEY is empty in .env")
    return {
        "key": settings.bsj_api_key,
        "version": settings.bsj_api_version,
        "Accept-Language": settings.bsj_api_language,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _json_body(resp: requests.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except Exception as exc:
        raise FileListApiError(f"BSJ API returned non-JSON response: {(resp.text or '')[:500]}") from exc
    if not isinstance(body, dict):
        raise FileListApiError("BSJ API returned unexpected response format")
    return body


def _request_json(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    """Official API request with small retry/backoff for rate-limit/transient failures."""
    last_error: Exception | None = None
    retries = max(1, settings.bsj_api_max_retries)
    for attempt in range(1, retries + 1):
        try:
            _pace_official_api()
            resp = requests.request(method, url, headers=_official_headers(), timeout=settings.bsj_api_timeout, **kwargs)
            # Retry transport-level throttling/server errors.
            if resp.status_code in (429, 502, 503, 504):
                if attempt < retries:
                    time.sleep(_retry_delay(attempt))
                    continue
                raise FileListApiError(f"HTTP {resp.status_code}: {(resp.text or '')[:300]}")
            resp.raise_for_status()
            body = _json_body(resp)
            code = int(body.get("code") or 0)
            # BSJ docs: 503 = request too frequently, 505 = response timeout.
            if code in (503, 505):
                message = str(body.get("msg") or body.get("message") or body)[:500]
                if attempt < retries:
                    time.sleep(_retry_delay(attempt))
                    continue
                raise FileListApiError(f"BSJ code={code}: {message}")
            return body
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(_retry_delay(attempt))
                continue
    raise FileListApiError(str(last_error or "BSJ API request failed"))


def _local_day(date_text: str) -> tuple[datetime, datetime]:
    start = datetime.strptime(date_text, "%Y-%m-%d")
    return start, start + timedelta(days=1) - timedelta(seconds=1)


def build_filelist_query_window(date_text: str) -> tuple[str, str]:
    """Build the file-list query window for the selected platform-local day.

    Live BSJ VOD traffic was verified to send the selected date directly as
    00:00:00 -> 23:59:59. LOCAL is therefore the default. UTC remains available
    as an explicit compatibility fallback through .env.
    """
    start, end = _local_day(date_text)
    if settings.bsj_filelist_request_time_mode == "UTC":
        offset = timedelta(hours=settings.app_utc_offset_hours)
        start -= offset
        end -= offset
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


def build_filelist_query_range(start_date_text: str, end_date_text: str) -> tuple[str, str]:
    """Build one inclusive BSJ file-list request window for multiple local days.

    This helper remains available for compatibility with older multi-day builds.
    V1.7.5 D-1-only does not use a multi-day File List request.
    """
    start = datetime.strptime(start_date_text, "%Y-%m-%d")
    end = datetime.strptime(end_date_text, "%Y-%m-%d") + timedelta(days=1) - timedelta(seconds=1)
    if end < start:
        start, end = end.replace(hour=0, minute=0, second=0), start + timedelta(days=1) - timedelta(seconds=1)
    if settings.bsj_filelist_request_time_mode == "UTC":
        offset = timedelta(hours=settings.app_utc_offset_hours)
        start -= offset
        end -= offset
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


def parse_filelist_time(value: Any) -> datetime | None:
    dt = _dt(value)
    if not dt:
        return None
    if settings.bsj_filelist_response_time_mode == "UTC":
        dt += timedelta(hours=settings.app_utc_offset_hours)
    return dt


def parse_installed_channels(profile: dict[str, Any] | None) -> set[str]:
    if not profile:
        return set()
    out: set[str] = set()
    raw = profile.get("camera_line")
    if isinstance(raw, (list, tuple)):
        values = raw
    else:
        values = re.findall(r"\d+", str(raw or ""))
    for value in values:
        try:
            ch = int(value)
        except Exception:
            continue
        if 1 <= ch <= 16:
            out.add(f"CH{ch:02d}")
    if not out:
        try:
            count = int(profile.get("camera_num") or 0)
        except Exception:
            count = 0
        for ch in range(1, min(count, 16) + 1):
            out.add(f"CH{ch:02d}")
    return out


def wake_device_api(imei: str) -> dict[str, Any]:
    base = settings.bsj_api_base_url.rstrip("/")
    body = _request_json("GET", f"{base}/command/wakenDevice", params={"terminalNo": imei})
    code = int(body.get("code") or 0)
    if code != 200:
        raise FileListApiError(str(body.get("msg") or body.get("message") or body))
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    return {"imei": imei, "command_no": data.get("commandNo"), "code": code, "msg": body.get("msg")}


def _extract_list_file(body: dict[str, Any]) -> list[dict[str, Any]]:
    data = body.get("data")
    if isinstance(data, dict) and isinstance(data.get("listFile"), list):
        return [x for x in data["listFile"] if isinstance(x, dict)]
    return []


def fetch_device_filelist(imei: str, date_text: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    start_time, end_time = build_filelist_query_window(date_text)
    base = settings.bsj_api_base_url.rstrip("/")
    payload = {
        "startTime": start_time,
        "endTime": end_time,
        "queryType": 1,
        "queryParam": imei,
        "channelNo": 0,
    }
    body = _request_json("POST", f"{base}/video/devRecordQuery", json=payload)
    code = int(body.get("code") or 0)
    if code != 200:
        raise FileListApiError(str(body.get("msg") or body.get("message") or body))
    rows = _extract_list_file(body)
    return rows, {
        "imei": imei,
        "code": code,
        "msg": body.get("msg"),
        "file_count": len(rows),
        "request_start": start_time,
        "request_end": end_time,
    }


def fetch_device_filelist_range(imei: str, start_date_text: str, end_date_text: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch a multi-day File List in one BSJ request.

    A successful empty response is still VALID evidence (no files). API failures
    raise FileListApiError and the scheduler can fall back to per-day requests.
    """
    start_time, end_time = build_filelist_query_range(start_date_text, end_date_text)
    base = settings.bsj_api_base_url.rstrip("/")
    payload = {
        "startTime": start_time,
        "endTime": end_time,
        "queryType": 1,
        "queryParam": imei,
        "channelNo": 0,
    }
    body = _request_json("POST", f"{base}/video/devRecordQuery", json=payload)
    code = int(body.get("code") or 0)
    if code != 200:
        raise FileListApiError(str(body.get("msg") or body.get("message") or body))
    rows = _extract_list_file(body)
    return rows, {
        "imei": imei,
        "code": code,
        "msg": body.get("msg"),
        "file_count": len(rows),
        "request_start": start_time,
        "request_end": end_time,
        "request_mode": "MULTI_DAY_RANGE",
    }


def build_file_data(
    imeis: list[str],
    date_text: str,
    profiles: dict[str, dict[str, Any]],
    file_rows_by_imei: dict[str, list[dict[str, Any]]],
    request_status: dict[str, dict[str, Any]],
    master_reference: dict[str, dict[str, Any]] | None = None,
    strict_channel_reference: bool = False,
) -> dict[str, Any]:
    day_start, day_end = _local_day(date_text)
    meta: dict[str, dict[str, Any]] = {}
    video_files: list[dict[str, Any]] = []

    master_reference = master_reference or {}
    for imei in imeis:
        profile = profiles.get(imei) or {}
        reference = master_reference.get(imei) or {}
        status = request_status.get(imei) or {}
        if reference:
            installed_channels = set(reference.get("installed_channels") or [])
            channel_config_status = str(reference.get("channel_config_status") or ("FOUND" if installed_channels else "NOT_FOUND")).upper()
            domain = str(reference.get("domain") or "UNKNOWN")
        else:
            installed_channels = parse_installed_channels(profile) if not strict_channel_reference else set()
            channel_config_status = "NOT_FOUND" if strict_channel_reference else "BSJ_FALLBACK"
            domain = str(profile.get("group") or "UNKNOWN")
        meta[imei] = {
            "imei": imei,
            "plate": profile.get("plate") or imei,
            "domain": domain,
            "bsj_vehicle_id": profile.get("vehicle_id"),
            "terminal_type": profile.get("terminal_type") or "",
            "command_status": status.get("status") or "UNKNOWN",
            "command_time": "",
            "command_message": status.get("message") or "",
            "filelist_ok": bool(status.get("ok")),
            "installed_channels": installed_channels,
            "channel_config_status": channel_config_status,
            "channel_reference_source": reference.get("source") or ("METABASE" if reference else "NONE"),
        }

        for row in file_rows_by_imei.get(imei, []):
            try:
                channel = int(row.get("channel"))
            except Exception:
                channel = 0
            start = parse_filelist_time(row.get("begintime") or row.get("beginTime") or row.get("startTime"))
            end = parse_filelist_time(row.get("endtime") or row.get("endTime"))
            if channel <= 0 or not start or not end or end <= start:
                continue
            # Keep only portions overlapping the selected local day.
            if end < day_start or start > day_end:
                continue
            clipped_start = max(start, day_start)
            clipped_end = min(end, day_end + timedelta(seconds=1))
            if clipped_end <= clipped_start:
                continue
            ch_text = f"CH{channel:02d}"
            # In scheduler mode the installed-channel list comes from the public
            # Metabase master and must be authoritative. Observed files on an
            # uninstalled channel are retained as raw evidence but do not make
            # that channel eligible for completeness judgment.
            if not strict_channel_reference:
                meta[imei]["installed_channels"].add(ch_text)
            seconds = int((clipped_end - clipped_start).total_seconds())
            video_files.append({
                "date": date_text,
                "imei": imei,
                "plate": meta[imei]["plate"],
                "domain": meta[imei]["domain"],
                "channel": ch_text,
                "begin_time": clipped_start.strftime("%Y-%m-%d %H:%M:%S"),
                "end_time": clipped_end.strftime("%Y-%m-%d %H:%M:%S"),
                "duration": _fmt(seconds),
                "duration_seconds": seconds,
                "datatype": row.get("datatype"),
                "codetype": row.get("codetype"),
                "command_status": meta[imei]["command_status"],
            })

    video_files.sort(key=lambda x: (x["imei"], x["channel"], x["begin_time"], x["end_time"]))
    return {"meta": meta, "video_files": video_files}


def _merge_intervals(intervals: list[tuple[datetime, datetime]], tolerance_seconds: int = 0) -> list[tuple[datetime, datetime]]:
    intervals = sorted((s, e) for s, e in intervals if e > s)
    if not intervals:
        return []
    merged: list[list[datetime]] = [[intervals[0][0], intervals[0][1]]]
    for start, end in intervals[1:]:
        current = merged[-1]
        if start <= current[1] or (start - current[1]).total_seconds() <= tolerance_seconds:
            if end > current[1]:
                current[1] = end
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def _missing_against_coverage(expected: list[tuple[datetime, datetime]], coverage: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Return the mathematically missing portions of expected recording time.

    Important: this function does *not* apply the QC gap threshold. Even a
    1-second hole reduces actual recording coverage. The configured threshold is
    used later only to decide which missing intervals are significant enough to
    highlight as QC gaps.
    """
    missing: list[tuple[datetime, datetime]] = []
    coverage = _merge_intervals(coverage, tolerance_seconds=0)
    for exp_start, exp_end in expected:
        cursor = exp_start
        for vid_start, vid_end in coverage:
            if vid_end <= cursor or vid_start >= exp_end:
                continue
            if vid_start > cursor:
                gap_end = min(vid_start, exp_end)
                if gap_end > cursor:
                    missing.append((cursor, gap_end))
            cursor = max(cursor, min(vid_end, exp_end))
            if cursor >= exp_end:
                break
        if cursor < exp_end:
            missing.append((cursor, exp_end))
    return missing


def analyze_completeness(file_data: dict[str, Any], engine_data: dict[str, Any], imeis: list[str], date_text: str) -> dict[str, Any]:
    meta = file_data["meta"]
    video_files = list(file_data["video_files"])
    gap_threshold = max(0, settings.gap_threshold_seconds)

    engine_events = list(engine_data.get("events") or [])
    engine_intervals = list(engine_data.get("intervals") or [])
    engine_unverified_intervals = list(engine_data.get("unverified_intervals") or [])
    intervals_by_imei: dict[str, list[tuple[datetime, datetime]]] = {i: [] for i in imeis}
    for interval in engine_intervals:
        start = _dt(interval.get("engine_on"))
        end = _dt(interval.get("engine_off"))
        if start and end and end > start and interval.get("imei") in intervals_by_imei:
            intervals_by_imei[interval["imei"]].append((start, end))

    # V1.4.3: surface *why* a device has no Engine ON data, instead of a bare
    # INCONCLUSIVE with no explanation. Vehicle-mapping and Engine Status
    # failures are now isolated per device (see engine.py) rather than aborting
    # the whole run, so this is where that per-device outcome becomes visible.
    engine_diag = engine_data.get("diagnostics") or {}
    vehicle_mapping_diag = engine_diag.get("vehicle_mapping") if isinstance(engine_diag, dict) else {}
    missing_vehicle_ids = set((vehicle_mapping_diag or {}).get("missing") or [])
    engine_api_failed = {
        str(f.get("imei")) for f in (engine_diag.get("failures") or []) if isinstance(f, dict) and f.get("imei")
    }
    unverified_seconds_by_imei: dict[str, int] = {}
    unverified_count_by_imei: dict[str, int] = {}
    for item in engine_unverified_intervals:
        imei = item.get("imei")
        if imei in intervals_by_imei:
            unverified_seconds_by_imei[imei] = unverified_seconds_by_imei.get(imei, 0) + int(item.get("duration_seconds") or 0)
            unverified_count_by_imei[imei] = unverified_count_by_imei.get(imei, 0) + 1

    def _engine_data_status(imei: str) -> str:
        if imei in missing_vehicle_ids:
            return "VEHICLE_MAPPING_FAILED"
        if imei in engine_api_failed:
            return "ENGINE_API_FAILED"
        if not intervals_by_imei.get(imei):
            return "NO_ENGINE_ON_EVENTS"
        return "OK"

    files_by_channel: dict[tuple[str, str], list[tuple[datetime, datetime]]] = {}
    for item in video_files:
        start, end = _dt(item["begin_time"]), _dt(item["end_time"])
        if start and end:
            files_by_channel.setdefault((item["imei"], item["channel"]), []).append((start, end))

    channel_analysis: list[dict[str, Any]] = []
    video_gaps: list[dict[str, Any]] = []
    detail: dict[str, Any] = {}
    device_summary: list[dict[str, Any]] = []

    for imei in imeis:
        expected = sorted(intervals_by_imei.get(imei, []))
        expected_seconds = int(sum((e - s).total_seconds() for s, e in expected))
        installed = sorted(meta[imei]["installed_channels"])
        if not installed:
            installed = sorted({x["channel"] for x in video_files if x["imei"] == imei})

        filelist_ok = bool(meta[imei].get("filelist_ok"))
        engine_data_status = _engine_data_status(imei)
        channel_config_status = str(meta[imei].get("channel_config_status") or "UNKNOWN").upper()
        channel_config_ok = channel_config_status in {"FOUND", "BSJ_FALLBACK"}
        analysis_ready = filelist_ok and engine_data_status == "OK" and expected_seconds > 0 and channel_config_ok and bool(installed)

        # Data-validity is intentionally separated from recording quality.
        # API/mapping/reference failures are INVALID DATA and must never become BAD.
        # A successful Engine API with no Engine ON interval is valid N/A data.
        if not filelist_ok or engine_data_status in {"VEHICLE_MAPPING_FAILED", "ENGINE_API_FAILED"} or not channel_config_ok or not installed:
            non_quality_status = "INVALID DATA"
        elif engine_data_status == "NO_ENGINE_ON_EVENTS" or expected_seconds <= 0:
            non_quality_status = "NO ENGINE ACTIVITY"
        else:
            non_quality_status = None

        device_channel_rows: list[dict[str, Any]] = []
        for channel in installed:
            coverage = files_by_channel.get((imei, channel), [])

            # Actual coverage is mathematical: every uncovered second counts.
            # The QC threshold is applied only to the *highlighted gap list*.
            actual_missing = _missing_against_coverage(expected, coverage) if analysis_ready else []
            significant_missing = [
                (start, end)
                for start, end in actual_missing
                if (end - start).total_seconds() > gap_threshold
            ]
            missing_seconds = int(sum((e - s).total_seconds() for s, e in actual_missing))
            significant_missing_seconds = int(sum((e - s).total_seconds() for s, e in significant_missing))
            covered_seconds = max(0, expected_seconds - missing_seconds) if analysis_ready else 0
            coverage_pct = round((covered_seconds / expected_seconds) * 100, 2) if analysis_ready else None
            if not analysis_ready:
                status = non_quality_status or "INVALID DATA"
            elif coverage_pct is not None and coverage_pct > 95:
                status = "GOOD"
            elif coverage_pct is not None and coverage_pct >= 85:
                status = "MEDIUM"
            else:
                status = "BAD"

            row = {
                "date": date_text,
                "imei": imei,
                "plate": meta[imei]["plate"],
                "domain": meta[imei]["domain"],
                "channel": channel,
                "engine_on_duration": _fmt(expected_seconds),
                "engine_on_seconds": expected_seconds,
                "covered_duration": _fmt(covered_seconds) if analysis_ready else "-",
                "covered_seconds": covered_seconds if analysis_ready else None,
                "missing_duration": _fmt(missing_seconds) if analysis_ready else "-",
                "missing_seconds": missing_seconds if analysis_ready else None,
                "significant_gap_duration": _fmt(significant_missing_seconds) if analysis_ready else "-",
                "significant_gap_seconds": significant_missing_seconds if analysis_ready else None,
                "coverage_percent": coverage_pct,
                "gap_count": len(significant_missing) if analysis_ready else None,
                "status": status,
                "command_status": meta[imei]["command_status"],
                "engine_data_status": engine_data_status,
                "channel_config_status": channel_config_status,
            }
            channel_analysis.append(row)
            device_channel_rows.append(row)

            if analysis_ready:
                for start, end in significant_missing:
                    seconds = int((end - start).total_seconds())
                    video_gaps.append({
                        "date": date_text,
                        "imei": imei,
                        "plate": meta[imei]["plate"],
                        "domain": meta[imei]["domain"],
                        "channel": channel,
                        "gap_start": start.strftime("%Y-%m-%d %H:%M:%S"),
                        "gap_end": end.strftime("%Y-%m-%d %H:%M:%S"),
                        "gap_duration": _fmt(seconds),
                        "gap_seconds": seconds,
                        "gap_minutes": round(seconds / 60.0, 2),
                    })

        coverages = [x["coverage_percent"] for x in device_channel_rows if x["coverage_percent"] is not None]
        if non_quality_status:
            overall = non_quality_status
        elif any((x or 0) < 85 for x in coverages) or len(coverages) < len(installed):
            overall = "BAD"
        elif any((x or 0) <= 95 for x in coverages):
            overall = "MEDIUM"
        else:
            overall = "GOOD"

        files = [x for x in video_files if x["imei"] == imei]
        gaps = [x for x in video_gaps if x["imei"] == imei]
        events = [x for x in engine_events if x["imei"] == imei]
        interval_rows = [x for x in engine_intervals if x["imei"] == imei]
        unverified_rows = [x for x in engine_unverified_intervals if x["imei"] == imei]
        min_coverage = min(coverages) if coverages else None
        device_summary.append({
            "imei": imei,
            "bsj_vehicle_id": meta[imei].get("bsj_vehicle_id"),
            "plate": meta[imei]["plate"],
            "domain": meta[imei]["domain"],
            "engine_on_duration": _fmt(expected_seconds),
            "engine_intervals": len(expected),
            "installed_channels": ", ".join(installed) if installed else "-",
            "video_files": len(files),
            "gap_count": len(gaps) if filelist_ok else None,
            "min_coverage_percent": min_coverage,
            "status": overall,
            "command_status": meta[imei]["command_status"],
            "engine_data_status": engine_data_status,
            "channel_config_status": channel_config_status,
            "unverified_engine_windows": unverified_count_by_imei.get(imei, 0),
            "unverified_engine_duration": _fmt(unverified_seconds_by_imei.get(imei, 0)),
        })
        detail[imei] = {
            "meta": {**meta[imei], "installed_channels": installed},
            "engine_events": events,
            "engine_intervals": interval_rows,
            "engine_unverified_intervals": unverified_rows,
            "channels": device_channel_rows,
            "gaps": gaps,
            "files": files,
        }

    counts = {"GOOD": 0, "MEDIUM": 0, "BAD": 0, "INVALID DATA": 0, "NO ENGINE ACTIVITY": 0}
    for row in device_summary:
        counts[row["status"]] = counts.get(row["status"], 0) + 1

    channel_analysis.sort(key=lambda x: (x["imei"], x["channel"]))
    video_gaps.sort(key=lambda x: (x["imei"], x["channel"], x["gap_start"]))
    return {
        "summary": {
            "requested_devices": len(imeis),
            "good_devices": counts.get("GOOD", 0),
            "medium_devices": counts.get("MEDIUM", 0),
            "bad_devices": counts.get("BAD", 0),
            "invalid_data_devices": counts.get("INVALID DATA", 0),
            "no_engine_activity_devices": counts.get("NO ENGINE ACTIVITY", 0),
            # Kept for older UI/API clients; INVALID DATA is the replacement semantics.
            "inconclusive_devices": counts.get("INVALID DATA", 0),
            "engine_events": len(engine_events),
            "engine_intervals": len(engine_intervals),
            "video_files": len(video_files),
            "video_gaps": len(video_gaps),
            "engine_unverified_windows": len(engine_unverified_intervals),
            "vehicle_mapping_failed_devices": len(missing_vehicle_ids),
            "engine_status_failed_devices": len(engine_api_failed),
        },
        "device_summary": device_summary,
        "channel_analysis": channel_analysis,
        "video_gaps": video_gaps,
        "engine_events": engine_events,
        "engine_intervals": engine_intervals,
        "engine_unverified_intervals": engine_unverified_intervals,
        "video_files": video_files,
        "detail": detail,
        "engine_diagnostics": engine_data.get("diagnostics") or {},
    }


def run_check(ctx: JobContext, *, environment: str = "Production", imeis: list[str], date_text: str) -> dict[str, Any]:
    datetime.strptime(date_text, "%Y-%m-%d")

    # 1) Resolve realtime BSJ device metadata FIRST. /monitor/getRealTimeInfo
    # returns accState where 0=ON and 1=OFF. Only confirmed ACC OFF devices
    # are woken. ACC ON and unknown-state devices go directly to File List.
    ctx.update(stage="checking_current_acc", message="Checking current ACC state from BSJ realtime", current=0, total=len(imeis))
    try:
        vehicle_map, mapping_diag = resolve_bsj_vehicle_ids(imeis)
    except VehicleMappingError as exc:
        raise RuntimeError(f"BSJ vehicle mapping for Engine Status failed: {exc}") from exc

    profiles = mapping_diag.get("profiles") if isinstance(mapping_diag, dict) else {}
    if not isinstance(profiles, dict):
        profiles = {}

    acc_on: list[str] = []
    acc_off: list[str] = []
    acc_unknown: list[str] = []
    current_acc: dict[str, str] = {}
    for imei in imeis:
        raw = (profiles.get(imei) or {}).get("acc_state")
        if raw == 0:
            acc_on.append(imei)
            current_acc[imei] = "ON"
        elif raw == 1:
            acc_off.append(imei)
            current_acc[imei] = "OFF"
        else:
            acc_unknown.append(imei)
            current_acc[imei] = "UNKNOWN"

    ctx.update(
        current=len(imeis),
        total=len(imeis),
        message=f"Current ACC: ON {len(acc_on)} · OFF {len(acc_off)} · UNKNOWN {len(acc_unknown)}",
    )

    file_rows_by_imei: dict[str, list[dict[str, Any]]] = {}
    file_request_status: dict[str, dict[str, Any]] = {}
    file_failures: list[dict[str, Any]] = []
    wake_results: list[dict[str, Any]] = []
    wake_failures: list[dict[str, Any]] = []

    def file_one(imei: str) -> tuple[str, list[dict[str, Any]] | None, dict[str, Any] | None, str | None]:
        try:
            rows, diag = fetch_device_filelist(imei, date_text)
            return imei, rows, diag, None
        except Exception as exc:
            return imei, None, None, str(exc)

    def save_file_result(
        imei: str,
        rows: list[dict[str, Any]] | None,
        diag: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        if error:
            file_rows_by_imei[imei] = []
            file_request_status[imei] = {"ok": False, "status": "API FAILED", "message": error}
            file_failures.append({"imei": imei, "error": error})
        else:
            rows = rows or []
            file_rows_by_imei[imei] = rows
            file_request_status[imei] = {
                "ok": True,
                "status": "SUCCESS" if rows else "SUCCESS - NO FILES",
                "message": (diag or {}).get("msg") or "Success",
                **(diag or {}),
            }

    # 2) ACC ON devices do NOT need wake-up. Unknown realtime ACC is also not
    # woken because the rule is strict: only confirmed accState=1 (OFF) may wake.
    # Query ON/UNKNOWN devices immediately so an ACC ON manual check has no 60s delay.
    direct_targets = acc_on + acc_unknown
    if direct_targets:
        ctx.update(
            stage="loading_file_list",
            message=f"ACC ON/unknown: querying File List directly for {len(direct_targets)} device(s)",
            current=0,
            total=len(direct_targets),
        )
        with ThreadPoolExecutor(max_workers=max(1, min(settings.bsj_api_max_workers, len(direct_targets)))) as pool:
            futures = [pool.submit(file_one, imei) for imei in direct_targets]
            done = 0
            for fut in as_completed(futures):
                imei, rows, diag, error = fut.result()
                done += 1
                save_file_result(imei, rows, diag, error)
                ctx.update(
                    current=done,
                    total=len(direct_targets),
                    message=f"Direct File List {done}/{len(direct_targets)}",
                )

    # 3) Wake ONLY confirmed ACC OFF devices.
    if acc_off:
        ctx.update(
            stage="waking_acc_off_devices",
            message=f"Waking {len(acc_off)} confirmed ACC OFF device(s)",
            current=0,
            total=len(acc_off),
        )

        def wake_one(imei: str) -> tuple[str, dict[str, Any] | None, str | None]:
            try:
                return imei, wake_device_api(imei), None
            except Exception as exc:
                return imei, None, str(exc)

        with ThreadPoolExecutor(max_workers=max(1, min(settings.bsj_api_max_workers, len(acc_off)))) as pool:
            futures = [pool.submit(wake_one, imei) for imei in acc_off]
            done = 0
            for fut in as_completed(futures):
                imei, info, error = fut.result()
                done += 1
                if error:
                    wake_failures.append({"imei": imei, "error": error})
                elif info:
                    wake_results.append(info)
                ctx.update(current=done, total=len(acc_off), message=f"ACC OFF wake requests {done}/{len(acc_off)}")

        # 4) Only the ACC OFF subset pays the wake wait.
        wake_wait = max(0, settings.wake_wait_seconds)
        ctx.update(
            stage="waiting_for_wakeup",
            message=f"Waiting {wake_wait}s for {len(acc_off)} ACC OFF device(s)",
            current=0,
            total=wake_wait,
        )
        for second in range(wake_wait):
            time.sleep(1)
            ctx.update(
                current=second + 1,
                total=wake_wait,
                message=f"Waiting for ACC OFF wake-up · {wake_wait - second - 1}s remaining",
            )

        # 5) Query File List for the ACC OFF subset after wake-up.
        ctx.update(
            stage="loading_file_list",
            message=f"Loading File List for {len(acc_off)} woken ACC OFF device(s)",
            current=0,
            total=len(acc_off),
        )
        with ThreadPoolExecutor(max_workers=max(1, min(settings.bsj_api_max_workers, len(acc_off)))) as pool:
            futures = [pool.submit(file_one, imei) for imei in acc_off]
            done = 0
            for fut in as_completed(futures):
                imei, rows, diag, error = fut.result()
                done += 1
                save_file_result(imei, rows, diag, error)
                ctx.update(current=done, total=len(acc_off), message=f"ACC OFF File Lists {done}/{len(acc_off)}")
    else:
        wake_wait = 0

    # Defensive fill so every requested IMEI has an explicit File List status.
    for imei in imeis:
        if imei not in file_request_status:
            file_rows_by_imei[imei] = []
            file_request_status[imei] = {
                "ok": False,
                "status": "API FAILED",
                "message": "File List was not executed",
            }
            file_failures.append({"imei": imei, "error": "File List was not executed"})

    file_data = build_file_data(imeis, date_text, profiles, file_rows_by_imei, file_request_status)

    # 6) Historical Engine ON/OFF still comes from BSJ Platform for the selected date.
    ctx.update(stage="loading_engine_status", message="Loading Engine ON/OFF from BSJ Platform", current=0, total=len(imeis))
    try:
        engine_data = fetch_engine_data(vehicle_map, date_text)
    except EngineApiError as exc:
        raise RuntimeError(f"Engine Status source failed: {exc}") from exc
    engine_data.setdefault("diagnostics", {})["vehicle_mapping"] = mapping_diag
    ctx.update(current=len(imeis), total=len(imeis), message="Engine Status loaded")

    # 7) Compare expected Engine ON windows against actual video coverage.
    ctx.update(stage="comparing", message="Comparing Engine ON intervals with video file list", current=0, total=1)
    result = analyze_completeness(file_data, engine_data, imeis, date_text)
    result["collection"] = {
        "current_acc_source": "BSJ official API /monitor/getRealTimeInfo accState",
        "current_acc_states": current_acc,
        "current_acc_on": acc_on,
        "current_acc_off": acc_off,
        "current_acc_unknown": acc_unknown,
        "wake_rule": "Wake only when realtime accState=1 (ACC OFF); accState=0 (ACC ON) goes directly to File List",
        "wake_source": "BSJ official API /command/wakenDevice using terminalNo/IMEI",
        "wake_requests_total": len(acc_off),
        "wake_requests_success": len(wake_results),
        "wake_requests_failed": len(wake_failures),
        "wake_skipped_acc_on": len(acc_on),
        "wake_skipped_unknown_acc": len(acc_unknown),
        "wake_wait_seconds": wake_wait,
        "wake_results": wake_results,
        "wake_failures": wake_failures,
        "file_list_source": "BSJ official API /video/devRecordQuery using IMEI directly",
        "file_list_identifier": "IMEI/device ID (queryType=1, queryParam=IMEI)",
        "file_list_requests_total": len(imeis),
        "file_list_requests_success": len(imeis) - len(file_failures),
        "file_list_requests_failed": len(file_failures),
        "file_list_failures": file_failures,
        "file_list_request_time_mode": settings.bsj_filelist_request_time_mode,
        "file_list_response_time_mode": settings.bsj_filelist_response_time_mode,
        "app_utc_offset_hours": settings.app_utc_offset_hours,
        "engine_source": "BSJ Platform /webapi/acc/getAccDetailReport",
        "engine_identifier": "BSJ numeric vehicleId",
        "vehicle_mapping_source": "BSJ official API /monitor/getRealTimeInfo",
        "gap_threshold_seconds": settings.gap_threshold_seconds,
        "coverage_method": "Actual uncovered seconds / Engine ON seconds; gap threshold does not inflate coverage",
        "significant_gap_rule": f"Only gaps > {settings.gap_threshold_seconds}s are highlighted/exported as Video Gaps",
        "vehicle_mapping_requests_total": len(imeis),
        "vehicle_mapping_failed": mapping_diag.get("missing") or [],
        "engine_status_failures": (engine_data.get("diagnostics") or {}).get("failures") or [],
        "engine_max_silence_hours": settings.bsj_engine_max_silence_hours,
    }
    ctx.update(current=1, total=1, message="Comparison complete")
    return result
