"""Descoberta somente leitura de vagas no MyGreenhouse."""

from .greenhouse import GreenhouseDiscoveryAdapter
from .eligibility import GeoEligibility, GeographyAssessment, assess_geography
from .matching import MatchProfile, load_match_profile, match_matrix, match_occurrence, role_family_compatible
from .shortlist import ShortlistEntry, ShortlistReport, rank_shortlist, save_shortlist
from .pipeline import PipelineItem, PipelineManifest, build_pipeline, load_pipeline, load_shortlist, save_pipeline
from .batch import BatchReport, load_policy, plan_batch, save_batch
from .models import (
    DiscoveryFilter,
    DiscoveryFilterOption,
    DiscoveryFilters,
    DiscoveryJob,
    DiscoveryJobOccurrence,
    DiscoveryMatch,
    DiscoveryMatrix,
    DiscoveryResults,
    DiscoverySearchRun,
    deduplicate_runs,
)
from .search_profile import DEFAULT_SEARCH_FAMILIES, SearchBudget, SearchCursor, SearchQuery, build_query_matrix
from .store import load_matrix, save_matrix

__all__ = [
    "DiscoveryFilter",
    "DiscoveryFilterOption",
    "DiscoveryFilters",
    "DiscoveryJob",
    "DiscoveryJobOccurrence",
    "DiscoveryMatch",
    "DiscoveryMatrix",
    "DiscoveryResults",
    "DiscoverySearchRun",
    "GeoEligibility",
    "GeographyAssessment",
    "assess_geography",
    "MatchProfile",
    "load_match_profile",
    "match_matrix",
    "match_occurrence",
    "role_family_compatible",
    "ShortlistEntry",
    "ShortlistReport",
    "rank_shortlist",
    "save_shortlist",
    "PipelineItem",
    "PipelineManifest",
    "build_pipeline",
    "load_pipeline",
    "load_shortlist",
    "save_pipeline",
    "BatchReport",
    "load_policy",
    "plan_batch",
    "save_batch",
    "deduplicate_runs",
    "DEFAULT_SEARCH_FAMILIES",
    "GreenhouseDiscoveryAdapter",
    "SearchQuery",
    "SearchBudget",
    "SearchCursor",
    "build_query_matrix",
    "load_matrix",
    "save_matrix",
]
