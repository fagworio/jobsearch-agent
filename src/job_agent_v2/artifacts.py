"""Artefatos locais que podem ser entregues à extensão."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
from pathlib import Path


# Native Messaging has a 4 MiB frame limit in the V2 transport. Leave room
# for base64 expansion and the surrounding JSON envelope.
MAX_RESUME_BYTES = 2_500_000


class ArtifactError(ValueError):
    """Currículo ausente, grande demais ou com tipo inválido."""


@dataclass(frozen=True)
class ResumeArtifact:
    path: str
    filename: str
    mime_type: str
    sha256: str
    size: int
    bytes_base64: str

    @classmethod
    def from_path(cls, path: str | Path) -> "ResumeArtifact":
        source = Path(path)
        if not source.is_file():
            raise ArtifactError("resume file does not exist")
        data = source.read_bytes()
        if len(data) > MAX_RESUME_BYTES:
            raise ArtifactError("resume exceeds the maximum size")
        if source.suffix.casefold() != ".pdf" or not data.startswith(b"%PDF-"):
            raise ArtifactError("resume must be a PDF")
        return cls(
            path=str(source),
            filename=source.name,
            mime_type="application/pdf",
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            bytes_base64=base64.b64encode(data).decode("ascii"),
        )

    def to_payload(self, field_id: str = "resume") -> dict[str, object]:
        if not field_id.strip():
            raise ArtifactError("resume field_id must not be empty")
        return {
            "field_id": field_id,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "sha256": self.sha256,
            "bytes_base64": self.bytes_base64,
        }
