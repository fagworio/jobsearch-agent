# V2 Greenhouse measurement — 2026-09-28

This measurement is read-only. Five current public Greenhouse application pages
were opened in Chromium; no application control was clicked and no resume was
uploaded.

## Observed pages

- GitLab — AI Engineer: HTTP 200, 30 fields, 7 required, 2 file inputs, 0
  native selects, 13 role comboboxes, 0 next/continue controls, no confirmation.
- Vercel — DevRel Engineer, Agentic Infrastructure: HTTP 200, 31 fields, 6
  required, 2 file inputs, 0 native selects, 13 role comboboxes, 0
  next/continue controls, no confirmation.
- Cloudflare — AI Security Research & Red Team Engineer: HTTP 200, 24 fields,
  5 required, 2 file inputs, 0 native selects, 8 role comboboxes, 0
  next/continue controls, no confirmation.
- Yext — Senior Software Engineer: HTTP 200, 32 fields, 9 required, 2 file
  inputs, 0 native selects, 13 role comboboxes, 0 next/continue controls, no
  confirmation.
- GitLab — Backend Engineer, Duo Chat: HTTP 200, 32 fields, 8 required, 2 file
  inputs, 0 native selects, 13 role comboboxes, 0 next/continue controls, no
  confirmation.

## Findings

- The current Greenhouse pages are already application pages; no separate Apply
  navigation was needed in these five samples.
- Every sample exposed one form and two file inputs, with a dedicated resume
  control among them.
- Native `<select>` controls were absent in all five samples. Structured
  questions are rendered primarily as `role=combobox`, radio or checkbox
  controls.
- No Next/Continue/Review control was observed, so these five samples did not
  exercise a multi-step application.
- No confirmation content was present before submission.
- Five challenge-related DOM signals were present in each sample. This is only
  an observed signal count, not a conclusion that the challenge was blocking;
  the V2 challenge state must be determined from visible state and page
  behavior, not from a keyword alone.
- Non-GET traffic observed during navigation was third-party telemetry only
  (`c.spl.greenhouse.io` and, for Yext, `jsv3.recruitics.com`). No application
  submit endpoint was called.

## Custom-question examples

The samples included factual, legal and self-identification questions such as:

- prior employment or consulting with GitLab;
- visa sponsorship and work authorization;
- current country of residence;
- prior employment with Yext;
- relocation or hub-location requirements;
- technical experience ratings;
- voluntary gender, ethnicity, veteran and disability questions.

The adapter therefore keeps the extension output neutral and leaves answer
meaning and authorization to Python. It does not generate a response from the
label alone.
