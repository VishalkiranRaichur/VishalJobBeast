# Security and privacy

Job Beast handles some of the most sensitive data a person has: contact details,
immigration and work-authorization answers, voluntary EEO self-identification,
transcripts, and employer application records. Treat your local copy accordingly.

## Never commit

- `.env` or any real API key/token (Apify, LLM gateways, GitHub, OpenAI, Anthropic, ...)
- your candidate profile (`profiles/<you>/`), especially `answers.private.yaml`,
  `application_answers.yaml`, and `private/`
- EEO / demographic answers (gender, race/ethnicity, veteran, disability)
- immigration, sponsorship, or legal attestation answers
- resumes, transcripts, and anything under `output/`
- the SQLite database (`data/jobs.db`) or any `*.db` / `*.sqlite` file
- browser profiles, cookies, storage state, or session files (`data/browser-profile/`)
- application screenshots, confirmation pages, or result JSON files

The provided `.gitignore` excludes all of the above, and only the fictional
`profiles/example/` profile is tracked. Keep your real profile out of the repo
entirely by pointing `JOBBEAST_PROFILE_DIR` at a directory outside it.

Before every push, run a secret scan, for example:

```bash
gitleaks git . --redact
gitleaks dir . --redact
```

## Design guarantees

- EEO/demographic values are stripped before any packet or prompt is built.
- The LinkedIn source uses Apify public search; Job Beast never asks for or
  stores LinkedIn credentials or cookies.
- CAPTCHA and MFA are never solved or bypassed; they are handed to the human.

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Use GitHub's
private vulnerability reporting ("Security" tab → "Report a vulnerability") on
this repository, with steps to reproduce. Never include real personal data or
credentials in a report.
