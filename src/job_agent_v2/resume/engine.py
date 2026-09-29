"""Orquestra estratégia, seleção, validação, renderização e cache por vaga."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .generator import generate_document
from .models import ResumeBuild, ResumeValidation
from .profile import load_profile
from .renderer import render_docx, render_pdf_from_docx, render_text
from .selector import select_fact_ids
from .strategy import build_strategy
from .validator import validate_document

ENGINE_VERSION = "v2-resume-001"


def _safe_job_id(job_id: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "-" for char in job_id).strip("-") or "job"


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def prepare_resume(
    job_id: str,
    title: str,
    company: str,
    description: str = "",
    *,
    profile_path: str | Path = "profile/career_profile.local.yaml",
    output_dir: str | Path = "data/v2-resumes",
    language_override: str | None = None,
    force: bool = False,
) -> ResumeBuild:
    profile = load_profile(profile_path)
    strategy = build_strategy(profile, title, description, language_override=language_override)
    identity_payload = {
        "engine_version": ENGINE_VERSION,
        "job_id": job_id,
        "title": title,
        "company": company,
        "description": description,
        "profile_hash": profile.source_hash,
        "language": strategy.language,
    }
    identity = hashlib.sha256(json.dumps(identity_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    artifact_dir = Path(output_dir) / f"greenhouse-{_safe_job_id(job_id)}"
    pdf_path = artifact_dir / "resume.pdf"
    meta_path = artifact_dir / "cache.json"
    validation_path = artifact_dir / "validation.json"
    if not force and meta_path.exists() and pdf_path.exists() and validation_path.exists():
        cached = json.loads(validation_path.read_text(encoding="utf-8"))
        if json.loads(meta_path.read_text(encoding="utf-8")).get("identity") == identity and cached.get("valid") is True:
            validation = ResumeValidation(True, str(cached.get("code", "OK")), tuple(cached.get("errors", [])), tuple(cached.get("warnings", [])), dict(cached.get("metrics", {})))
            return ResumeBuild(job_id, str(artifact_dir), str(pdf_path), identity, strategy, validation, True)

    artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    selected_ids = select_fact_ids(profile, strategy, title, description)
    document = generate_document(job_id, title, company, profile, strategy, selected_ids)
    validation = validate_document(document, profile, strategy, f"{title} {description}")
    _write_json(artifact_dir / "job.json", {"job_id": job_id, "title": title, "company": company, "description": description})
    _write_json(artifact_dir / "strategy.json", strategy.to_dict())
    _write_json(artifact_dir / "selected-facts.json", [profile.facts[fact_id].to_dict() for fact_id in selected_ids])
    _write_json(artifact_dir / "resume.json", document.to_dict())
    _write_json(artifact_dir / "cache.json", {"identity": identity, **identity_payload})
    if not validation.valid:
        _write_json(validation_path, validation.to_dict())
        return ResumeBuild(job_id, str(artifact_dir), "", identity, strategy, validation)

    (artifact_dir / "resume.txt").write_text(render_text(document), encoding="utf-8")
    try:
        docx_path = render_docx(document, artifact_dir / "resume.docx")
        render_pdf_from_docx(docx_path, pdf_path)
    except RuntimeError as exc:
        failed = ResumeValidation(False, "RESUME_RENDER_FAILED", (str(exc),), metrics=validation.metrics)
        _write_json(validation_path, failed.to_dict())
        return ResumeBuild(job_id, str(artifact_dir), "", identity, strategy, failed)
    _write_json(validation_path, validation.to_dict())
    return ResumeBuild(job_id, str(artifact_dir), str(pdf_path), identity, strategy, validation)
