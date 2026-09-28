from __future__ import annotations

from job_agent_v2.challenges import ChallengeState, classify


def test_challenge_observation_preserves_explicit_browser_state():
    observation = classify({"state": "BLOCKING", "signals": ["visible-hcaptcha-iframe"]})
    assert observation.state is ChallengeState.BLOCKING
    assert observation.signals == ("visible-hcaptcha-iframe",)


def test_challenge_observation_is_fail_closed_for_unknown_shape():
    assert classify({"signals": ["looks-like-captcha"]}).state is ChallengeState.UNKNOWN
    assert classify({"state": "CLEAR", "signals": "not-a-list"}).state is ChallengeState.UNKNOWN
