---
name: jobsearch-agent
description: Analyze job postings, prepare grounded ATS-compatible resumes and fill a real application page without submitting it.
version: 0.1.0
metadata:
  hermes:
    tags: [jobs, resume, safety, review]
---

# jobsearch-agent

Use the `jobsearch-agent` CLI from the project workdir. This skill covers Analyze + Generate, the
prepare/application boundary and the live fill. It may ingest a job, calculate fit, generate a
tailored resume, write audit artifacts, and drive a real application page through navigation,
filling and resume upload. It must not submit an application without an explicit user
authorization, must not bypass CAPTCHA/MFA, and must not invent candidate facts.

## Safe workflow

1. Validate the profile with `jobsearch-agent profile validate`.
2. Ingest a URL or sanitized JSON fixture.
3. Run `jobsearch-agent analyze <job-id>`.
4. Run `jobsearch-agent prepare <job-id>` only for relevant jobs.
5. Run `jobsearch-agent precheck <job-id>` and stop when the decision is not `READY`.
6. Run `jobsearch-agent apply <job-id>` to fill the real page. It reports
   `FILLED_REVIEW_REQUIRED` and never submits.
7. Report the JSON result and artifact directory, then ask the user before any submission.

## Submission boundary

- `apply` without `--submit` performs no network write: the browser session blocks
  POST/PUT/PATCH/DELETE, WebSocket and form submission.
- A submission requires an explicit human authorization: either `apply <job-id> --submit`
  or `application authorize-submit <intent-id>` followed by `application submit`.
- Never pass `--submit` on your own initiative.
- LinkedIn has no live executor. Do not attempt to automate, navigate or fill LinkedIn: the
  platform policy prohibits third-party automation of activity.

Every factual claim must reference a locked fact. A validation failure is `NEEDS_HUMAN`.
The demo profile is never valid for a real application. Do not expose credentials, tokens,
cookies or sensitive values in prompts or reports. Keep the CLI output JSON and do not retry
failed LLM calls indefinitely.
