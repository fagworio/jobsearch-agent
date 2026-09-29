from __future__ import annotations

import json

import yaml

from job_agent_v2.resume import (
    ResumeClaim,
    ResumeDocument,
    build_strategy,
    generate_document,
    load_profile,
    prepare_resume,
    select_fact_ids,
    validate_document,
)


def _profile(tmp_path):
    path = tmp_path / "career_profile.yaml"
    path.write_text(yaml.safe_dump({
        "identity": {"name": "Candidate", "email": "candidate@example.test", "location": "Brazil"},
        "professional_summary": {
            "en-US": {"text": "Web developer with JavaScript experience.", "fact_ids": ["fact_summary"]},
        },
        "experience": [{
            "id": "exp-1",
            "company": "Example",
            "role": "Frontend Developer",
            "start_date": "2020",
            "end_date": "",
            "facts": ["fact_frontend"],
        }],
        "education": [{
            "id": "edu-1",
            "institution": "Example University",
            "credential": {"en-US": "Bachelor's Degree"},
            "field_of_study": {"en-US": "Computer Science"},
            "facts": ["fact_education"],
        }],
        "skills": {"javascript": {"tags": ["javascript"]}, "php": {"tags": ["php"]}},
        "facts": {
            "fact_summary": {"type": "summary", "statement": {"en-US": "Web developer with JavaScript experience."}, "tags": ["javascript"]},
            "fact_frontend": {"type": "experience", "statement": {"en-US": "Built JavaScript frontend interfaces and REST APIs."}, "tags": ["javascript", "frontend", "rest-api"]},
            "fact_education": {"type": "education", "statement": {"en-US": "Completed a Bachelor's Degree in Computer Science."}, "tags": ["education"]},
        },
    }, allow_unicode=True), encoding="utf-8")
    return path


def test_dynamic_strategy_selects_relevant_facts_and_keeps_provenance(tmp_path):
    profile = load_profile(_profile(tmp_path))
    strategy = build_strategy(profile, "Frontend JavaScript Engineer", "Build frontend interfaces with REST APIs")
    selected = select_fact_ids(profile, strategy, strategy.target_role, "Build frontend interfaces with REST APIs")
    document = generate_document("job-1", strategy.target_role, "Example Co", profile, strategy, selected)
    validation = validate_document(document, profile, strategy, strategy.target_role)

    assert strategy.language == "en-US"
    assert "javascript" in strategy.focus
    assert "fact_frontend" in selected
    assert validation.valid
    assert all(claim.supported_by for claim in document.claims)


def test_unsupported_claim_blocks_resume(tmp_path):
    profile = load_profile(_profile(tmp_path))
    document = ResumeDocument(
        resume_id="resume-1",
        job_id="job-1",
        company="Example Co",
        target_role="Frontend Engineer",
        language="en-US",
        header={"name": "Candidate", "email": "candidate@example.test"},
        summary="Web developer with JavaScript experience.",
        skills=("javascript",),
        experience=({"company": "Example", "role": "Developer", "bullets": ["Improved conversion by 45%"]},),
        education=(),
        claims=(ResumeClaim("Improved conversion by 45%", ("fact_frontend",)),),
    )
    result = validate_document(document, profile, build_strategy(profile, "Frontend Engineer"), "Frontend Engineer")
    assert not result.valid
    assert result.code == "RESUME_VALIDATION_FAILED"


def test_resume_engine_writes_job_artifacts_and_reuses_cache(tmp_path, monkeypatch):
    profile_path = _profile(tmp_path)
    output = tmp_path / "resumes"

    def fake_docx(document, path):
        path.write_bytes(b"docx")
        return path

    def fake_pdf(docx_path, path):
        path.write_bytes(b"pdf")
        return path

    monkeypatch.setattr("job_agent_v2.resume.engine.render_docx", fake_docx)
    monkeypatch.setattr("job_agent_v2.resume.engine.render_pdf_from_docx", fake_pdf)
    first = prepare_resume("example:1", "Frontend Engineer", "Example Co", profile_path=profile_path, output_dir=output)
    second = prepare_resume("example:1", "Frontend Engineer", "Example Co", profile_path=profile_path, output_dir=output)

    assert first.ready
    assert not first.cache_hit
    assert second.ready
    assert second.cache_hit
    assert (output / "greenhouse-example-1" / "job.json").exists()
    validation = json.loads((output / "greenhouse-example-1" / "validation.json").read_text(encoding="utf-8"))
    assert validation["valid"] is True
