# Resume diff view

Tailored sources are ordinary text and can be compared with:

```bash
diff -u output/<job_a>/resume.tex output/<job_b>/resume.tex
```

With the fictional example profile, the database-heavy posting in
`examples/evaluation_set.json` selects MiniDB first, while the distributed
systems posting selects TaskGrid. Skills are reordered around each posting's
supported requirements; unsupported technologies (for example Pandas when no
verified evidence exists) are excluded rather than added.
