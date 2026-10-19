from jobbeast.models import Job
from jobbeast.tailor import choose_projects


def row_from_job(job):
    import json
    return {
        'id': job.id,
        'title': job.title,
        'company': job.company,
        'description': job.description,
        'locations_json': json.dumps(job.locations),
        'source': job.source,
        'date_posted': None,
        'url': job.url,
    }


def test_database_role_prefers_database_project():
    job = Job(
        source='fixture', company='Acme', title='Backend Database Engineering Intern', url='x',
        locations=['San Jose, CA'],
        description='Build SQL storage engines, indexing, caching, and backend systems in Java.'
    )
    selected = choose_projects(row_from_job(job))
    assert selected[0]['name'] == 'MiniDB'


def test_reliability_agent_role_surfaces_reliability_project():
    job = Job(
        source='fixture', company='Acme', title='AI Reliability Intern', url='x',
        description='Work on agent workflows, incident reliability, Stripe integrations, idempotency and verification.'
    )
    selected = choose_projects(row_from_job(job))
    assert selected[0]['name'] == 'IncidentBot'
