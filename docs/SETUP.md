# Setup and demo

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

The PDF quality gate expects `tectonic`, `pdfinfo`, `pdftotext`, and `pdftoppm`
on `PATH`. The engine still supports `parse-jd` and `--dry-run` without TeX.

```bash
jobbeast validate-evidence
jobbeast tailor examples/epic_games_data_science_intern.json \
  --output-dir output/demo --tex-name resume.tex
```

This runs against the fictional `profiles/example/` profile unless
`JOBBEAST_PROFILE` / `JOBBEAST_PROFILE_DIR` points at your own. The output
directory contains LaTeX, PDF, PNG preview, JSON coverage, Markdown coverage,
and validation results. The tailoring command never submits an application.

## Application automation

Install the optional browser layer only when you are ready to test an employer
form:

```bash
pip install -e '.[apply]'
playwright install chromium
cp profiles/<you>/application_answers.example.yaml profiles/<you>/application_answers.yaml
```

Use `jobbeast apply JOB_ID --dry-run` to create and validate the canonical
resume artifact. A headed `jobbeast apply JOB_ID` can fill verified identity
fields and upload the job-specific resume, then stop before submission. Only
`jobbeast apply JOB_ID --submit` may attempt a submit action. Unknown legal or
sensitive questions, CAPTCHA, MFA, unknown provider forms, or missing
confirmation transition to `NEEDS_INPUT`/`NEEDS_HUMAN`; they are never guessed
or bypassed.
