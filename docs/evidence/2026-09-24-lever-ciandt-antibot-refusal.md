# Real run: CI&T (Lever) anti-bot refusal

- **Date:** 2026-09-24
- **Board:** `https://jobs.lever.co/ciandt/59494544-d851-4267-b0d2-fd953d4d8a72/apply`
- **Job:** `[Job-31538] Senior Software Architect (Frontend Angular), Brasil` (`0cd3d1467200558b`)
- **Application:** `application-bc7e48e543d73f95`
- **Command:** `apply 0cd3d1467200558b --no-headless --submit --timeout 300`

This is the run that exercised the whole human-handoff path against a real provider: fill, resume
upload, submit attempt, a human-solved challenge in the visible browser, a provider refusal, and the
handoff package built from the material that was approved.

## What happened

```text
15:57:27  submission_started     the agent clicks the final control; the challenge appears
   …      human                  the visible hCaptcha image challenge is solved by hand
15:59:35  submission_challenged  the POST leaves the browser with that token
```

Recorded attempt evidence:

```text
status_code: 400        submit_write: true        confirmed_submission: false
page error: "✱ There was an error verifying your application. Please try again."
decision:   provider_rejected    reason_token: provider_rejected_submission
```

The two-minute gap between `submission_started` and `submission_challenged` is what proves the write
happened **after** the human action: the observation loop only leaves before the deadline when a
response to the application URL arrives, and a POST sent at click time would have ended the run in
seconds. Compare with the previous run, where nobody solved the challenge:
`authorized_writes_used: 0`, no response, outcome `NEEDS_CAPTCHA` / `captcha_no_write`.

Resulting state: `NEEDS_HUMAN_CAPTCHA` (a submission that was sent and refused), then
`HANDOFF_IN_PROGRESS` with package `hpkg-5ecc8ee1b559c8c3932ea71d`.

## Attribution, and what is *not* claimed

The guard's decision carried `provider: recaptcha_enterprise`. That is an **attribution made from the
observed evidence**, not an established fact about which technology refused the submission, so it is
recorded in three parts:

```text
visible challenge provider:      hcaptcha            (the widget the human actually solved)
post-submit decision:            provider_rejected   (the library's decision, with reason token)
anti-bot provider attribution:   recaptcha_enterprise  (recorded from the decision)
```

What is directly established:

- the visible challenge was hCaptcha, and it was solved by a person;
- one submission write left the browser after that;
- the provider answered `400` with "There was an error verifying your application";
- the library classified the outcome as a provider rejection after a write, and attributed the
  rejection to reCAPTCHA Enterprise.

What is **not** established by this run: that reCAPTCHA Enterprise specifically refused the token, or
that the human solution was insufficient *because of* a risk score. Until this run the provenance of
the attribution (`sources`, `signal_kinds`) was not persisted, so the record could not be audited
back to a signal. It is persisted from now on: `challenge_provenance()` keeps the library's redacted
evidence — `sources`, `signal_kinds`, `confidence`, `rounds_observed`, `http_status` — in the
attempt, which is what separates an attribution from a fact.

Separately, ADR 0003 records an earlier direct observation on this same board: the widget itself
reported `navigator.webdriver = true`. That is a property of the browser context Playwright creates,
and masking it is deliberately out of scope (ADR 0001/0003). So the consistent reading is that the
automated environment is identified, not that the human solved the puzzle incorrectly.

## What was delivered

- The submission was **not** accepted. Nothing reached CI&T; there is still no confirmed submission.
- The handoff package was produced from the approved material: 18 approved answers, a copy of the
  resume, `package_sha256` over the whole content, and the bundle below the private artifacts root:

```text
data/applications/0cd3d1467200558b/handoff/hpkg-5ecc8ee1b559c8c3932ea71d/
├── package.json
└── resume.pdf
```

- The event journal holds only the reference and short tokens (`handoff_package_id`, `provider`,
  `reason_token`, `challenge_session_id`). Verified on this run: no name, e-mail or phone anywhere in
  the journal, and stdout of `application handoff` carries no answers.

## Defects this run found

1. Every live submission failed with `AttributeError: 'PlaywrightSessionManager' object has no
   attribute 'arm_challenge_runtime'` — the session class did not implement what the submitter
   called, and the test fake did. Fixed in `43767c8`, with a regression test derived from the
   consuming code.
2. `application handoff` printed the whole Application, spilling the candidate's answers to stdout.
   Fixed in `748b60e`.
