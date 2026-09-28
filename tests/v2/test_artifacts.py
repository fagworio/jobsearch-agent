from __future__ import annotations

import hashlib

import pytest

from job_agent_v2.artifacts import ArtifactError, ResumeArtifact


def test_pdf_artifact_is_hashed_and_encoded(tmp_path):
    path = tmp_path / "resume.pdf"
    data = b"%PDF-1.7\nminimal fixture\n"
    path.write_bytes(data)
    artifact = ResumeArtifact.from_path(path)
    assert artifact.sha256 == hashlib.sha256(data).hexdigest()
    assert artifact.mime_type == "application/pdf"
    assert artifact.to_payload()["field_id"] == "resume"


def test_artifact_rejects_non_pdf(tmp_path):
    path = tmp_path / "resume.txt"
    path.write_text("not a pdf", encoding="utf-8")
    with pytest.raises(ArtifactError, match="must be a PDF"):
        ResumeArtifact.from_path(path)
