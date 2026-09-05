"""Append-only storage for source rows rejected before reconciliation."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

from .source_contracts import mask_pii, sha256_hex


class QuarantineStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._seen = self._load_ids()

    def _load_ids(self) -> set[str]:
        if not os.path.exists(self.path):
            return set()
        with open(self.path, encoding="utf-8") as fh:
            return {
                json.loads(line)["quarantine_id"]
                for line in fh
                if line.strip()
            }

    def append(self, source: str, row: object, error_code: str, detail: str) -> str:
        raw_hash = sha256_hex(row)
        quarantine_id = sha256_hex({
            "source": source,
            "raw_hash": raw_hash,
            "error_code": error_code,
        })
        record = {
            "quarantine_id": quarantine_id,
            "source": source,
            "raw_hash": raw_hash,
            "error_code": error_code,
            "detail": detail,
            "received_at": datetime.now(timezone.utc).isoformat(),
            # Malformed scalar/list payloads may contain PII but have no schema
            # whose fields can be masked safely. Persist only their type; the
            # raw hash still supports deduplication without echoing content.
            "row": mask_pii(row) if isinstance(row, dict)
                   else {"malformed_type": type(row).__name__},
        }
        with self._lock:
            if quarantine_id in self._seen:
                return quarantine_id
            parent = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(parent, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            self._seen.add(quarantine_id)
        return quarantine_id
