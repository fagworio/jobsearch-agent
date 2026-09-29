from __future__ import annotations

import pytest

from job_agent_v2.discovery import DiscoveryJob, DiscoveryMatrix, DiscoverySearchRun, GeoEligibility, GreenhouseDiscoveryAdapter, assess_geography, build_query_matrix, deduplicate_runs, load_match_profile, match_matrix, rank_shortlist, save_matrix
from job_agent_v2.discovery.greenhouse import DiscoveryInspectionError


SNAPSHOT = {
    "provider": "greenhouse",
    "page_type": "search",
    "surface": "document",
    "url": "https://my.greenhouse.io/jobs/search?query=frontend&work_type%5B%5D=remote",
    "title": "MyGreenhouse",
    "ready": True,
    "query": "frontend",
    "work_type": ["remote"],
    "jobs": [
        {
            "provider": "greenhouse",
            "job_id": "realchemistry:5431230008",
            "title": "Lead Front End Architect/Engineer",
            "company": "Real Chemistry",
            "href": "https://my.greenhouse.io/jobs/realchemistry/5431230008",
            "remote": True,
            "work_type": "Remote",
            "location": "United States",
            "salary": None,
            "posted": "Posted · 4 days ago • Applied · 18 minutes ago",
            "status": "Posted · 4 days ago • Applied · 18 minutes ago",
            "applied": True,
            "viewed": False,
        }
    ],
}


def test_greenhouse_discovery_preserves_card_fields_and_statuses():
    results = GreenhouseDiscoveryAdapter().inspect(SNAPSHOT)
    assert results.query == "frontend"
    assert results.work_type == ("remote",)
    assert len(results.jobs) == 1
    job = results.jobs[0]
    assert job.job_id == "realchemistry:5431230008"
    assert job.remote is True
    assert job.applied is True
    assert job.viewed is False
    assert job.salary is None


def test_greenhouse_discovery_rejects_application_snapshots():
    with pytest.raises(DiscoveryInspectionError, match="page type"):
        GreenhouseDiscoveryAdapter().inspect({**SNAPSHOT, "page_type": "application"})


def test_greenhouse_discovery_does_not_invent_jobs_when_missing():
    results = GreenhouseDiscoveryAdapter().inspect({**SNAPSHOT, "jobs": []})
    assert results.jobs == ()


def test_greenhouse_filter_discovery_preserves_real_parameter_contract():
    snapshot = {
        "provider": "greenhouse",
        "page_type": "search",
        "surface": "document",
        "url": "https://my.greenhouse.io/jobs/search?query=frontend&location=Brazil&lat=-11.929178&lon=-49.542799&location_type=country&country_short_name=BR&work_type[]=remote",
        "title": "MyGreenhouse",
        "ready": True,
        "query": "frontend",
        "parameters": {
            "query": ["frontend"],
            "location": ["Brazil"],
            "lat": ["-11.929178"],
            "lon": ["-49.542799"],
            "location_type": ["country"],
            "country_short_name": ["BR"],
            "work_type[]": ["remote"],
        },
        "filters": [
            {
                "key": "date_posted",
                "label": "Date posted",
                "control": "radio",
                "parameter": "date_posted",
                "selected": [],
                "options": [{"label": "Within 1 day", "value": "past_day", "selected": False}],
            },
            {
                "key": "salary",
                "label": "Salary",
                "control": "radio",
                "parameter": "salary",
                "selected": [],
                "options": [{"label": "$100,000+", "value": "more_than_100k", "selected": False}],
            },
            {
                "key": "work_type",
                "label": "Work type",
                "control": "checkbox",
                "parameter": "work_type[]",
                "selected": ["remote"],
                "options": [{"label": "Remote", "value": "remote", "selected": True}],
            },
            {
                "key": "employment_type",
                "label": "Employment type",
                "control": "checkbox",
                "parameter": "employment_type[]",
                "selected": [],
                "options": [{"label": "Full time", "value": "full_time", "selected": False}],
            },
            {
                "key": "location",
                "label": "Location",
                "control": "combobox",
                "parameter": "location",
                "selected": ["Brazil"],
                "options": [],
            },
        ],
    }
    result = GreenhouseDiscoveryAdapter().inspect_filters(snapshot)
    assert result.parameters["work_type[]"] == ("remote",)
    assert result.parameters["location_type"] == ("country",)
    assert result.filters[0].options[0].value == "past_day"
    assert result.filters[2].parameter == "work_type[]"


def test_greenhouse_filter_discovery_rejects_malformed_parameters():
    with pytest.raises(DiscoveryInspectionError, match="parameters"):
        GreenhouseDiscoveryAdapter().inspect_filters({
            "provider": "greenhouse",
            "page_type": "search",
            "surface": "document",
            "url": "https://my.greenhouse.io/jobs/search",
            "title": "MyGreenhouse",
            "ready": True,
            "query": "",
            "parameters": {"query": "frontend"},
            "filters": [],
        })


def test_search_matrix_is_stable_and_deduplicated():
    matrix = build_query_matrix()
    assert len(matrix) == 25
    assert matrix[0].family == "primary"
    assert matrix[0].query == "wordpress developer"
    assert matrix[-1].family == "broader"
    assert len({item.query.casefold() for item in matrix}) == len(matrix)


def test_discovery_matrix_store_is_private_and_round_trippable(tmp_path):
    matrix = DiscoveryMatrix("greenhouse", ("remote",), ())
    path = save_matrix(matrix, tmp_path / "nested" / "matrix.json")
    assert path.read_text(encoding="utf-8").startswith('{\n  "provider": "greenhouse"')
    assert path.stat().st_mode & 0o777 == 0o600


def test_discovery_matrix_deduplicates_by_job_id_and_keeps_sources():
    adapter = GreenhouseDiscoveryAdapter()
    first = adapter.inspect(SNAPSHOT)
    second = adapter.inspect({
        **SNAPSHOT,
        "query": "wordpress",
        "url": "https://my.greenhouse.io/jobs/search?query=wordpress&work_type%5B%5D=remote",
        "jobs": [{**SNAPSHOT["jobs"][0], "title": "Updated card title"}],
    })
    runs = (
        DiscoverySearchRun("primary", "frontend", first),
        DiscoverySearchRun("primary", "wordpress", second),
    )
    unique = deduplicate_runs(runs)
    assert len(unique) == 1
    assert unique[0].job.title == "Lead Front End Architect/Engineer"
    assert unique[0].families == ("primary",)
    assert unique[0].queries == ("frontend", "wordpress")


def test_discovery_matrix_persists_raw_and_unique_counts():
    adapter = GreenhouseDiscoveryAdapter()
    results = adapter.inspect(SNAPSHOT)
    matrix = DiscoveryMatrix(
        "greenhouse",
        ("remote",),
        (
            DiscoverySearchRun("primary", "frontend", results),
            DiscoverySearchRun("frontend", "web developer", results),
        ),
    )
    payload = matrix.to_dict()
    assert payload["job_count"] == 2
    assert payload["unique_job_count"] == 1
    assert payload["duplicate_job_count"] == 1
    assert payload["deduplicated_jobs"][0]["queries"] == ["frontend", "web developer"]


@pytest.mark.parametrize(
    ("location", "remote", "expected", "scope"),
    [
        ("Southeast, BR", True, GeoEligibility.ELIGIBLE, "brazil"),
        ("Buenos Aires, Argentina", True, GeoEligibility.ELIGIBLE, "latam"),
        ("Denver, CO", True, GeoEligibility.UNKNOWN, "unspecified"),
        ("Worldwide", True, GeoEligibility.ELIGIBLE, "worldwide"),
        ("Remote", True, GeoEligibility.LIKELY_ELIGIBLE, "remote_unspecified"),
        ("United States", True, GeoEligibility.UNKNOWN, "country_specific"),
        ("US residents only", True, GeoEligibility.INELIGIBLE, "restricted"),
    ],
)
def test_geography_assessment_is_conservative(location, remote, expected, scope):
    job = GreenhouseDiscoveryAdapter().inspect({
        **SNAPSHOT,
        "jobs": [{**SNAPSHOT["jobs"][0], "location": location, "remote": remote}],
    }).jobs[0]
    assessment = assess_geography(job)
    assert assessment.status is expected
    assert assessment.scope == scope


def test_matrix_serializes_geography_summary():
    results = GreenhouseDiscoveryAdapter().inspect(SNAPSHOT)
    payload = DiscoveryMatrix(
        "greenhouse",
        ("remote",),
        (DiscoverySearchRun("primary", "frontend", results),),
    ).to_dict()
    assert payload["geography"]["candidate_scope"] == "Brazil/LATAM/Worldwide"
    assert payload["deduplicated_jobs"][0]["geography"]["status"] == "UNKNOWN"


def test_match_uses_explicit_profile_skills_and_preserves_basis(tmp_path):
    profile_path = tmp_path / "career_profile.yaml"
    profile_path.write_text(
        """
skills:
  wordpress: {tags: [wordpress, custom-plugins]}
  php: {tags: [php]}
  react: {tags: [react]}
experience:
  - role: WordPress Developer | Front-End Developer
""",
        encoding="utf-8",
    )
    result = GreenhouseDiscoveryAdapter().inspect({
        **SNAPSHOT,
        "jobs": [{**SNAPSHOT["jobs"][0], "title": "Senior PHP/Wordpress Developer"}],
    })
    matrix = DiscoveryMatrix("greenhouse", ("remote",), (DiscoverySearchRun("primary", "wordpress developer", result),))
    matched = match_matrix(matrix, load_match_profile(profile_path))
    payload = matched.to_dict()
    match = payload["matching"]["matches"][0]
    assert match["job_id"] == "realchemistry:5431230008"
    assert match["matched_skills"] == ["Wordpress", "Php"]
    assert match["matched_roles"] == ["wordpress"]
    assert "observed card description" in match["basis"]
    assert payload["deduplicated_jobs"][0]["match"]["score"] == match["score"]


def test_match_does_not_invent_missing_skills(tmp_path):
    profile_path = tmp_path / "career_profile.yaml"
    profile_path.write_text("skills: {wordpress: {tags: [wordpress]}}\nexperience: []\n", encoding="utf-8")
    result = GreenhouseDiscoveryAdapter().inspect({
        **SNAPSHOT,
        "jobs": [{**SNAPSHOT["jobs"][0], "title": "Senior Systems Engineer"}],
    })
    matrix = DiscoveryMatrix("greenhouse", ("remote",), (DiscoverySearchRun("primary", "wordpress", result),))
    matched = match_matrix(matrix, load_match_profile(profile_path))
    match = matched.to_dict()["matching"]["matches"][0]
    assert match["matched_skills"] == ["Wordpress"]
    assert match["matched_roles"] == []
    assert match["basis"].startswith("job card title, observed card description")


def test_match_uses_observed_description_without_accepting_adjacent_role(tmp_path):
    profile_path = tmp_path / "career_profile.yaml"
    profile_path.write_text(
        """
skills:
  wordpress: {tags: [wordpress]}
  react: {tags: [react]}
experience:
  - role: Front-End Developer
""",
        encoding="utf-8",
    )
    adapter = GreenhouseDiscoveryAdapter()
    technical = adapter.inspect({
        **SNAPSHOT,
        "jobs": [{
            **SNAPSHOT["jobs"][0],
            "title": "Frontend Developer",
            "applied": False,
            "status": "Posted",
            "description": "Build React interfaces and custom WordPress integrations.",
        }],
    })
    adjacent = adapter.inspect({
        **SNAPSHOT,
        "jobs": [{
            **SNAPSHOT["jobs"][0],
            "job_id": "example:2",
            "title": "Project Manager",
            "applied": False,
            "status": "Posted",
            "description": "Manage WordPress and React delivery across clients.",
        }],
    })
    matrix = DiscoveryMatrix(
        "greenhouse",
        ("remote",),
        (
            DiscoverySearchRun("frontend", "frontend developer", technical),
            DiscoverySearchRun("broader", "web developer", adjacent),
        ),
    )
    matches = match_matrix(matrix, load_match_profile(profile_path))
    by_id = {match.job_id: match for match in matches.matches}
    assert "React" in by_id["realchemistry:5431230008"].matched_skills
    assert by_id["realchemistry:5431230008"].role_compatible is True
    assert by_id["example:2"].role_compatible is False
    report = rank_shortlist(matches)
    assert {entry.job_id: entry.selection for entry in report.entries}["example:2"] == "REJECTED"


def test_geography_rejects_explicit_description_restriction():
    job = DiscoveryJob(
        provider="greenhouse",
        job_id="restricted:1",
        title="WordPress Developer",
        company="Example",
        href="https://my.greenhouse.io/jobs/example/1",
        remote=True,
        work_type="Remote",
        location="Remote",
        salary=None,
        posted="Posted",
        status="Posted",
        applied=False,
        viewed=False,
        description="Remote, but must be based in the United States.",
    )
    assert assess_geography(job).status is GeoEligibility.INELIGIBLE
