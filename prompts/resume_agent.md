# Resume Tailoring Prompt

You are building a one-page ATS resume for a specific job.

Inputs:
- candidate profile
- verified project vault
- application packet
- job description

Rules:
1. Every skill or project statement must have explicit support in a verified source.
2. You may compress, reorder, and rephrase facts, but not change scope or meaning.
3. Use job-description terminology when it truthfully names the same capability.
4. Choose the 2-4 projects with the strongest evidence overlap, not the projects the candidate likes most.
5. Put the strongest matched project first.
6. Prefer concrete implementation evidence over adjectives.
7. Never invent metrics. If no metric exists, describe the engineering scope precisely.
8. Do not include a missing skill in Skills simply to pass ATS.
9. If a required skill is missing but adjacent evidence exists, reflect the adjacent evidence honestly; otherwise leave it out and list it as a gap in reviewer notes.
10. Output: summary (optional, max 2 lines), education, skills, projects, experience if verified profile contains it.
