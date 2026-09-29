"""Motor de currículo factual por vaga da V2."""

from .engine import ENGINE_VERSION, prepare_resume
from .generator import generate_document
from .models import ResumeBuild, ResumeClaim, ResumeDocument, ResumeFact, ResumeProfile, ResumeStrategy, ResumeValidation
from .profile import load_profile
from .selector import cluster_fact_ids, select_fact_ids
from .strategy import build_strategy, detect_language
from .validator import quality_metrics, validate_document, validate_facts, validate_ats

__all__ = [
    "ENGINE_VERSION",
    "ResumeBuild",
    "ResumeClaim",
    "ResumeDocument",
    "ResumeFact",
    "ResumeProfile",
    "ResumeStrategy",
    "ResumeValidation",
    "build_strategy",
    "cluster_fact_ids",
    "detect_language",
    "generate_document",
    "load_profile",
    "prepare_resume",
    "quality_metrics",
    "select_fact_ids",
    "validate_ats",
    "validate_document",
    "validate_facts",
]
