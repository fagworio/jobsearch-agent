# ADR 0007: unified question resolution

- **Status:** Accepted
- **Date:** 2026-09-24

## Context

The application flow could already answer a lot: approved answers, answer rules, identity, skills,
work authorization, sponsorship, relocation, semantic near-matches. But it ended in `None` as soon as
it met a *legitimate question nobody had registered* — and that is exactly where the product stopped
fulfilling its purpose. The application could not finish, and nothing said why.

Three failure modes were possible in the old shape, and only one of them was acceptable:

```text
silently None   -> the field stays empty, the Safety Gate reports "unknown_answer", fine but mute
guessed answer  -> inventing a fact about the candidate
explicit stop   -> NEEDS_HUMAN with a reason
```

## Decision

### One resolver, always an explicit decision

```python
QuestionResolver.resolve_field(field) -> QuestionResolution
```

```text
RESOLVED      there is an answer, with origin and support
NEEDS_HUMAN   the question is legitimate and a fact is missing
UNSUPPORTED   the form asks for something the agent cannot receive
```

Never `None` without an explanation. `ApplicationAnswer` remains the persisted form of an accepted
answer; `QuestionResolution` is the engine's decision, and it carries `source`, `reason`,
`confidence`, `supported_by`, `generated` and `requires_human`.

### Precedence: determinism first, generation last

```text
1  exact approved answer
2  explicit AnswerRule
3  structured Career Profile / locked facts
4  CandidatePreferences
5  semantic reuse of an approved answer
6  grounded generation
7  NEEDS_HUMAN
```

`AnswerKnowledgeBase` was **not** replaced — it became the first stage of the resolver. Steps 3 and 5
needed their own implementation because the knowledge base reaches them only after the adapter's
confidence gate: a custom question the adapter does not recognise arrives with confidence `0.0`, so
`How many years of WordPress experience do you have?` resolved to nothing even though the fact was in
the profile, and a paraphrase never reused the approved answer. Reusing approved material is not
inferring field semantics: the evidence is the approved answer itself.

### Generation is only for discursive questions

The factual/discursive split is mechanical, not a judgement call:

```text
factual      identity, contact, years of experience, current company, salary, notice period,
             work authorization, sponsorship, relocation, education …
             -> a fact exists or the answer is NEEDS_HUMAN. No creativity.

discursive   "why are you interested", "describe", "tell us about", "why are you a good fit" …
             -> may be written, but only from authorized context.
```

When in doubt the resolver treats a question as **factual**. Generating where an objective fact was
missing is precisely the error this ADR exists to prevent.

### The model may write; it may not invent raw material

`CandidateContextBuilder` produces the *authorized* context, and every item carries its origin
(`profile.skills.wordpress`, `fact_qa_first_name`, `preferences.work_authorization`, …). The provider
receives that context and must answer with structure:

```json
{"answer": "…", "supported_by": ["profile.skills.wordpress"], "confidence": 0.9}
```

Text alone is not accepted.

### Claims are validated after generation

`FactValidator` does not trust `supported_by`. It checks that every source exists in the authorized
context, that `supported_by` is not empty, and that the answer's **numbers, money and capitalised
entities** are grounded. It blocks the classes the roadmap names: invented employer, role, years,
certification, metric, technology and salary. Capitalisation at the start of a sentence is grammar,
not a claim — treating it as one rejected legitimate answers, which the tests caught.

This is a gate, not a proof, and it is documented as such.

### Legal and self-identification are answered only from explicit data

Criminal history, disability, race/ethnicity, veteran status, visa and work authorization are never
generated. With an explicit fact or preference they are answered; without one they are `NEEDS_HUMAN`.
Autonomy does not mean fabricating an answer about the candidate's life.

### Field type is part of the answer

A semantically valid answer the form cannot receive is not an answer. Values are mapped to an existing
option for `select`/`radio`/`checkbox`; without a safe match the result is `NEEDS_HUMAN`. For
`textarea`, `max_length` and the job's language travel as generation constraints, and the result is
rejected if it exceeds them.

### Memory is not automatic

A generated answer is **not** promoted to a reusable approved answer. Some are application-specific
("why are you interested in this position at Company X"); others are semantically reusable ("how many
years of WordPress experience"). Distinguishing them, and ranking context, is JSA-QA-002. Until then
the generated answer lives in the application it was written for, marked `generated_grounded`.

## Consequences

- A question the agent cannot answer now produces a **reason**, not silence.
- The E2E no longer pre-approves `why_this_role`: the open question is generated from authorized
  context, validated, filled, submitted, and the persisted answer records `source=generated_grounded`
  with non-empty `supported_by`. Provenance is asserted, not assumed.
- Without a generator the same E2E stops at `NEEDS_ANSWER` with `unknown_answer:job_application[why_this_role]`
  — verified by mutation, so generation is load-bearing rather than decorative.
- Two latencies entered the flow and both are optional in the worst case: an unreliable generator
  fails the *question*, never the application (`generator_failed:*` → `NEEDS_HUMAN`).
- Improving generation quality, context ranking and cross-application answer memory is JSA-QA-002.
  Multi-step forms are JSA-LOOP-002.
