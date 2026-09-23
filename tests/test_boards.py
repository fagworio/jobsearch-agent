"""Descoberta em APIs públicas de boards de ATS."""

from __future__ import annotations

import json

import httpx
import pytest

from jobsearch_agent.boards import (
    BoardError,
    _board_url,
    discover_board,
    discover_many,
    supported_providers,
)


GREENHOUSE_PAYLOAD = {
    "jobs": [
        {
            "id": 1,
            "title": "WordPress Developer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
            "location": {"name": "Remote - Brazil"},
            "content": "<p>We need <b>WordPress</b> and PHP experience.</p>",
            "updated_at": "2026-09-01T10:00:00-04:00",
        },
        {
            "id": 2,
            "title": "Sales Manager",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
            "location": {"name": "New York, NY"},
            "content": "<p>Sell things to people.</p>",
            "updated_at": "2026-08-01T10:00:00-04:00",
        },
    ]
}

LEVER_PAYLOAD = [
    {
        "id": "abc",
        "text": "Front-End Developer",
        "hostedUrl": "https://jobs.lever.co/spotify/abc",
        "categories": {"location": "Remote", "commitment": "Full-time"},
        "descriptionPlain": "React and TypeScript.",
        "createdAt": 1757000000000,
    }
]

ASHBY_PAYLOAD = {
    "jobs": [
        {
            "id": "x",
            "title": "Frontend Engineer",
            "location": "Remote",
            "employmentType": "FullTime",
            "jobUrl": "https://jobs.ashbyhq.com/ramp/x",
            "descriptionHtml": "<p>React and <b>TypeScript</b></p>",
            "publishedAt": "2026-08-01",
        }
    ]
}


def _client(payload, *, status: int = 200, seen: list | None = None, text: str | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        if text is not None:
            return httpx.Response(status, text=text, headers={"content-type": "text/html"})
        return httpx.Response(status, json=payload, headers={"content-type": "application/json"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_supported_providers_are_the_documented_boards():
    assert supported_providers() == ["ashby", "greenhouse", "lever"]


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("greenhouse", "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"),
        ("lever", "https://api.lever.co/v0/postings/acme?mode=json"),
        ("ashby", "https://api.ashbyhq.com/posting-api/job-board/acme"),
    ],
)
def test_board_url_uses_the_provider_public_api(provider: str, expected: str):
    assert _board_url(provider, "acme") == expected


def test_greenhouse_board_is_normalized_into_domain_jobs():
    seen: list[str] = []
    jobs = discover_board("greenhouse", "acme", client=_client(GREENHOUSE_PAYLOAD, seen=seen))

    assert seen == ["https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"]
    assert len(jobs) == 2
    first = jobs[0]
    assert first.title == "WordPress Developer"
    assert first.company == "acme"
    assert first.source == "greenhouse"
    assert first.location == "Remote - Brazil"
    assert first.url == "https://boards.greenhouse.io/acme/jobs/1"
    assert first.posted_at.startswith("2026-09-01")
    # HTML is reduced to text so fit analysis sees the posting content.
    assert first.description == "We need WordPress and PHP experience."
    assert "<p>" not in first.description
    # The raw provider record is preserved and must be JSON-serializable.
    json.dumps(first.raw_payload)


def test_lever_board_is_normalized_from_categories():
    jobs = discover_board("lever", "spotify", client=_client(LEVER_PAYLOAD))
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Front-End Developer"
    assert job.company == "spotify"
    assert job.location == "Remote"
    assert job.employment_type == "Full-time"
    assert job.url == "https://jobs.lever.co/spotify/abc"
    assert job.description == "React and TypeScript."


def test_ashby_board_is_normalized_from_description_html():
    jobs = discover_board("ashby", "ramp", client=_client(ASHBY_PAYLOAD))
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Frontend Engineer"
    assert job.employment_type == "FullTime"
    assert job.description == "React and TypeScript"
    assert job.url == "https://jobs.ashbyhq.com/ramp/x"


def test_query_filters_on_title_and_description():
    jobs = discover_board("greenhouse", "acme", client=_client(GREENHOUSE_PAYLOAD), query="wordpress")
    assert [job.title for job in jobs] == ["WordPress Developer"]


def test_query_requires_every_token():
    jobs = discover_board("greenhouse", "acme", client=_client(GREENHOUSE_PAYLOAD), query="wordpress rust")
    assert jobs == []


def test_location_filters_the_posting():
    jobs = discover_board("greenhouse", "acme", client=_client(GREENHOUSE_PAYLOAD), location="brazil")
    assert [job.title for job in jobs] == ["WordPress Developer"]


def test_limit_caps_the_result():
    jobs = discover_board("greenhouse", "acme", client=_client(GREENHOUSE_PAYLOAD), limit=1)
    assert len(jobs) == 1


@pytest.mark.parametrize("provider", ["workday", "", "leverr"])
def test_unsupported_provider_is_rejected(provider: str):
    with pytest.raises(BoardError, match="unsupported board provider"):
        discover_board(provider, "acme", client=_client(GREENHOUSE_PAYLOAD))


def test_provider_lookup_is_case_and_whitespace_insensitive():
    jobs = discover_board("  GREENHOUSE ", "acme", client=_client(GREENHOUSE_PAYLOAD))
    assert len(jobs) == 2


@pytest.mark.parametrize("board", ["", "../etc/passwd", "acme/../../x", "a" * 81, "acme jobs", "acme?x=1"])
def test_invalid_board_token_is_rejected(board: str):
    with pytest.raises(BoardError, match="invalid board token"):
        discover_board("greenhouse", board, client=_client(GREENHOUSE_PAYLOAD))


def test_provider_http_error_becomes_a_board_error():
    with pytest.raises(BoardError, match="HTTP 500"):
        discover_board("greenhouse", "acme", client=_client({}, status=500))


def test_non_json_response_becomes_a_board_error():
    with pytest.raises(BoardError, match="did not return JSON"):
        discover_board("greenhouse", "acme", client=_client(None, text="<html>blocked</html>"))


def test_malformed_payload_shape_becomes_a_board_error():
    with pytest.raises(BoardError, match="no jobs list"):
        discover_board("greenhouse", "acme", client=_client({"unexpected": []}))
    with pytest.raises(BoardError, match="list of postings"):
        discover_board("lever", "acme", client=_client({"jobs": []}))


def test_discover_many_reports_failures_without_aborting(monkeypatch):
    def fake(provider, board, **_kwargs):
        if board == "broken":
            raise BoardError("greenhouse board returned HTTP 503")
        return discover_board(provider, board, client=_client(GREENHOUSE_PAYLOAD))

    monkeypatch.setattr("jobsearch_agent.boards.discover_board", fake)
    jobs, failures = discover_many([("greenhouse", "acme"), ("greenhouse", "broken")])
    assert len(jobs) == 2
    assert failures == [{"provider": "greenhouse", "board": "broken", "error": "greenhouse board returned HTTP 503"}]
