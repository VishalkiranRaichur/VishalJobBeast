# Contributing

Thanks for helping improve Job Beast.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
PYTHONPATH=src python -m pytest -q
```

Tests run against the fictional `profiles/example/` profile. They must pass
without any `.env`, network access to Apify, or a private profile.

## Ground rules

- **Truthfulness is the product.** Changes must never let a resume or answer
  claim a skill, metric, date, or experience that is not in verified evidence.
- **No real personal data** in code, tests, fixtures, or issues. Use the
  fictional example profile and `example.com` / `555-01xx` style placeholders.
- **No credentials, cookies, or browser state** anywhere in a PR.
- Sensitive or legal answers (work authorization, sponsorship, EEO, attestations)
  must stay "ask the human" unless explicitly configured by the user.
- Never add CAPTCHA/MFA solving or bot-detection evasion.
- One source or job failing must not break the rest of a run (failure isolation).

## Pull requests

1. Keep PRs focused, with a test for each behavior change.
2. Run `PYTHONPATH=src python -m pytest -q` and a secret scan
   (`gitleaks dir . --redact`) before opening the PR.
3. Describe user-visible behavior changes in the PR description.
