"""Geração, validação factual/ATS e renderização de currículo."""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from .models import CareerProfile, Fact, Job, Resume, ResumeClaim, ResumeStrategy, ValidationResult


def _statement(fact: Fact, language: str) -> str:
    return fact.statements.get(language) or fact.statements.get("en-US") or next(iter(fact.statements.values()))


def select_fact_ids(job: Job, strategy: ResumeStrategy, profile: CareerProfile, facts: dict[str, Fact]) -> list[str]:
    wanted = {term.lower() for term in strategy.keywords + strategy.focus}
    selected: list[str] = []
    for experience in profile.experiences:
        for fact_id in experience.fact_ids:
            fact = facts.get(fact_id)
            if fact and (not wanted or wanted & {tag.lower() for tag in fact.tags} or any(term in _statement(fact, strategy.language).lower() for term in wanted)):
                selected.append(fact_id)
    return selected or [fact_id for experience in profile.experiences for fact_id in experience.fact_ids]


def generate_resume(job: Job, strategy: ResumeStrategy, profile: CareerProfile, facts: dict[str, Fact], selected_ids: list[str]) -> Resume:
    language = strategy.language
    claims: list[ResumeClaim] = []
    summary = profile.professional_summary.get(language) or profile.professional_summary.get("en-US", "")
    if summary:
        claims.append(ResumeClaim(summary, list(selected_ids), bool(selected_ids)))
    experience_rows: list[dict[str, Any]] = []
    for experience in profile.experiences:
        exp_facts = [fact_id for fact_id in experience.fact_ids if fact_id in selected_ids]
        if not exp_facts:
            continue
        bullets = [_statement(facts[fact_id], language) for fact_id in exp_facts]
        for bullet, fact_id in zip(bullets, exp_facts):
            claims.append(ResumeClaim(bullet, [fact_id], True))
        experience_rows.append({"company": experience.company, "role": experience.role, "start_date": experience.start_date, "end_date": experience.end_date, "bullets": bullets, "fact_ids": exp_facts})
    return Resume(
        id=f"resume-{job.id}-{language}", job_id=job.id, language=language,
        header={"name": profile.identity.get("name", ""), "email": profile.identity.get("email", ""), "location": profile.identity.get("location", "")},
        summary=summary, skills=strategy.keywords, experience=experience_rows, claims=claims,
    )


def validate_facts(resume: Resume, facts: dict[str, Fact]) -> ValidationResult:
    errors: list[str] = []
    for claim in resume.claims:
        supported = [fact_id for fact_id in claim.supported_by if fact_id in facts and facts[fact_id].verified]
        if not supported:
            claim.valid = False
            errors.append(f"unsupported claim: {claim.claim}")
            continue
        corpus = " ".join(_statement(facts[fact_id], resume.language).lower() for fact_id in supported)
        normalized_claim = re.sub(r"[^a-z0-9áéíóúãõç ]", " ", claim.claim.lower())
        important = [token for token in normalized_claim.split() if len(token) > 3]
        if important and not any(token in corpus for token in important):
            claim.valid = False
            errors.append(f"claim does not match supporting facts: {claim.claim}")
    return ValidationResult(not errors, "RESUME_VALIDATION_FAILED" if errors else "OK", errors)


def validate_ats(resume: Resume) -> ValidationResult:
    errors: list[str] = []
    if not resume.header.get("name"):
        errors.append("contact name is missing")
    if not resume.summary:
        errors.append("summary is missing")
    if len(resume.experience) == 0:
        errors.append("experience is missing")
    if any(not row.get("bullets") for row in resume.experience):
        errors.append("experience entry has no bullets")
    return ValidationResult(not errors, "ATS_VALIDATION_FAILED" if errors else "OK", errors, details={"template": resume.template, "one_column": True})


def render_text(resume: Resume) -> str:
    lines = [resume.header.get("name", ""), resume.header.get("email", ""), resume.header.get("location", ""), "", "SUMMARY", resume.summary, "", "SKILLS", ", ".join(resume.skills), "", "EXPERIENCE"]
    for row in resume.experience:
        dates = f"{row['start_date']} - {row['end_date'] or 'Present'}"
        lines.extend([f"{row['role']} | {row['company']} | {dates}"] + [f"- {bullet}" for bullet in row["bullets"]] + [""])
    return "\n".join(line for line in lines if line is not None).strip() + "\n"


def _xml_text(text: str) -> str:
    return html.escape(text, quote=False).replace("\n", "</w:t></w:r><w:r><w:br/><w:t>")


def render_docx(resume: Resume, path: str | Path) -> Path:
    """Escreve DOCX ATS de uma coluna sem exigir python-docx no runtime."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    paragraphs = []
    for line in render_text(resume).splitlines():
        paragraphs.append(f"<w:p><w:r><w:t xml:space=\"preserve\">{_xml_text(line)}</w:t></w:r></w:p>")
    document = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{''.join(paragraphs)}<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1080" w:right="1080" w:bottom="1080" w:left="1080"/></w:sectPr></w:body></w:document>'''
    content_types = '''<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'''
    rels = '''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'''
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document)
    return target


def render_pdf_from_docx(docx_path: str | Path, pdf_path: str | Path) -> Path:
    target = Path(pdf_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    libreoffice = shutil.which("libreoffice") or shutil.which("soffice")
    if not libreoffice:
        raise RuntimeError("LibreOffice is required to render PDF")
    with tempfile.TemporaryDirectory(prefix="jobsearch-pdf-") as directory:
        result = subprocess.run([libreoffice, "--headless", "--convert-to", "pdf", "--outdir", directory, str(docx_path)], capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "PDF conversion failed")
        generated = Path(directory) / (Path(docx_path).stem + ".pdf")
        if not generated.exists():
            raise RuntimeError("LibreOffice did not produce a PDF")
        target.write_bytes(generated.read_bytes())
    return target

