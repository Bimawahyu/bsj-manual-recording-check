from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except Exception:
        return default


_load_env(ENV_PATH)


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "BSJ Recording Reliability Scheduler V1.7.5 D1 Only")
    app_host: str = os.getenv("APP_HOST", "127.0.0.1")
    app_port: int = _int("APP_PORT", 8010)

    # Remote GitHub Pages manual-check frontend. BSJ credentials remain server-side.
    remote_dashboard_api_token: str = os.getenv("REMOTE_DASHBOARD_API_TOKEN", "").strip()
    remote_dashboard_allowed_origins: str = os.getenv(
        "REMOTE_DASHBOARD_ALLOWED_ORIGINS", "https://bimawahyu.github.io"
    ).strip()

    # BSJ official API: IMEI mapping, wake-up, and video file-list.
    bsj_api_base_url: str = os.getenv("BSJ_API_BASE_URL", "https://y.gpstracktech.com/api").strip()
    bsj_api_key: str = os.getenv("BSJ_API_KEY", "").strip()
    bsj_api_version: str = os.getenv("BSJ_API_VERSION", "1.0").strip() or "1.0"
    bsj_api_language: str = os.getenv("BSJ_API_LANGUAGE", "en").strip() or "en"
    bsj_api_timeout: int = _int("BSJ_API_TIMEOUT_SECONDS", 45)
    bsj_api_mapping_batch_size: int = _int("BSJ_API_MAPPING_BATCH_SIZE", 100)
    bsj_api_max_workers: int = _int("BSJ_API_MAX_WORKERS", 5)
    bsj_api_max_retries: int = _int("BSJ_API_MAX_RETRIES", 3)
    bsj_api_retry_backoff_seconds: float = _float("BSJ_API_RETRY_BACKOFF_SECONDS", 1.0)
    bsj_api_retry_backoff_max_seconds: float = _float("BSJ_API_RETRY_BACKOFF_MAX_SECONDS", 20.0)
    bsj_official_min_interval_seconds: float = _float("BSJ_OFFICIAL_MIN_INTERVAL_SECONDS", 0.0)

    # Live BSJ VOD traffic was verified to send the selected calendar day directly
    # (00:00:00 -> 23:59:59) without a UTC-7 shift. Keep UTC mode available only
    # as an explicit fallback for other deployments.
    app_utc_offset_hours: int = _int("APP_UTC_OFFSET_HOURS", 7)
    bsj_filelist_request_time_mode: str = os.getenv("BSJ_FILELIST_REQUEST_TIME_MODE", "LOCAL").strip().upper() or "LOCAL"
    bsj_filelist_response_time_mode: str = os.getenv("BSJ_FILELIST_RESPONSE_TIME_MODE", "LOCAL").strip().upper() or "LOCAL"

    # Device wake before video-resource query.
    wake_wait_seconds: int = _int("RECORDING_CHECKER_WAKE_WAIT_SECONDS", 60)
    gap_threshold_seconds: int = _int("RECORDING_CHECKER_GAP_THRESHOLD_SECONDS", 60)

    # BSJ Platform web API: actual Engine ON/OFF report.
    bsj_web_base_url: str = os.getenv("BSJ_WEB_BASE_URL", "https://y.gpstracktech.com").strip()
    bsj_web_username: str = os.getenv("BSJ_WEB_USERNAME", "").strip()
    # Exact password payload observed in DevTools login request (not plaintext).
    bsj_web_password_payload: str = os.getenv("BSJ_WEB_PASSWORD_PAYLOAD", "").strip()
    bsj_web_timeout: int = _int("BSJ_WEB_TIMEOUT_SECONDS", 45)
    bsj_engine_page_size: int = _int("BSJ_ENGINE_PAGE_SIZE", 100)
    bsj_engine_max_pages: int = _int("BSJ_ENGINE_MAX_PAGES", 100)
    bsj_engine_lookback_days: int = _int("BSJ_ENGINE_LOOKBACK_DAYS", 1)
    bsj_engine_request_delay_seconds: float = _float("BSJ_ENGINE_REQUEST_DELAY_SECONDS", 0.15)
    bsj_engine_min_interval_seconds: float = _float("BSJ_ENGINE_MIN_INTERVAL_SECONDS", 0.0)
    # Optional diagnostic only. 0 disables long-span warnings. If set >0, an
    # Engine ON interval longer than this is flagged for review but is NOT
    # excluded from Engine ON duration or coverage calculation.
    bsj_engine_max_silence_hours: float = _float("BSJ_ENGINE_MAX_SILENCE_HOURS", 0)


    # Automatic recording-reliability scheduler. It continuously processes all
    # devices registered under the BSJ account and refreshes only the previous full day (D-1).
    recording_scheduler_enabled: bool = os.getenv("RECORDING_SCHEDULER_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    # V1.7.5 uses a continuous parallel wave pipeline for D-1 only rather than a 5-minute
    # blocking batch loop. Wake and Engine requests overlap; File List starts
    # after the configured wake wait. Worker counts are deliberately bounded.
    recording_scheduler_interval_minutes: int = _int("RECORDING_SCHEDULER_INTERVAL_MINUTES", 5)  # retained for UI/backward compatibility
    recording_scheduler_batch_size: int = _int("RECORDING_SCHEDULER_BATCH_SIZE", 100)
    recording_scheduler_min_batch_size: int = _int("RECORDING_SCHEDULER_MIN_BATCH_SIZE", 80)
    recording_scheduler_max_batch_size: int = _int("RECORDING_SCHEDULER_MAX_BATCH_SIZE", 120)
    recording_scheduler_api_workers: int = _int("RECORDING_SCHEDULER_API_WORKERS", 6)  # legacy alias / default
    recording_scheduler_wake_workers: int = _int("RECORDING_SCHEDULER_WAKE_WORKERS", 8)
    recording_scheduler_file_workers: int = _int("RECORDING_SCHEDULER_FILE_WORKERS", 12)
    recording_scheduler_engine_workers: int = _int("RECORDING_SCHEDULER_ENGINE_WORKERS", 8)
    recording_scheduler_global_workers: int = _int("RECORDING_SCHEDULER_GLOBAL_MAX_INFLIGHT", 16)
    recording_scheduler_wave_cooldown_seconds: float = _float("RECORDING_SCHEDULER_WAVE_COOLDOWN_SECONDS", 1.0)
    recording_scheduler_filelist_range_enabled: bool = False  # D-1-only build uses one daily File List request/device
    recording_scheduler_filelist_range_fallback_daily: bool = os.getenv("RECORDING_SCHEDULER_FILELIST_RANGE_FALLBACK_DAILY", "true").strip().lower() in {"1", "true", "yes", "on"}
    recording_scheduler_lookback_days: int = 1  # fixed by design in the D-1-only build
    recording_scheduler_device_delay_seconds: float = _float("RECORDING_SCHEDULER_DEVICE_DELAY_SECONDS", 0.0)
    recording_scheduler_max_device_attempts: int = _int("RECORDING_SCHEDULER_MAX_DEVICE_ATTEMPTS", 3)
    recording_scheduler_mass_pause_seconds: int = _int("RECORDING_SCHEDULER_MASS_PAUSE_SECONDS", 60)

    # Public Metabase master: IMEI -> Domain + actual installed channels.
    device_master_metabase_base_url: str = os.getenv("DEVICE_MASTER_METABASE_BASE_URL", "https://dashboard.mceasy.com").strip()
    device_master_metabase_uuid: str = os.getenv("DEVICE_MASTER_METABASE_UUID", "f40b08ac-b945-4469-beb2-de76b47e75b3").strip()
    device_master_timeout_seconds: int = _int("DEVICE_MASTER_TIMEOUT_SECONDS", 60)

    # Destructive device administration: delete current BSJ vehicle record and
    # re-register the same terminal using a live configuration snapshot.
    device_reregister_wait_seconds: float = _float("DEVICE_REREGISTER_WAIT_SECONDS", 2.0)
    device_reregister_add_retries: int = _int("DEVICE_REREGISTER_ADD_RETRIES", 3)
    device_reregister_retry_delay_seconds: float = _float("DEVICE_REREGISTER_RETRY_DELAY_SECONDS", 2.0)

    # Persistent mass re-registration service. V1.7.4 no longer uses a bundled
    # manifest: every expiry scan starts from the current BSJ live inventory.
    mass_job_name: str = os.getenv("MASS_JOB_NAME", "BSJ live expired-device re-registration")
    mass_chunk_size: int = _int("MASS_CHUNK_SIZE", 25)
    mass_device_delay_seconds: float = _float("MASS_DEVICE_DELAY_SECONDS", 1.0)
    mass_chunk_cooldown_seconds: float = _float("MASS_CHUNK_COOLDOWN_SECONDS", 20.0)
    mass_preflight_retries: int = _int("MASS_PREFLIGHT_RETRIES", 3)
    mass_preflight_retry_delay_seconds: float = _float("MASS_PREFLIGHT_RETRY_DELAY_SECONDS", 3.0)
    mass_preflight_device_delay_seconds: float = _float("MASS_PREFLIGHT_DEVICE_DELAY_SECONDS", 0.2)
    # Expiry gate for destructive mass re-registration. A cached scan is only
    # accepted for a limited time, and each device is revalidated again during
    # preflight and immediately before deletion.
    mass_expiry_scan_max_age_minutes: int = _int("MASS_EXPIRY_SCAN_MAX_AGE_MINUTES", 30)
    # The BSJ documentation defines /monitor/getRealTimeInfo queryParams as an
    # array and exposes `expired` as 0=Not expired, 1=Expired, 2=Not activated.
    # V1.7.4 performs a normal batch pass, then re-queries omitted terminals in
    # smaller batches before falling back to current inventory expireDate.
    mass_expiry_query_batch_size: int = _int("MASS_EXPIRY_QUERY_BATCH_SIZE", 50)
    mass_expiry_requery_batch_size: int = _int("MASS_EXPIRY_REQUERY_BATCH_SIZE", 10)
    mass_expiry_requery_passes: int = _int("MASS_EXPIRY_REQUERY_PASSES", 2)
    mass_expiry_scan_batch_delay_seconds: float = _float("MASS_EXPIRY_SCAN_BATCH_DELAY_SECONDS", 0.25)
    mass_recovery_max_attempts: int = _int("MASS_RECOVERY_MAX_ATTEMPTS", 10)
    mass_recovery_retry_delay_seconds: float = _float("MASS_RECOVERY_RETRY_DELAY_SECONDS", 30.0)


    @property
    def recording_runtime_dir(self) -> Path:
        p = PROJECT_ROOT / "runtime" / "recording_scheduler"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def recording_db_path(self) -> Path:
        return self.recording_runtime_dir / "recording_scheduler.sqlite3"

    @property
    def recording_export_dir(self) -> Path:
        p = self.recording_runtime_dir / "exports"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def device_backup_dir(self) -> Path:
        p = PROJECT_ROOT / "runtime" / "device_backups"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def export_dir(self) -> Path:
        p = PROJECT_ROOT / "runtime" / "exports"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def mass_runtime_dir(self) -> Path:
        p = PROJECT_ROOT / "runtime" / "mass_service"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def mass_db_path(self) -> Path:
        return self.mass_runtime_dir / "mass_jobs.sqlite3"

    @property
    def mass_expiry_scan_path(self) -> Path:
        return self.mass_runtime_dir / "expiry_scan.json"

    @property
    def static_dir(self) -> Path:
        return PROJECT_ROOT / "backend" / "app" / "static"


settings = Settings()
