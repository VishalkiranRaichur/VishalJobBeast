# Job Beast

**Job Beast is an internship/job discovery and application-preparation system that
prioritizes fresh, eligible, high-fit opportunities — and never lies on your behalf.**

It watches job feeds, throws out roles you can't take, ranks the rest by fit *and*
freshness, reads the full job description, matches every requirement to evidence you
have actually verified, and generates a tailored one-page ATS resume that is
machine-validated before anyone sees it. It then prepares a self-contained packet for
an (optional) browser operator to fill the application, hands CAPTCHA/MFA and
sensitive questions to *you*, and records the outcome only when the employer
confirms it.

> Built for students targeting **Summer 2027** internships, but the season, titles,
> and profile are all configurable.

---

## What it does

| Capability | What happens |
|---|---|
| **Discovery** | Pulls the SimplifyJobs Summer 2027 feed, optionally LinkedIn (via Apify) and JobSpy boards. |
| **Fresh Job Radar** | `fresh` / `watch` sync sources and rank by *fit + bounded freshness bonus*; postings ≤3h old with fit ≥70 go to a `FAST_LANE`. |
| **Deduplication** | Canonical URLs + company/title fingerprints merge the same role seen in several feeds; prior submissions block re-applying. |
| **Hard eligibility filtering** | Conservative gates for U.S.-person/export control, no-sponsorship (when your profile needs it), mandatory graduate degree, high-school-only, explicit graduation windows, security clearance, mandatory location, student-visa exclusions. Ambiguous cases become `NEEDS_CONFIRMATION`, not guesses. |
| **JD hydration** | Fetches the official posting (Greenhouse, Lever, Ashby, SmartRecruiters, Workday, …). Jobs with too little trustworthy text are parked as `NEEDS_HYDRATION`. |
| **JD analysis** | Preserves employer wording, normalizes aliases, and classifies each item as technical, domain, responsibility, education, eligibility/logistics, or nice-to-have. |
| **Verified-evidence matching** | Every requirement is `SUPPORTED_EXACT`, `SUPPORTED_UMBRELLA`, `SUPPORTED_ADJACENT`, or `UNSUPPORTED` against stable evidence IDs (`EXP-*`, `PROJ-*`, `SKILL-*`, `METRIC-*`). Unsupported skills are reported, never added. |
| **Tailored ATS resume** | Builds a LaTeX resume (Jake's Resume layout) from selected evidence, reorders projects and skills for the role. |
| **One-page validation** | Compiles to PDF and rejects multi-page output, clipped words, overlapping lines, missing name/sections, or forbidden terms. |
| **Claude/browser handoff** | `handoff` writes a packet, operator instructions, and a result template for *your* browser agent. |
| **Human verification** | CAPTCHA → `READY_FOR_HUMAN_CAPTCHA`, MFA → `READY_FOR_HUMAN_MFA`, unknown legal wording → `NEEDS_INPUT`. |
| **Result tracking** | `import-result` ingests the operator's result; `SUBMITTED` requires an employer confirmation, otherwise `SUBMITTED_UNVERIFIED`. |
| **Failure isolation** | One failing source, job, or compile never stops the rest of a sync or batch. |

## Architecture

```text
Sources (Simplify · optional LinkedIn/Apify · optional JobSpy)
        ↓
Fresh Job Radar  (sync, fit + freshness ranking, FAST_LANE / BACKLOG)
        ↓
Deduplication    (canonical URL + fingerprint; prior submissions block)
        ↓
Eligibility      (hard gates → INELIGIBLE / NEEDS_CONFIRMATION / ELIGIBLE)
        ↓
JD Hydration     (official posting text; NEEDS_HYDRATION if too thin)
        ↓
Fit / Requirement Matching   (category-aware, evidence-ID matrix)
        ↓
Tailored Resume  (LaTeX from verified evidence only)
        ↓
Validation       (one page, required text, no clipping/overlap, forbidden terms)
        ↓
Browser Operator Handoff     (packet + instructions + result template)
        ↓
Human Verification if Required  (CAPTCHA · MFA · legal/sensitive questions)
        ↓
Submission Result Tracking   (SUBMITTED only with employer confirmation)
```

Code map: `sources/` (feeds) · `radar.py` (fresh radar, queue) · `store.py` (SQLite)
· `eligibility.py` · `hydration.py` · `jd.py` + `matching.py` (analysis) ·
`resume_builder.py` + `validation.py` (resume) · `handoff.py` (operator packets and
result import) · `apply/` (optional Playwright form automation) · `browser_extension/`
(optional Chrome companion). See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Quick start

Requirements: Python ≥ 3.10. For PDF output and validation also install
[`tectonic`](https://tectonic-typesetting.github.io/) and poppler
(`pdfinfo`, `pdftotext`, `pdftoppm`), e.g. `brew install tectonic poppler`.
Requirement parsing and `--dry-run` work without them.

```bash
# 1. Get the code
git clone https://github.com/VishalkiranRaichur/VishalJobBeast.git
cd VishalJobBeast

# 2. Virtual environment + install
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 3. Environment (every variable is optional)
cp .env.example .env
set -a; source .env; set +a        # export the variables into this shell

# 4. Run the tests (they use the fictional example profile)
python -m pytest -q

# 5. Sanity checks
jobbeast doctor
jobbeast init
jobbeast validate-evidence
```

### Create your own candidate profile

The repo ships a **fictional** profile in `profiles/example/` ("Alex Rivera").
Copy it, then replace every fact with your own *verified* facts:

```bash
cp -r profiles/example profiles/me
export JOBBEAST_PROFILE=me                 # or put it in .env
cp profiles/me/application_answers.example.yaml profiles/me/application_answers.yaml
cp profiles/me/answers.private.example.yaml   profiles/me/answers.private.yaml
```

- `profile.yaml` — identity, education, experience (with claims), core skills.
  Optional `work_preferences.sponsorship_now/sponsorship_future` feed the
  sponsorship eligibility gate.
- `projects/*.yaml` — one file per project; only `verified: true` projects are
  ever used. Give each a `type` and `tags` (e.g. `database`, `distributed`, `rag`).
- `evidence.yaml` — stable `EXP-*`/`PROJ-*`/`SKILL-*`/`METRIC-*` items; every
  resume bullet must trace to one.
- `application_answers.yaml` / `answers.private.yaml` — your answer vault. Leave
  anything you haven't personally confirmed blank or `REQUIRES_USER_INPUT`.

Everything under `profiles/` except `profiles/example/` is gitignored. To keep your
data entirely outside the repo, set `JOBBEAST_PROFILE_DIR=/absolute/path/to/profile`.

### Configure sources

Edit `config.yaml`:

- `simplify.raw_url` — the SimplifyJobs Summer 2027 `listings.json` feed (default).
- `search.target_titles` / `preferred_locations` — what counts as on-target.
- `apify_linkedin` — optional LinkedIn source settings (see below).
- `automation` — daily/per-run limits and minimum fit score.

### Run the radar

```bash
jobbeast fresh --top 25                          # one sync + rank of fresh jobs
jobbeast watch --sources simplify --interval 30  # keep polling every 30 minutes
jobbeast list --limit 20 --min-score 55          # browse ranked jobs
jobbeast hydrate <JOB_ID>                        # fetch the official posting
```

### Prepare applications

```bash
# Try the pipeline on a bundled job description first:
jobbeast parse-jd examples/epic_games_data_science_intern.json
jobbeast tailor examples/epic_games_data_science_intern.json --output-dir output/demo
jobbeast coverage examples/epic_games_data_science_intern.json --output-dir output/demo

# Real queue:
jobbeast next                       # highest-priority eligible job
jobbeast handoff <JOB_ID>           # packet + resume + operator instructions
jobbeast handoff-batch --limit 5    # several, with per-job failure isolation
jobbeast import-result <JOB_ID>     # ingest output/<JOB_ID>/claude_result.json
jobbeast dashboard                  # HTML overview of the queue
jobbeast export-csv
```

`handoff` writes `output/<JOB_ID>/` with the tailored `resume_final.pdf`,
`application_packet.json`, `application_context.md`, `claude_browser_prompt.md`,
`requirement_coverage.md`, and a `claude_result.json` template. Submission is only
authorized when you pass `--authorize-submission`.

## Browser operation (optional)

Job Beast does **not** bundle an AI browser agent. The contract is:

```text
Job Beast prepares  →  your browser operator executes  →  Job Beast imports results
   (handoff)              (fills the live form)           (import-result)
```

You can drive the operator with any supported agent that can read the packet and
write `claude_result.json` — for example Claude in Chrome / Claude Code, or another
browser agent you configure yourself. The operator prompt it receives
(`claude_browser_prompt.md`) encodes the safety rules below. Your own browser
sessions, cookies, and agent credentials stay on your machine and must never be
committed.

For deterministic automation without an AI agent there is also an optional
Playwright layer:

```bash
pip install -e ".[apply]"
playwright install chromium
jobbeast apply <JOB_ID> --dry-run           # build/validate artifacts only (default)
jobbeast apply <JOB_ID> --headed            # fill and stop before submit
jobbeast apply <JOB_ID> --submit --headed   # attempt submit; still stops on anything unverified
```

## LinkedIn via Apify (optional)

The LinkedIn source is **off unless you enable it**.

- It uses an [Apify](https://apify.com) actor to read **public, logged-out** LinkedIn
  job search results. You need **your own Apify account and token**
  (`APIFY_TOKEN` in `.env`); Apify bills per result.
- **Never** supply LinkedIn cookies, session state, or account credentials — Job
  Beast never asks for them and ships no authenticated LinkedIn state.
- You are responsible for complying with the terms of LinkedIn, Apify, and any
  other site you use.
- Rate limiting (HTTP 429) and partial results can occur; the radar tolerates them.

```bash
jobbeast fetch-linkedin --dry-run        # print the search URLs, no Apify call
jobbeast fetch-linkedin --top 25
jobbeast watch --sources simplify linkedin --interval 60
```

Without a token the LinkedIn commands exit with a clear message and the Simplify
source keeps working.

## Application automation safety

These invariants are enforced in code and in the operator instructions:

- **Never fabricate skills.** Unsupported requirements are reported as gaps, never
  added to Technical Skills.
- **Never fabricate experience.** Every resume bullet traces to a verified evidence ID;
  `verified: false` projects and `forbidden_resume_terms` are blocked by validation.
- **Never guess sensitive or legal answers.** Work authorization, sponsorship,
  immigration, export control, clearance, attestations, and EEO questions use only
  answers you confirmed, for questions with the same material meaning — otherwise
  `NEEDS_INPUT`. EEO values are stripped from every packet.
- **Never bypass CAPTCHA.** It is left for you (`READY_FOR_HUMAN_CAPTCHA`).
- **Never bypass MFA.** It is left for you (`READY_FOR_HUMAN_MFA`).
- **Duplicate applications are prevented.** A recorded submission (verified or not)
  blocks re-preparing the same job.
- **Submitted requires confirmation.** A click on "Submit" is not success; without
  employer confirmation the result is `SUBMITTED_UNVERIFIED`.
- **Job descriptions are untrusted data.** Instructions embedded in a posting are
  never followed.

## Project layout

```text
src/jobbeast/         core package (CLI: `jobbeast`)
profiles/example/     fictional example candidate (copy this)
config.yaml           sources, titles, limits
templates/            LaTeX resume layout
examples/             sample job descriptions and explanatory notes
browser_extension/    optional Chrome companion (unpacked extension)
docs/                 architecture, setup, LLM gateway notes
tests/                pytest suite (runs offline against the example profile)
```

## Contributing, security, license

- [CONTRIBUTING.md](CONTRIBUTING.md) — setup, ground rules, PR checklist.
- [SECURITY.md](SECURITY.md) — what must never be committed; how to report issues.
- [LICENSE](LICENSE) — MIT. Third-party attributions: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md),
  upstream data/projects: [UPSTREAMS.md](UPSTREAMS.md).

Created by [Vishalkiran Raichur](https://github.com/VishalkiranRaichur).
