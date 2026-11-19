"""Atomic canonical persistence for retained live evidence receipts."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from .live_evidence import RetainedLiveEvidence
from .live_news_evidence import (
    LiveNewsEvidenceManifest,
    PublicNewsEvidenceProjection,
)


class LiveEvidenceStore:
    def write(self, evidence: RetainedLiveEvidence, target: Path) -> str:
        payload = evidence.canonical_bytes() + b"\n"
        digest = hashlib.sha256(payload).hexdigest()
        _atomic_write(target, payload)
        _atomic_write(
            target.with_suffix(target.suffix + ".sha256"), f"{digest}\n".encode()
        )
        return digest

    def read(self, target: Path) -> RetainedLiveEvidence:
        payload = target.read_bytes()
        sidecar = target.with_suffix(target.suffix + ".sha256")
        if hashlib.sha256(payload).hexdigest() != sidecar.read_text().strip():
            raise ValueError("retained live evidence file checksum does not match")
        evidence = RetainedLiveEvidence.model_validate_json(payload)
        if payload != evidence.canonical_bytes() + b"\n":
            raise ValueError("retained live evidence bytes are not canonical")
        return evidence


class LiveNewsEvidenceStore:
    def write_private(self, manifest: LiveNewsEvidenceManifest, target: Path) -> str:
        return _write_canonical(manifest.canonical_bytes(), target)

    def read_private(self, target: Path) -> LiveNewsEvidenceManifest:
        return _read_canonical(target, LiveNewsEvidenceManifest)

    def write_public(
        self, projection: PublicNewsEvidenceProjection, target: Path
    ) -> str:
        return _write_canonical(projection.canonical_bytes(), target)

    def read_public(self, target: Path) -> PublicNewsEvidenceProjection:
        return _read_canonical(target, PublicNewsEvidenceProjection)


def _write_canonical(payload: bytes, target: Path) -> str:
    document = payload + b"\n"
    digest = hashlib.sha256(document).hexdigest()
    _atomic_write(target, document)
    _atomic_write(target.with_suffix(target.suffix + ".sha256"), f"{digest}\n".encode())
    return digest


def _read_canonical(target: Path, model):
    payload = target.read_bytes()
    sidecar = target.with_suffix(target.suffix + ".sha256")
    if hashlib.sha256(payload).hexdigest() != sidecar.read_text().strip():
        raise ValueError("news evidence file checksum does not match")
    artifact = model.model_validate_json(payload)
    if payload != artifact.canonical_bytes() + b"\n":
        raise ValueError("news evidence bytes are not canonical")
    return artifact


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


__all__ = ["LiveEvidenceStore", "LiveNewsEvidenceStore"]
