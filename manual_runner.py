from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import threading
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend.app.checker import parse_imeis, run_check
from backend.app.jobs import Job, JobContext
from backend.app.report import export_result


def _decode_payload(payload: str, token: str) -> dict:
    raw = payload.strip()
    raw += "=" * (-len(raw) % 4)
    encrypted = base64.urlsafe_b64decode(raw.encode("ascii"))
    if len(encrypted) < 13:
        raise RuntimeError("Invalid encrypted request payload")
    iv, ciphertext = encrypted[:12], encrypted[12:]
    key = hashlib.sha256(token.encode("utf-8")).digest()
    plaintext = AESGCM(key).decrypt(iv, ciphertext, None)
    data = json.loads(plaintext.decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("Invalid request payload")
    return data


def main() -> None:
    request_id = os.environ.get("REQUEST_ID", "").strip()
    encrypted_payload = os.environ.get("REQUEST_PAYLOAD", "").strip()
    github_token = os.environ.get("MANUAL_GITHUB_TOKEN", "").strip()
    if not request_id or not encrypted_payload or not github_token:
        raise RuntimeError("Missing REQUEST_ID, REQUEST_PAYLOAD, or MANUAL_GITHUB_TOKEN")

    request = _decode_payload(encrypted_payload, github_token)
    imeis = parse_imeis(request.get("imeis") or "")
    date_text = str(request.get("date") or "").strip()

    output_dir = Path("output")
    output_dir.mkdir(parents=True, exist_ok=True)

    job = Job(id=request_id)
    ctx = JobContext(job, threading.Lock())
    result = run_check(ctx, environment="Production", imeis=imeis, date_text=date_text)

    (output_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (output_dir / "request_meta.json").write_text(
        json.dumps(
            {
                "request_id": request_id,
                "date": date_text,
                "device_count": len(imeis),
                "wake_rule": "Only confirmed realtime ACC OFF (accState=1) is woken; ACC ON (accState=0) goes directly to File List.",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    excel_path = export_result(request_id, result)
    shutil.copy2(excel_path, output_dir / "result.xlsx")

    collection = result.get("collection") or {}
    print(
        "Completed",
        request_id,
        "devices=", len(imeis),
        "acc_on=", len(collection.get("current_acc_on") or []),
        "acc_off=", len(collection.get("current_acc_off") or []),
        "wake_requests=", collection.get("wake_requests_total"),
    )


if __name__ == "__main__":
    main()
