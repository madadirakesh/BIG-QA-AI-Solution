"""
Evidence storage. The engine only depends on EvidenceStore; LocalEvidenceStore
is the default. An object-storage backend implements the same three methods
(put / get / delete_run) and is selected in service.build_evidence_store().
"""
from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from abc import ABC, abstractmethod
from typing import Optional

from .models import Evidence, utcnow_iso
from .security import sanitize_filename, safe_join


class EvidenceStore(ABC):
    @abstractmethod
    def put(self, run_id: str, test_key: str, step_number: Optional[int], status: str, data: bytes,
            label: str = "") -> Evidence: ...

    @abstractmethod
    def get(self, run_id: str, path: str) -> Optional[bytes]: ...

    @abstractmethod
    def delete_run(self, run_id: str) -> None: ...

    def is_duplicate(self, run_id: str, test_key: str, data: bytes) -> bool:
        """True when the screenshot is byte-identical to the previous one of the same test."""
        digest = hashlib.sha256(data).hexdigest()
        key = (run_id, test_key)
        if not hasattr(self, "_last_digest"):
            self._last_digest: dict[tuple[str, str], str] = {}
        dup = self._last_digest.get(key) == digest
        self._last_digest[key] = digest
        return dup


def evidence_filename(test_key: str, step_number: Optional[int], status: str, digest: str) -> str:
    """Descriptive and collision resistant: TC-01_step03_failed_20261023T084100_ab12cd34.png"""
    stamp = utcnow_iso().replace("-", "").replace(":", "").replace("+0000", "")[:15]
    step = "final" if step_number is None else f"step{step_number:02d}"
    return f"{sanitize_filename(test_key, 'test')}_{step}_{sanitize_filename(status.lower().replace(' ', '-'))}_{stamp}_{digest[:8]}.png"


class LocalEvidenceStore(EvidenceStore):
    def __init__(self, root: str):
        self.root = os.path.realpath(root)
        os.makedirs(self.root, exist_ok=True)

    def put(self, run_id, test_key, step_number, status, data, label=""):
        digest = hashlib.sha256(data).hexdigest()
        run_dir = safe_join(self.root, sanitize_filename(run_id))
        os.makedirs(run_dir, exist_ok=True)
        name = evidence_filename(test_key, step_number, status, digest + uuid.uuid4().hex)
        with open(safe_join(run_dir, name), "wb") as f:
            f.write(data)
        return Evidence(
            evidence_id=uuid.uuid4().hex[:12], test_key=test_key, step_number=step_number, status=status,
            captured_at=utcnow_iso(), path=f"{sanitize_filename(run_id)}/{name}", size_bytes=len(data), label=label,
        )

    def get(self, run_id, path):
        try:
            full = safe_join(self.root, path)
        except ValueError:
            return None
        if not full.startswith(safe_join(self.root, sanitize_filename(run_id))) or not os.path.isfile(full):
            return None
        with open(full, "rb") as f:
            return f.read()

    def delete_run(self, run_id):
        shutil.rmtree(safe_join(self.root, sanitize_filename(run_id)), ignore_errors=True)
