from __future__ import annotations

import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable


@dataclass
class Job:
    id: str
    status: str = "queued"
    stage: str = "queued"
    message: str = "Queued"
    current: int = 0
    total: int = 0
    result: Any = None
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    export_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class JobContext:
    def __init__(self, job: Job, lock: threading.Lock):
        self.job = job
        self.lock = lock

    def update(self, **kwargs: Any) -> None:
        with self.lock:
            for key, value in kwargs.items():
                setattr(self.job, key, value)


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=4)

    def submit(self, fn: Callable[..., Any], **kwargs: Any) -> Job:
        job = Job(id=uuid.uuid4().hex)
        with self.lock:
            self.jobs[job.id] = job
        self.pool.submit(self._run, job, fn, kwargs)
        return job

    def _run(self, job: Job, fn: Callable[..., Any], kwargs: dict[str, Any]) -> None:
        ctx = JobContext(job, self.lock)
        ctx.update(status="running", stage="starting", message="Starting")
        try:
            result = fn(ctx, **kwargs)
            ctx.update(status="completed", stage="completed", message="Completed", result=result)
        except Exception as exc:
            ctx.update(status="failed", stage="failed", message="Failed", error=f"{exc}\n{traceback.format_exc(limit=3)}")

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            return self.jobs.get(job_id)


job_manager = JobManager()
