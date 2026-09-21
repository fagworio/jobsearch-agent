---
name: jobsearch-agent
description: Analyze job postings and prepare grounded, ATS-compatible resumes without submitting applications.
version: 0.1.0
metadata:
  hermes:
    tags: [jobs, resume, safety, review]
---

# jobsearch-agent

Use the `jobsearch-agent` CLI from the project workdir. This milestone is limited to
Analyze + Generate. It may ingest a job, calculate fit, generate a tailored resume and
write audit artifacts. It must not submit applications, use a browser, bypass CAPTCHA/MFA,
or invent candidate facts.

## Safe workflow

1. Validate the profile with `jobsearch-agent profile validate`.
2. Ingest a URL or sanitized JSON fixture.
3. Run `jobsearch-agent analyze <job-id>`.
4. Run `jobsearch-agent prepare <job-id>` only for relevant jobs.
5. Report the JSON result and artifact directory.

Every factual claim must reference a locked fact. A validation failure is `NEEDS_HUMAN`.
The demo profile is never valid for a real application. Do not expose credentials, tokens,
cookies or sensitive values in prompts or reports. Keep the CLI output JSON and do not retry
failed LLM calls indefinitely.

