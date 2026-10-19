# Follow-up work

## Open
- **JD requirement extraction is thin on some postings.** `jd.parse_job_description`
  sometimes yields only a handful of technical requirements, so skill/bullet
  ordering is driven more by the evidence pool than by the JD. Resumes stay
  truthful; tailoring could be sharper.
- **Cross-source dedupe is company+title based.** Re-posters (aggregators that
  list "<role> at <employer>") are not always merged with the employer's own listing.
- **No direct ATS board discovery.** Discovery is Simplify (+ optional LinkedIn
  via Apify); Greenhouse/Lever/Ashby/Workday/SmartRecruiters are detected and
  hydrated from listing URLs but not polled as sources.
- **LinkedIn rate limiting.** Some searches per run can lose results to HTTP 429s.
- **JS-rendered pages.** Static hydration cannot render JS-only postings; jobs
  with <300 chars of trusted text are parked as NEEDS_HYDRATION.
- **Student-visa exclusion regex misses long visa lists.** Wording such as
  "temporary visas such as E, F-1 (including those with OPT or CPT), H-1, ... are
  not eligible for hire" puts "are not eligible" more than 40 characters after
  "F-1", so `_STUDENT_VISA_EXCLUDED` does not fire. Widen the window within one
  sentence and add this wording as a regression test.
