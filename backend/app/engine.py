from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Iterable

import requests

from .settings import settings


class EngineApiError(RuntimeError):
    pass


class VehicleMappingError(RuntimeError):
    pass


_engine_rate_lock = threading.Lock()
_engine_last_request = 0.0


def _pace_engine_api() -> None:
    """Apply a process-wide minimum interval between Engine API report calls."""
    global _engine_last_request
    interval = max(0.0, settings.bsj_engine_min_interval_seconds)
    if interval <= 0:
        return
    with _engine_rate_lock:
        now = time.monotonic()
        wait = interval - (now - _engine_last_request)
        if wait > 0:
            time.sleep(wait)
        _engine_last_request = time.monotonic()


def _retry_delay(attempt: int) -> float:
    base = max(0.0, settings.bsj_api_retry_backoff_seconds)
    cap = max(base, settings.bsj_api_retry_backoff_max_seconds)
    return min(cap, base * (2 ** max(0, attempt - 1)))


@dataclass
class BSJWebSession:
    sid: str
    user_id: int | None = None
    user_name: str = ""


def _official_api_headers() -> dict[str, str]:
    if not settings.bsj_api_key:
        raise VehicleMappingError(
            "BSJ official API key is not configured. Set BSJ_API_KEY in .env."
        )
    return {
        "key": settings.bsj_api_key,
        "version": settings.bsj_api_version,
        "Accept-Language": settings.bsj_api_language,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _profile_from_row(row: dict[str, Any]) -> dict[str, Any]:
    terminal = str(row.get("terminalNo") or row.get("deviceId") or "").strip()
    camera_line = row.get("cameraLine")
    camera_num = row.get("cameraNum")
    try:
        camera_num_i = int(camera_num) if camera_num not in (None, "") else None
    except Exception:
        camera_num_i = None
    try:
        vehicle_id = int(row.get("vehicleId"))
    except Exception:
        vehicle_id = None
    expired_raw = row.get("expired")
    try:
        expired_i = int(expired_raw) if expired_raw not in (None, "") else None
    except Exception:
        expired_i = None
    try:
        vehicle_state_i = int(row.get("vehicleState")) if row.get("vehicleState") not in (None, "") else None
    except Exception:
        vehicle_state_i = None
    try:
        acc_state_i = int(row.get("accState")) if row.get("accState") not in (None, "") else None
    except Exception:
        acc_state_i = None
    return {
        "imei": terminal,
        "vehicle_id": vehicle_id,
        "plate": str(row.get("plate") or row.get("deviceName") or terminal or "-").strip() or "-",
        "group": str(row.get("groupName") or row.get("group") or "-").strip() or "-",
        "camera_line": camera_line,
        "camera_num": camera_num_i,
        "terminal_type": str(row.get("terminalType") or "").strip(),
        "expired": expired_i,
        "expire_date": row.get("expireDate"),
        "vehicle_state": vehicle_state_i,
        "acc_state": acc_state_i,
    }


def _parse_expire_datetime(value: Any) -> datetime | None:
    """Best-effort parser for BSJ expireDate variants used by live inventory."""
    text = str(value or "").strip()
    if not text:
        return None
    # Common API forms seen in BSJ docs and device responses. Timezone labels are
    # intentionally ignored for expiry-day gating; explicit `expired` remains the
    # primary source whenever it is returned.
    candidates = [text]
    for token in (" CST ", " GMT+8 ", " GMT+08:00 "):
        if token in text:
            candidates.append(text.replace(token, " "))
    for candidate in candidates:
        try:
            return datetime.fromisoformat(candidate.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            pass
        for fmt in (
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
            "%a %b %d %H:%M:%S %Y", "%a %b %d %H:%M:%S %Z %Y",
        ):
            try:
                return datetime.strptime(candidate, fmt)
            except Exception:
                continue
    return None


def classify_bsj_expiry(profile: dict[str, Any] | None, now: datetime | None = None) -> dict[str, Any]:
    """Classify renewal status with safe precedence.

    `expired` from realtime is authoritative when present. If realtime does not
    return a device, live-inventory `expireDate` (or vehicleState=4) is used as a
    fallback for discovery. Unexpected/missing evidence remains INVALID.
    """
    p = profile or {}
    current = now or datetime.now()
    expired = p.get("expired")
    expire_date = p.get("expire_date") if "expire_date" in p else p.get("expireDate")
    vehicle_state = p.get("vehicle_state") if "vehicle_state" in p else p.get("vehicleState")
    dt = _parse_expire_datetime(expire_date)

    if expired == 1:
        status, reason, source = "EXPIRED", "BSJ realtime expired=1", "REALTIME_EXPIRED"
    elif expired == 0:
        if dt is not None and dt <= current:
            # Conflict: realtime says active. Never authorize destructive action from
            # a stale date when the authoritative realtime flag says active.
            status, reason, source = "ACTIVE", "BSJ realtime expired=0 (expireDate is past; realtime wins)", "REALTIME_EXPIRED"
        else:
            status, reason, source = "ACTIVE", "BSJ realtime expired=0", "REALTIME_EXPIRED"
    elif expired == 2:
        status, reason, source = "NOT_ACTIVATED", "BSJ realtime expired=2", "REALTIME_EXPIRED"
    elif vehicle_state == 4:
        status, reason, source = "EXPIRED", "BSJ vehicleState=4 (Expired)", "VEHICLE_STATE"
    elif dt is not None:
        if dt <= current:
            status, reason, source = "EXPIRED", f"BSJ expireDate {expire_date} is in the past", "EXPIRE_DATE"
        else:
            status, reason, source = "ACTIVE", f"BSJ expireDate {expire_date} is still valid", "EXPIRE_DATE"
    else:
        status, reason, source = "INVALID", "No usable realtime expired flag, vehicleState, or expireDate", "NONE"
    return {
        "status": status, "expired": expired, "expire_date": expire_date,
        "vehicle_state": vehicle_state, "reason": reason, "expiry_source": source,
    }


def _extract_vehicle_profiles_from_realtime(body: dict[str, Any], requested: set[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    data = body.get("data")
    rows = data.get("list") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        profile = _profile_from_row(row)
        terminal = profile["imei"]
        if terminal in requested and profile["vehicle_id"] is not None:
            out[terminal] = profile
    return out


def _extract_vehicle_profiles_from_user_device(body: dict[str, Any], requested: set[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    data = body.get("data")
    if not isinstance(data, list):
        return out
    for group in data:
        if not isinstance(group, dict):
            continue
        vehicles = group.get("vehicleList")
        if not isinstance(vehicles, list):
            continue
        for row in vehicles:
            if not isinstance(row, dict):
                continue
            merged = dict(row)
            merged.setdefault("groupName", group.get("groupName"))
            profile = _profile_from_row(merged)
            terminal = profile["imei"]
            if terminal in requested and profile["vehicle_id"] is not None:
                out[terminal] = profile
    return out


def _extract_vehicle_profiles_from_flat_list(body: dict[str, Any], requested: set[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    data = body.get("data")
    if not isinstance(data, list):
        return out
    for row in data:
        if not isinstance(row, dict):
            continue
        profile = _profile_from_row(row)
        terminal = profile["imei"]
        if terminal in requested and profile["vehicle_id"] is not None:
            out[terminal] = profile
    return out


def _extract_vehicle_map_from_realtime(body: dict[str, Any], requested: set[str]) -> dict[str, int]:
    return {imei: int(p["vehicle_id"]) for imei, p in _extract_vehicle_profiles_from_realtime(body, requested).items()}


def _extract_vehicle_map_from_user_device(body: dict[str, Any], requested: set[str]) -> dict[str, int]:
    return {imei: int(p["vehicle_id"]) for imei, p in _extract_vehicle_profiles_from_user_device(body, requested).items()}


def _extract_vehicle_map_from_flat_list(body: dict[str, Any], requested: set[str]) -> dict[str, int]:
    return {imei: int(p["vehicle_id"]) for imei, p in _extract_vehicle_profiles_from_flat_list(body, requested).items()}

def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    size = max(1, int(size))
    for index in range(0, len(values), size):
        yield values[index:index + size]


def resolve_bsj_vehicle_ids(imeis: list[str]) -> tuple[dict[str, int], dict[str, Any]]:
    """Resolve terminalNo/IMEI -> BSJ vehicleId and collect device metadata.

    Primary endpoint: POST /monitor/getRealTimeInfo with queryType=1.
    Fallbacks: GET /monitor/getUserDevice and GET /vehicle/getChildVehicle.
    Diagnostics include ``profiles`` with plate/group/cameraLine/cameraNum so the
    recording checker can evaluate installed channels even when a channel has zero files.
    """
    requested_list = [str(x).strip() for x in imeis if str(x).strip()]
    requested = set(requested_list)
    if not requested:
        return {}, {"source": "official_api", "requested": 0, "resolved": 0, "endpoints": [], "profiles": {}}

    headers = _official_api_headers()
    base = settings.bsj_api_base_url.rstrip("/")
    mapping: dict[str, int] = {}
    profiles: dict[str, dict[str, Any]] = {}
    endpoint_diag: list[dict[str, Any]] = []

    for batch in _chunks(requested_list, settings.bsj_api_mapping_batch_size):
        url = f"{base}/monitor/getRealTimeInfo"
        payload = {"queryType": "1", "queryParams": batch}
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=settings.bsj_api_timeout)
            resp.raise_for_status()
            body = _json_body(resp)
            code = int(body.get("code") or 0)
            if code == 200:
                found_profiles = _extract_vehicle_profiles_from_realtime(body, requested - set(mapping))
                for imei, profile in found_profiles.items():
                    profiles[imei] = profile
                    mapping[imei] = int(profile["vehicle_id"])
                endpoint_diag.append({"endpoint": "/monitor/getRealTimeInfo", "code": code, "resolved": len(found_profiles)})
            else:
                endpoint_diag.append({"endpoint": "/monitor/getRealTimeInfo", "code": code, "error": str(body.get("msg") or body.get("message") or body)})
        except Exception as exc:
            endpoint_diag.append({"endpoint": "/monitor/getRealTimeInfo", "error": str(exc)})

    missing = requested - set(mapping)
    if missing:
        url = f"{base}/monitor/getUserDevice"
        try:
            resp = requests.get(url, headers=headers, timeout=settings.bsj_api_timeout)
            resp.raise_for_status()
            body = _json_body(resp)
            code = int(body.get("code") or 0)
            if code == 200:
                found_profiles = _extract_vehicle_profiles_from_user_device(body, missing)
                for imei, profile in found_profiles.items():
                    profiles[imei] = profile
                    mapping[imei] = int(profile["vehicle_id"])
                endpoint_diag.append({"endpoint": "/monitor/getUserDevice", "code": code, "resolved": len(found_profiles)})
            else:
                endpoint_diag.append({"endpoint": "/monitor/getUserDevice", "code": code, "error": str(body.get("msg") or body.get("message") or body)})
        except Exception as exc:
            endpoint_diag.append({"endpoint": "/monitor/getUserDevice", "error": str(exc)})

    missing = requested - set(mapping)
    if missing:
        url = f"{base}/vehicle/getChildVehicle"
        try:
            resp = requests.get(url, headers=headers, timeout=settings.bsj_api_timeout)
            resp.raise_for_status()
            body = _json_body(resp)
            code = int(body.get("code") or 0)
            if code == 200:
                found_profiles = _extract_vehicle_profiles_from_flat_list(body, missing)
                for imei, profile in found_profiles.items():
                    profiles[imei] = profile
                    mapping[imei] = int(profile["vehicle_id"])
                endpoint_diag.append({"endpoint": "/vehicle/getChildVehicle", "code": code, "resolved": len(found_profiles)})
            else:
                endpoint_diag.append({"endpoint": "/vehicle/getChildVehicle", "code": code, "error": str(body.get("msg") or body.get("message") or body)})
        except Exception as exc:
            endpoint_diag.append({"endpoint": "/vehicle/getChildVehicle", "error": str(exc)})

    missing_list = [imei for imei in requested_list if imei not in mapping]
    diagnostics = {
        "source": "BSJ official API terminalNo -> vehicleId",
        "requested": len(requested_list),
        "resolved": len(mapping),
        "missing": missing_list,
        "mapping": dict(mapping),
        "profiles": profiles,
        "endpoints": endpoint_diag,
    }
    # V1.4.3: a device that cannot be mapped to a BSJ vehicleId (e.g. not yet
    # registered on the platform) no longer aborts the whole batch. It is reported
    # in `missing` / the "Vehicle Mapping Failures" export sheet and surfaces as
    # engine_data_status = VEHICLE_MAPPING_FAILED for that device only; every other
    # device in the run still gets a full result.
    return mapping, diagnostics


def _query_bsj_realtime_expiry_batch(batch: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Query one documented /monitor/getRealTimeInfo batch with retry/backoff."""
    headers = _official_api_headers()
    base = settings.bsj_api_base_url.rstrip("/")
    url = f"{base}/monitor/getRealTimeInfo"
    payload = {"queryType": "1", "queryParams": batch}
    body: dict[str, Any] | None = None
    last_error: Exception | None = None
    retries = max(1, settings.bsj_api_max_retries)
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=settings.bsj_api_timeout)
            if resp.status_code in (429, 502, 503, 504) and attempt < retries:
                time.sleep(settings.bsj_api_retry_backoff_seconds * attempt)
                continue
            resp.raise_for_status()
            candidate = _json_body(resp)
            code = int(candidate.get("code") or 0)
            if code in (503, 505) and attempt < retries:
                time.sleep(settings.bsj_api_retry_backoff_seconds * attempt)
                continue
            body = candidate
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(settings.bsj_api_retry_backoff_seconds * attempt)

    if body is None:
        return {}, {"count": len(batch), "error": str(last_error or "BSJ API request failed")}
    code = int(body.get("code") or 0)
    if code != 200:
        return {}, {"count": len(batch), "code": code, "error": str(body.get("msg") or body.get("message") or f"BSJ code {code}")}
    data = body.get("data")
    rows = data.get("list") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return {}, {"count": len(batch), "code": code, "error": "BSJ response has no data.list"}

    requested = set(batch)
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        profile = _profile_from_row(row)
        imei = str(profile.get("imei") or "").strip()
        if imei in requested:
            found[imei] = profile
    return found, {"count": len(batch), "code": code, "returned": len(found)}


def get_bsj_expiry_statuses(imeis: list[str], inventory_profiles: dict[str, dict[str, Any]] | None = None) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Read current BSJ renewal state using the documented `expired` field.

    Primary evidence is POST /monitor/getRealTimeInfo:
      expired=0 -> ACTIVE, expired=1 -> EXPIRED, expired=2 -> NOT_ACTIVATED.

    BSJ can return only a subset of a large queryParams request. V1.7.4 therefore
    re-queries omitted terminals in smaller batches before classifying them from
    the merged live-inventory expireDate/vehicleState metadata. An API error or
    a terminal with no usable evidence remains INVALID and is never eligible for
    destructive re-registration.
    """
    requested_list = list(dict.fromkeys(str(x).strip() for x in imeis if str(x).strip()))
    if not requested_list:
        return {}, {"requested": 0, "returned": 0, "batches": []}

    results: dict[str, dict[str, Any]] = {}
    profiles_found: dict[str, dict[str, Any]] = {}
    batch_diag: list[dict[str, Any]] = []

    primary_size = max(1, int(getattr(settings, "mass_expiry_query_batch_size", settings.bsj_api_mapping_batch_size)))
    for batch in _chunks(requested_list, primary_size):
        found, diag = _query_bsj_realtime_expiry_batch(batch)
        profiles_found.update(found)
        diag["pass"] = "PRIMARY"
        batch_diag.append(diag)
        if getattr(settings, "mass_expiry_scan_batch_delay_seconds", 0) > 0:
            time.sleep(settings.mass_expiry_scan_batch_delay_seconds)

    # A large realtime request may be partially returned. Re-query omissions in
    # smaller groups, which is substantially cheaper than one request per device
    # while recovering devices hidden by partial batch responses.
    missing = [imei for imei in requested_list if imei not in profiles_found]
    requery_size = max(1, int(getattr(settings, "mass_expiry_requery_batch_size", 10)))
    passes = max(0, int(getattr(settings, "mass_expiry_requery_passes", 2)))
    for pass_no in range(1, passes + 1):
        if not missing:
            break
        before = len(missing)
        for batch in _chunks(missing, requery_size):
            found, diag = _query_bsj_realtime_expiry_batch(batch)
            profiles_found.update(found)
            diag["pass"] = f"REQUERY_{pass_no}"
            batch_diag.append(diag)
            if getattr(settings, "mass_expiry_scan_batch_delay_seconds", 0) > 0:
                time.sleep(settings.mass_expiry_scan_batch_delay_seconds)
        missing = [imei for imei in missing if imei not in profiles_found]
        if len(missing) == before:
            # No recovery in this pass. A second identical pass only adds load.
            break

    for imei in requested_list:
        profile = profiles_found.get(imei)
        if profile is not None:
            classification = classify_bsj_expiry(profile)
            results[imei] = {
                "imei": imei,
                **classification,
                "vehicle_id": profile.get("vehicle_id"),
                "terminal_type": profile.get("terminal_type"),
                "status_source": "GET_REAL_TIME_INFO",
            }
            continue

        inv = (inventory_profiles or {}).get(imei) or {}
        classification = classify_bsj_expiry(inv)
        reason = classification.get("reason")
        if classification.get("status") == "INVALID":
            reason = f"Not returned by /monitor/getRealTimeInfo after recovery pass; {reason}"
        results[imei] = {
            "imei": imei,
            **classification,
            "vehicle_id": inv.get("vehicle_id"),
            "terminal_type": inv.get("terminal_type"),
            "reason": reason,
            "status_source": "LIVE_INVENTORY_FALLBACK" if classification.get("status") != "INVALID" else "NO_USABLE_EVIDENCE",
        }

    counts: dict[str, int] = {"EXPIRED": 0, "ACTIVE": 0, "NOT_ACTIVATED": 0, "INVALID": 0}
    source_counts: dict[str, int] = {}
    for item in results.values():
        status = str(item.get("status") or "INVALID")
        counts[status] = counts.get(status, 0) + 1
        source = str(item.get("expiry_source") or item.get("status_source") or "UNKNOWN")
        source_counts[source] = source_counts.get(source, 0) + 1
    return results, {
        "requested": len(requested_list),
        "returned": len(results),
        "realtime_returned": len(profiles_found),
        "realtime_missing": len(requested_list) - len(profiles_found),
        "counts": counts,
        "source_counts": source_counts,
        "batches": batch_diag,
        "source": "BSJ /monitor/getRealTimeInfo expired=0/1/2 + smaller-batch recovery + merged live inventory fallback",
    }

def _json_body(resp: requests.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except Exception as exc:
        text = (resp.text or "")[:500]
        raise EngineApiError(f"BSJ Platform returned non-JSON response: {text}") from exc
    if not isinstance(body, dict):
        raise EngineApiError("BSJ Platform returned unexpected response format")
    return body


def login_bsj_web() -> BSJWebSession:
    # Always create a fresh BSJ web session. The SID is dynamic and must be
    # taken from the successful /webapi/user/login JSON response at data.sid.
    if not settings.bsj_web_username or not settings.bsj_web_password_payload:
        raise EngineApiError(
            "BSJ Platform Engine API is not configured. Set BSJ_WEB_USERNAME and "
            "BSJ_WEB_PASSWORD_PAYLOAD in .env."
        )

    url = f"{settings.bsj_web_base_url.rstrip('/')}/webapi/user/login"
    payload = {
        "userName": settings.bsj_web_username,
        "password": settings.bsj_web_password_payload,
    }
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Accept-Language": "en",
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=settings.bsj_web_timeout)
        resp.raise_for_status()
    except Exception as exc:
        raise EngineApiError(f"Unable to login to BSJ Platform: {exc}") from exc

    body = _json_body(resp)
    if int(body.get("code") or 0) != 200:
        raise EngineApiError(f"BSJ Platform login failed: {body.get('msg') or body.get('message') or body}")
    data = body.get("data") or {}
    if not isinstance(data, dict) or not data.get("sid"):
        raise EngineApiError("BSJ Platform login succeeded but response data.sid was missing")
    try:
        uid = int(data.get("userId")) if data.get("userId") is not None else None
    except Exception:
        uid = None
    # IMPORTANT: SID is read directly from the login response, for example:
    # {"code": 200, "data": {"sid": "U180-..."}}
    sid = str(data["sid"]).strip()
    return BSJWebSession(sid=sid, user_id=uid, user_name=str(data.get("userName") or settings.bsj_web_username))


def _auth_headers(session: BSJWebSession) -> dict[str, str]:
    # Browser traffic confirms Token=<login sid>. Extra sid/smuser are harmless and
    # improve compatibility across BSJ platform builds.
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Accept-Language": "en",
        "Token": session.sid,
        "sid": session.sid,
    }
    if session.user_name:
        headers["smuser"] = session.user_name
    return headers


def _extract_rows(body: dict[str, Any]) -> tuple[list[dict[str, Any]], int | None]:
    total = body.get("total")
    try:
        total_i = int(total) if total is not None else None
    except Exception:
        total_i = None

    data = body.get("data")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)], total_i
    if isinstance(data, dict):
        if total_i is None:
            for tk in ("total", "records", "count"):
                try:
                    if data.get(tk) is not None:
                        total_i = int(data.get(tk))
                        break
                except Exception:
                    pass
        for key in ("list", "rows", "records", "items", "content", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)], total_i
    return [], total_i


def _acc_detail_payload(vehicle_id: int, start_time: datetime, end_time: datetime, page: int, page_size: int) -> dict[str, Any]:
    """Exact payload observed from BSJ Platform Engine Status detail report.

    Browser request:
      POST /webapi/acc/getAccDetailReport
      {
        "startTime": "YYYY-MM-DD HH:mm:ss",
        "endTime": "YYYY-MM-DD HH:mm:ss",
        "vehicleId": 32770,
        "accStatus": -1,
        "pageNum": 1,
        "pageSize": 20
      }

    Important: this endpoint uses the internal numeric ``vehicleId`` rather than
    the device IMEI. The app resolves this numeric ID from the BSJ official API before calling the Engine Status report.
    """
    return {
        "startTime": start_time.strftime("%Y-%m-%d %H:%M:%S"),
        "endTime": end_time.strftime("%Y-%m-%d %H:%M:%S"),
        "vehicleId": int(vehicle_id),
        "accStatus": -1,
        "pageNum": page,
        "pageSize": page_size,
    }


def fetch_acc_report_raw(
    device_map: dict[str, int],
    start_time: datetime,
    end_time: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch Engine Status rows using the exact BSJ browser payload.

    ``device_map`` maps IMEI/device ID -> BSJ numeric vehicleId. The report is
    queried once per vehicleId and paginated with ``pageNum``/``pageSize``.
    """
    session = login_bsj_web()
    url = f"{settings.bsj_web_base_url.rstrip('/')}/webapi/acc/getAccDetailReport"
    page_size = max(1, settings.bsj_engine_page_size)
    max_pages = max(1, settings.bsj_engine_max_pages)

    all_rows: list[dict[str, Any]] = []
    pages_by_device: dict[str, int] = {}
    rows_by_device: dict[str, int] = {}
    totals_by_device: dict[str, int | None] = {}
    vehicle_ids: dict[str, int] = {}
    # V1.4.3: failures are isolated per device. One device with a bad/expired
    # vehicleId, a timeout, or an error response no longer aborts Engine Status
    # retrieval for every other device in the batch.
    failures: list[dict[str, Any]] = []

    for imei, vehicle_id_raw in device_map.items():
        try:
            vehicle_id = int(vehicle_id_raw)
        except Exception as exc:
            failures.append({"imei": str(imei), "vehicle_id": vehicle_id_raw, "error": f"Invalid/missing vehicleId: {exc}"})
            continue
        vehicle_ids[str(imei)] = vehicle_id

        page = 1
        device_rows: list[dict[str, Any]] = []
        total: int | None = None
        device_error: str | None = None

        while page <= max_pages:
            payload = _acc_detail_payload(vehicle_id, start_time, end_time, page, page_size)
            try:
                resp = requests.post(url, json=payload, headers=_auth_headers(session), timeout=settings.bsj_web_timeout)
                if resp.status_code in (401, 403):
                    # Session may have expired. Login again and use the NEW response data.sid.
                    session = login_bsj_web()
                    resp = requests.post(url, json=payload, headers=_auth_headers(session), timeout=settings.bsj_web_timeout)
                resp.raise_for_status()
                body = _json_body(resp)
                if int(body.get("code") or 0) != 200:
                    msg = body.get("msg") or body.get("message") or body
                    raise EngineApiError(f"code={body.get('code')}: {msg}")
            except Exception as exc:
                device_error = f"page {page}: {exc}"
                break

            rows, page_total = _extract_rows(body)
            if page_total is not None:
                total = page_total

            # The current response contains deviceId, but keep a safe fallback
            # because the request itself already identifies the device.
            for row in rows:
                if not any(row.get(k) not in (None, "") for k in ("deviceId", "device_id", "terminalNo", "imei", "deviceNo")):
                    row = dict(row)
                    row["deviceId"] = str(imei)
                device_rows.append(row)

            if not rows:
                break
            if total is not None:
                if len(device_rows) >= total:
                    break
            elif len(rows) < page_size:
                break
            page += 1

        if device_error:
            failures.append({"imei": str(imei), "vehicle_id": vehicle_id, "error": device_error})
            if not device_rows:
                # Nothing usable was retrieved for this device before it failed;
                # skip it entirely rather than recording a partial/misleading result.
                continue

        all_rows.extend(device_rows)
        pages_by_device[str(imei)] = page
        rows_by_device[str(imei)] = len(device_rows)
        totals_by_device[str(imei)] = total
        if settings.bsj_engine_request_delay_seconds > 0:
            time.sleep(settings.bsj_engine_request_delay_seconds)

    return all_rows, {
        "pages": sum(pages_by_device.values()),
        "rows": len(all_rows),
        "total": sum(v for v in totals_by_device.values() if isinstance(v, int)) if any(isinstance(v, int) for v in totals_by_device.values()) else None,
        "pages_by_device": pages_by_device,
        "rows_by_device": rows_by_device,
        "totals_by_device": totals_by_device,
        "vehicle_ids": vehicle_ids,
        "failures": failures,
        "user_id": session.user_id,
        "source": "/webapi/acc/getAccDetailReport",
        "request_mode": "one numeric vehicleId per request",
    }


def _first(row: dict[str, Any], keys: Iterable[str]) -> Any:
    if not isinstance(row, dict):
        return None
    lower = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
        value = lower.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, "", "-"):
        return None
    if isinstance(value, (int, float)):
        # Accept epoch seconds or milliseconds.
        val = float(value)
        if val > 10_000_000_000:
            val /= 1000.0
        try:
            return datetime.fromtimestamp(val)
        except Exception:
            return None
    text = str(value).strip().replace("T", " ")
    if text.endswith("Z"):
        text = text[:-1]
    if len(text) >= 6 and text[-6] in "+-" and text[-3] == ":":
        text = text[:-6]
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def _status(row: dict[str, Any]) -> str | None:
    # Exact current BSJ Engine Status response uses isAcc where 0 = ON and 1 = OFF.
    # This matches BSJ's documented ACC convention (accState 0=On, 1=Off).
    is_acc = _first(row, ["isAcc", "is_acc"])
    if is_acc is not None:
        text = str(is_acc).strip().upper()
        if text in {"0", "FALSE"}:
            return "ON"
        if text in {"1", "TRUE"}:
            return "OFF"

    # Compatibility fallbacks for older/newer platform builds.
    value = _first(row, ["engineStatus", "engine_status", "accStatus", "acc_status", "status", "state", "accState", "acc_state", "acc"])
    if value is None:
        return None
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    text = str(value).strip().upper()
    if text in {"ON", "ENGINE ON", "ACC ON", "TRUE"}:
        return "ON"
    if text in {"OFF", "ENGINE OFF", "ACC OFF", "FALSE"}:
        return "OFF"
    keyset = {str(k).lower() for k in row.keys()}
    if "accstate" in keyset or "acc_state" in keyset:
        if text == "0":
            return "ON"
        if text == "1":
            return "OFF"
    return None


def normalize_engine_events(rows: list[dict[str, Any]], requested_imeis: list[str]) -> list[dict[str, Any]]:
    requested = set(requested_imeis)
    events: list[dict[str, Any]] = []
    for row in rows:
        imei = str(_first(row, ["deviceId", "device_id", "terminalNo", "terminal_no", "imei", "deviceNo", "device_no"]) or "").strip()
        if not imei or imei not in requested:
            # Fallback: find a scalar value equal to one of the requested IMEIs.
            for value in row.values():
                text = str(value).strip() if value is not None else ""
                if text in requested:
                    imei = text
                    break
        if not imei or imei not in requested:
            continue
        event_time = _parse_datetime(_first(row, ["stateChangeTime", "state_change_time", "time", "eventTime", "event_time", "accTime", "acc_time", "devTime", "dev_time", "alarmTime", "alarm_time", "createTime", "created_on", "createdOn", "startTime"]))
        if not event_time:
            for value in row.values():
                event_time = _parse_datetime(value)
                if event_time:
                    break
        status = _status(row)
        if not status:
            for value in row.values():
                text = str(value).strip().upper() if value is not None else ""
                if text in {"ON", "ENGINE ON", "ACC ON"}:
                    status = "ON"
                    break
                if text in {"OFF", "ENGINE OFF", "ACC OFF"}:
                    status = "OFF"
                    break
        if not event_time or not status:
            continue
        events.append({
            "imei": imei,
            "device_name": str(_first(row, ["deviceName", "device_name", "plate"]) or ""),
            "group": str(_first(row, ["group", "groupName", "group_name"]) or ""),
            "time": event_time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": status,
            "location": str(_first(row, ["position", "location", "address", "lonAndLat", "lon_and_lat"]) or ""),
            "mileage": _first(row, ["currentMileage", "current_mileage", "mileage", "mileageKm", "mileage_km"]),
            "speed": _first(row, ["speed", "speedKmH", "speed_kmh"]),
            "raw": row,
        })
    events.sort(key=lambda x: (x["imei"], x["time"], 0 if x["status"] == "ON" else 1))
    return events


def build_engine_intervals(
    events: list[dict[str, Any]], imeis: list[str], date_text: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    day_start = datetime.strptime(date_text, "%Y-%m-%d")
    day_end = day_start + timedelta(days=1)
    now = datetime.now()
    clip_end = min(day_end, now) if day_start.date() == now.date() else day_end

    by_imei: dict[str, list[dict[str, Any]]] = {i: [] for i in imeis}
    for event in events:
        if event["imei"] in by_imei:
            by_imei[event["imei"]].append(event)

    intervals: list[dict[str, Any]] = []
    # V1.4.4: BSJ ACC data is a state-change report. Therefore a valid ON -> OFF
    # pair is trusted for coverage regardless of how many hours elapsed between
    # the two events. Long spans must not be silently discarded from Engine ON
    # duration. An optional max-silence setting may still *flag* a long span for
    # review, but the interval remains included in coverage math.
    unverified_intervals: list[dict[str, Any]] = []
    max_silence_seconds = settings.bsj_engine_max_silence_hours * 3600 if settings.bsj_engine_max_silence_hours > 0 else None
    event_export: dict[str, list[dict[str, Any]]] = {i: [] for i in imeis}

    def _emit(imei: str, start: datetime, end: datetime) -> None:
        if end <= start:
            return
        duration = int((end - start).total_seconds())
        record = {
            "imei": imei,
            "engine_on": start.strftime("%Y-%m-%d %H:%M:%S"),
            "engine_off": end.strftime("%Y-%m-%d %H:%M:%S"),
            "duration_seconds": duration,
        }
        intervals.append(record)
        if max_silence_seconds is not None and duration > max_silence_seconds:
            warning = dict(record)
            warning["reason"] = (
                f"Engine ON span exceeds {settings.bsj_engine_max_silence_hours:g}h without another ACC state change. "
                "Flagged for review only; it is still INCLUDED in Engine ON duration and coverage calculation."
            )
            unverified_intervals.append(warning)

    for imei in imeis:
        seq = sorted(by_imei[imei], key=lambda x: x["time"])
        # Remove consecutive duplicate states; retain the latest state before the day start.
        dedup: list[dict[str, Any]] = []
        for e in seq:
            if dedup and dedup[-1]["status"] == e["status"]:
                # Keep both in raw export, but state machine only needs one. For pre-day
                # duplicates, latest is more representative; for same-day duplicates, first
                # transition time is retained to avoid inflating ON duration.
                if _parse_datetime(e["time"]) and _parse_datetime(e["time"]) < day_start:
                    dedup[-1] = e
                continue
            dedup.append(e)

        state = "OFF"
        open_on: datetime | None = None
        for e in dedup:
            t = _parse_datetime(e["time"])
            if not t:
                continue
            if t < day_start:
                state = e["status"]
                continue
            if t >= clip_end:
                break
            event_export[imei].append({k: v for k, v in e.items() if k != "raw"})
            if e["status"] == "ON":
                if state != "ON":
                    open_on = max(t, day_start)
                state = "ON"
            else:
                if state == "ON":
                    start = open_on or day_start
                    end = min(t, clip_end)
                    _emit(imei, start, end)
                state = "OFF"
                open_on = None

        # Device was already ON at midnight based on the latest lookback state.
        if state == "ON" and open_on is None:
            # If no same-day OFF ever appeared this means ON throughout the checked window.
            open_on = day_start
        if state == "ON" and open_on is not None and clip_end > open_on:
            _emit(imei, open_on, clip_end)

    intervals.sort(key=lambda x: (x["imei"], x["engine_on"]))
    unverified_intervals.sort(key=lambda x: (x["imei"], x["engine_on"]))
    return intervals, unverified_intervals, event_export


def fetch_engine_data(device_map: dict[str, int], date_text: str) -> dict[str, Any]:
    imeis = list(device_map.keys())
    day_start = datetime.strptime(date_text, "%Y-%m-%d")
    # Look back so an OFF event early in the selected day can inherit an ON state from before midnight.
    query_start = day_start - timedelta(days=max(0, settings.bsj_engine_lookback_days))
    day_end = day_start + timedelta(days=1) - timedelta(seconds=1)
    now = datetime.now()
    query_end = min(day_end, now) if day_start.date() == now.date() else day_end

    raw_rows, diagnostics = fetch_acc_report_raw(device_map, query_start, query_end)
    normalized = normalize_engine_events(raw_rows, imeis)
    intervals, unverified_intervals, per_device_events = build_engine_intervals(normalized, imeis, date_text)
    events = [e for imei in imeis for e in per_device_events.get(imei, [])]
    return {
        "events": events,
        "intervals": intervals,
        "unverified_intervals": unverified_intervals,
        "raw_count": len(raw_rows),
        "normalized_count": len(normalized),
        "diagnostics": diagnostics,
    }


def fetch_acc_device_raw(
    imei: str,
    vehicle_id_raw: int,
    start_time: datetime,
    end_time: datetime,
    session: BSJWebSession | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch Engine Status for exactly one device.

    V1.7.4 scheduler helper. Independent device requests can run in a bounded
    ThreadPoolExecutor while reusing one login SID for the wave. Transient HTTP
    throttling/server errors and BSJ 503/505 responses use the same bounded retry
    policy as the official API.
    """
    try:
        vehicle_id = int(vehicle_id_raw)
    except Exception as exc:
        raise EngineApiError(f"Invalid/missing vehicleId for {imei}: {exc}") from exc

    active_session = session or login_bsj_web()
    url = f"{settings.bsj_web_base_url.rstrip('/')}/webapi/acc/getAccDetailReport"
    page_size = max(1, settings.bsj_engine_page_size)
    max_pages = max(1, settings.bsj_engine_max_pages)
    retries = max(1, settings.bsj_api_max_retries)
    page = 1
    device_rows: list[dict[str, Any]] = []
    total: int | None = None

    while page <= max_pages:
        payload = _acc_detail_payload(vehicle_id, start_time, end_time, page, page_size)
        body: dict[str, Any] | None = None
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                _pace_engine_api()
                resp = requests.post(url, json=payload, headers=_auth_headers(active_session), timeout=settings.bsj_web_timeout)
                if resp.status_code in (401, 403):
                    active_session = login_bsj_web()
                    _pace_engine_api()
                    resp = requests.post(url, json=payload, headers=_auth_headers(active_session), timeout=settings.bsj_web_timeout)
                if resp.status_code in (429, 502, 503, 504):
                    if attempt < retries:
                        time.sleep(_retry_delay(attempt))
                        continue
                    raise EngineApiError(f"HTTP {resp.status_code}: {(resp.text or '')[:300]}")
                resp.raise_for_status()
                candidate = _json_body(resp)
                code = int(candidate.get("code") or 0)
                if code in (503, 505):
                    msg = str(candidate.get("msg") or candidate.get("message") or candidate)[:500]
                    if attempt < retries:
                        time.sleep(_retry_delay(attempt))
                        continue
                    raise EngineApiError(f"BSJ code={code}: {msg}")
                if code != 200:
                    msg = candidate.get("msg") or candidate.get("message") or candidate
                    raise EngineApiError(f"code={candidate.get('code')}: {msg}")
                body = candidate
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(_retry_delay(attempt))

        if body is None:
            raise EngineApiError(f"Engine request failed for {imei} page {page}: {last_error or 'unknown error'}")

        rows, page_total = _extract_rows(body)
        if page_total is not None:
            total = page_total
        for row in rows:
            if not any(row.get(k) not in (None, "") for k in ("deviceId", "device_id", "terminalNo", "imei", "deviceNo")):
                row = dict(row)
                row["deviceId"] = str(imei)
            device_rows.append(row)

        if not rows:
            break
        if total is not None and len(device_rows) >= total:
            break
        if total is None and len(rows) < page_size:
            break
        page += 1

    return device_rows, {
        "imei": str(imei),
        "vehicle_id": vehicle_id,
        "pages": page,
        "rows": len(device_rows),
        "total": total,
        "user_id": active_session.user_id,
        "source": "/webapi/acc/getAccDetailReport",
    }
