from pathlib import Path
from jobbeast.sources.simplify import fetch_file


def test_offline_simplify_parser_filters_inactive():
    path = Path(__file__).parent / "fixtures" / "simplify_sample.json"
    jobs = fetch_file(path)
    assert len(jobs) == 1
    assert jobs[0].company == "ExampleAI"
    assert jobs[0].title == "Machine Learning Engineer Intern"
