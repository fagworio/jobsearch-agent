"""Classificação geográfica conservadora para vagas descobertas."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

from .models import DiscoveryJob


class GeoEligibility(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    LIKELY_ELIGIBLE = "LIKELY_ELIGIBLE"
    UNKNOWN = "UNKNOWN"
    INELIGIBLE = "INELIGIBLE"


@dataclass(frozen=True)
class GeographyAssessment:
    status: GeoEligibility
    scope: str
    evidence: str
    matched_terms: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "scope": self.scope,
            "evidence": self.evidence,
            "matched_terms": list(self.matched_terms),
        }


_BRAZIL_TERMS = (
    "brazil",
    "brasil",
    "brazilian",
    "sao paulo",
    "são paulo",
    "rio de janeiro",
    "belo horizonte",
    "betim",
    "brasilia",
    "brasilia",
)
_BRAZIL_CODES = ("br",)
_LATAM_TERMS = (
    "latam",
    "latin america",
    "south america",
    "central america",
    "americas",
    "argentina",
    "bolivia",
    "chile",
    "colombia",
    "costa rica",
    "cuba",
    "dominican republic",
    "ecuador",
    "el salvador",
    "guatemala",
    "honduras",
    "mexico",
    "nicaragua",
    "panama",
    "paraguay",
    "peru",
    "puerto rico",
    "uruguay",
    "venezuela",
    "bogota",
    "buenos aires",
    "medellin",
    "santiago",
    "lima",
    "mexico city",
    "san jose",
    "managua",
    "montevideo",
    "quito",
    "caracas",
)
# Country abbreviations overlap heavily with US state/province codes (for
# example CO = Colorado/Colombia, CA = California/Canada). We therefore only
# trust explicit country/region names for LATAM; BR remains safe because it is
# the country code used by the observed MyGreenhouse cards and does not overlap
# with a US state code.
_LATAM_CODES: tuple[str, ...] = ()
_WORLDWIDE_TERMS = (
    "worldwide",
    "global",
    "anywhere",
    "work from anywhere",
    "all countries",
    "international",
    "distributed",
)
_EXCLUSION_TERMS = (
    "us residents only",
    "usa residents only",
    "united states only",
    "only in the united states",
    "must be based in the united states",
    "must reside in the united states",
    "canada only",
    "must be based in canada",
    "must reside in canada",
    "no international hiring",
    "no international applicants",
    "no international candidates",
)
_COUNTRY_SPECIFIC_TERMS = (
    "united states",
    "usa",
    "canada",
    "australia",
    "india",
    "philippines",
    "singapore",
    "united kingdom",
    "germany",
    "france",
    "spain",
    "portugal",
    "ireland",
    "netherlands",
    "new zealand",
    "japan",
)


def _normalise(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value.casefold())
    without_marks = "".join(char for char in folded if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", without_marks).strip()


def _matches(text: str, terms: tuple[str, ...]) -> tuple[str, ...]:
    found: list[str] = []
    for term in terms:
        normalised = _normalise(term)
        if re.search(rf"(?<![a-z0-9]){re.escape(normalised)}(?![a-z0-9])", text):
            found.append(term)
    return tuple(found)


def _code_matches(text: str, codes: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(code.upper() for code in codes if re.search(rf"(?<![a-z0-9]){code}(?![a-z0-9])", text))


def assess_geography(job: DiscoveryJob) -> GeographyAssessment:
    """Classifica apenas sinais geográficos observáveis no card da vaga.

    A ausência de uma restrição explícita não é tratada como autorização de
    trabalho. Por isso uma vaga ``Remote`` sem escopo recebe ``LIKELY`` e uma
    vaga remota restrita a outro país recebe ``UNKNOWN``.
    """

    location = _normalise(f"{job.location} {job.work_type}")
    description = _normalise(job.description)
    remote = job.remote or "remote" in location or "work from home" in location

    exclusions = _matches(location, _EXCLUSION_TERMS) + _matches(description, _EXCLUSION_TERMS)
    if exclusions:
        return GeographyAssessment(
            GeoEligibility.INELIGIBLE,
            "restricted",
            f"explicit geographic exclusion: {', '.join(exclusions)}",
            exclusions,
        )

    brazil = _matches(location, _BRAZIL_TERMS) + _code_matches(location, _BRAZIL_CODES)
    if not brazil:
        brazil = _matches(description, _BRAZIL_TERMS)
    if brazil:
        return GeographyAssessment(GeoEligibility.ELIGIBLE, "brazil", "explicit Brazil location or scope", brazil)

    latam = _matches(location, _LATAM_TERMS) + _code_matches(location, _LATAM_CODES)
    if not latam:
        latam = _matches(description, _LATAM_TERMS)
    if latam:
        return GeographyAssessment(GeoEligibility.ELIGIBLE, "latam", "explicit LATAM location or scope", latam)

    worldwide = _matches(location, _WORLDWIDE_TERMS) + _matches(description, _WORLDWIDE_TERMS)
    if worldwide:
        return GeographyAssessment(GeoEligibility.ELIGIBLE, "worldwide", "explicit worldwide or international scope", worldwide)

    country_specific = _matches(location, _COUNTRY_SPECIFIC_TERMS)
    if country_specific:
        return GeographyAssessment(
            GeoEligibility.UNKNOWN,
            "country_specific",
            f"remote scope is country-specific and needs job-description verification: {', '.join(country_specific)}",
            country_specific,
        )

    if remote and (not job.location.strip() or _normalise(job.location) in {"remote", "remoto", "distributed"}):
        return GeographyAssessment(
            GeoEligibility.LIKELY_ELIGIBLE,
            "remote_unspecified",
            "remote is explicit but no geographic scope is shown on the card",
            ("remote",),
        )

    if remote:
        return GeographyAssessment(
            GeoEligibility.UNKNOWN,
            "unspecified",
            "remote is explicit but the card does not establish Brazil, LATAM, or worldwide eligibility",
            ("remote",),
        )

    return GeographyAssessment(
        GeoEligibility.UNKNOWN,
        "unspecified",
        "the card does not establish a qualifying remote geographic scope",
        (),
    )
