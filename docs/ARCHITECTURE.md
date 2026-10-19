# Architecture and hallucination controls

```text
Job source / saved JD
        |
        v
Complete job record + retrieval metadata
        |
        v
JD parser: original wording + normalized aliases + priority + category
        |
        v
Evidence bank: stable EXP/PROJ/SKILL/METRIC IDs
        |
        v
Match matrix: exact / umbrella / adjacent / unsupported
        |
        v
Tailoring plan: selected experiences, projects, skills, evidence IDs
        |
        v
LaTeX -> PDF -> text / geometry / preview validation
        |
        v
Application preparation -> ATS adapter -> preflight -> explicit submit -> verified receipt
```

The truth gate requires every generated bullet to reference an ID in the
verified evidence bank. Unsupported job terms can be reported but cannot become
Technical Skills or resume claims. The machine gate rejects multi-page PDFs,
missing required text, clipped words, and same-line overlaps.

`jobbeast.jd` preserves employer wording in `original` and stores a normalized
canonical term. Each extracted item is classified as
`TECHNICAL_REQUIREMENT`, `DOMAIN_SIGNAL`, `RESPONSIBILITY`,
`EDUCATION_REQUIREMENT`, `ELIGIBILITY_LOGISTICS`, or `NICE_TO_HAVE`.
`jobbeast.matching` records evidence IDs and match type. Coverage is reported
separately for technical requirements, domain alignment, education, logistics,
and nice-to-have items; the resume optimization score weights technical and
domain evidence and does not treat authorization, location, relocation,
graduation timing, or application instructions as technical gaps.
`jobbeast.resume_builder` keeps each tailored plan separate from the candidate
vault, so a new role cannot rewrite the master history.

Application execution is isolated under `jobbeast.apply`. The SQLite store
persists the state machine (`DISCOVERED` through `SUBMITTED`/`NEEDS_HUMAN`),
transition events, application questions, and artifact hashes. Provider adapters
use deterministic Playwright field handling first. Generic or ambiguous fields,
CAPTCHA, MFA, and unverified success signals stop safely for human review.
The private answer vault is never synthesized from a job description and is
never required for a dry-run artifact build.
