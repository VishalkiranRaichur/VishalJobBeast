from pathlib import Path

import jobbeast.resume_builder as builder


def test_arondite_style_two_page_resume_compacts_to_one(monkeypatch, tmp_path):
    plan = {
        "experiences": [],
        "projects": [
            {"name": "Strong", "stack": "Python", "bullets": [{"evidence_ids": ["A"], "text": "strong"}, {"evidence_ids": ["B"], "text": "secondary"}]},
            {"name": "Lower", "stack": "SQL", "bullets": [{"evidence_ids": ["C"], "text": "lower"}, {"evidence_ids": ["D"], "text": "lowest"}]},
        ],
        "skills": {"Languages": "Python, SQL"}, "skill_evidence": {},
    }
    pages = iter([2, 1])
    monkeypatch.setattr(builder, "compile_tex", lambda _path: (True, "ok"))
    monkeypatch.setattr(builder, "_pdf_page_count", lambda _path: next(pages))
    monkeypatch.setattr(builder, "build_requirement_matrix", lambda _job: {"requirements": [], "coverage": {}, "matches": []})
    monkeypatch.setattr(builder, "write_application_report", lambda **_kwargs: None)
    result = builder.build_application(job={"company": "Arondite", "title": "Software Engineer Intern", "description": "", "url": "https://example.test"}, output_dir=tmp_path, plan=plan)
    assert result["compiled"] is True
    assert result["compaction"]["original_page_count"] == 2
    assert result["compaction"]["final_page_count"] == 1
    assert result["compaction"]["compaction_stage"] == 1
    assert result["compaction"]["removed_content_ids"] == ["D"]
