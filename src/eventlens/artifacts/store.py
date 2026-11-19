"""Atomic filesystem persistence for validated artifact envelopes."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from .models import ArtifactEnvelope


class AtomicArtifactStore:
    """Persist one envelope with same-directory temp writes and fsync."""

    def write(self, envelope: ArtifactEnvelope, target: Path) -> str:
        payload = envelope.canonical_bytes() + b"\n"
        digest = hashlib.sha256(payload).hexdigest()
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, target)
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        return digest

    def read(self, target: Path) -> ArtifactEnvelope:
        payload = target.read_bytes()
        envelope = ArtifactEnvelope.model_validate_json(payload)
        if hashlib.sha256(payload).hexdigest() != hashlib.sha256(envelope.canonical_bytes() + b"\n").hexdigest():
            raise ValueError("artifact bytes are not canonical")
        if not envelope.verify_payload_checksum():
            raise ValueError("artifact payload checksum does not match envelope")
        return envelope


__all__ = ["AtomicArtifactStore"]
