# Requirement-to-evidence view

The generated `requirement_coverage.md` retains employer wording, normalized
term, priority, match type, evidence IDs, and whether that evidence was used in
the resume.

| Priority | Original wording | Normalized | Match | Evidence | Resume location |
|---|---|---|---|---|---|
| MUST_HAVE | Experience with Python for data analysis | python | SUPPORTED_EXACT | EXP-001, PROJ-003 | demonstrated |
| PREFERRED | pandas preferred | pandas | SUPPORTED_ADJACENT | EXP-001 | excluded from skills |
| PREFERRED | anomaly detection | anomaly detection | UNSUPPORTED | — | not represented |

The exact report is generated from the current job and evidence bank; this file
is a conceptual view.
