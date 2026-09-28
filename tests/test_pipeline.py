from datetime import timedelta

import pytest
from sqlmodel import select

from app import geo, pipeline
from app.config import Centre, get_settings, save_settings
from app.db import Job, session, utcnow
from app.sources import RawJob

CARDIFF = (51.4816, -3.1791)
LONDON = (51.5072, -0.1276)


@pytest.fixture(autouse=True)
def cardiff_search_area():
    """Tests use their own search area rather than whatever config/defaults.yaml ships."""
    s = get_settings()
    s.search.centres = [Centre(name="Cardiff", lat=CARDIFF[0], lon=CARDIFF[1], radius_mi=25)]
    s.search.include_remote, s.search.max_age_days = True, 21
    save_settings(s)


def raw(**kw) -> RawJob:
    base = dict(title="Graduate Software Engineer", url="https://example.com/job/1", source="reed",
                company="Acme Ltd", location="Cardiff", lat=CARDIFF[0], lon=CARDIFF[1],
                description="Join our team building Python services.")
    base.update(kw)
    return RawJob(**base)


def all_jobs() -> list[Job]:
    with session() as s:
        return list(s.exec(select(Job)).all())


def test_same_job_from_two_sources_merges():
    settings = get_settings()
    pipeline.ingest([raw()], settings)
    stats = pipeline.ingest(
        [raw(source="adzuna", url="https://adzuna.co.uk/land/ad/999?utm_source=x", company="ACME LIMITED",
             description="Join our team building Python services. Longer text here.")],
        settings,
    )
    jobs = all_jobs()
    assert len(jobs) == 1
    assert stats == {"found": 1, "new": 0, "merged": 1, "filtered": 0}
    assert {s["source"] for s in jobs[0].sources} == {"reed", "adzuna"}
    assert jobs[0].description.endswith("Longer text here.")  # longer description wins


def test_duplicates_within_one_batch_merge():
    pipeline.ingest([raw(), raw(url="https://example.com/job/1?utm_campaign=z")], get_settings())
    assert len(all_jobs()) == 1


def test_distinct_jobs_stay_separate():
    pipeline.ingest([raw(), raw(title="IT Support Technician", url="https://example.com/job/2")], get_settings())
    assert len(all_jobs()) == 2


def test_radius_filter_keeps_local_drops_far():
    pipeline.ingest(
        [raw(), raw(title="Data Analyst", url="https://e.com/2", location="London", lat=LONDON[0], lon=LONDON[1])],
        get_settings(),
    )
    by_title = {j.title: j for j in all_jobs()}
    assert not by_title["Graduate Software Engineer"].filtered_out
    assert by_title["Graduate Software Engineer"].distance_mi < 1
    london = by_title["Data Analyst"]
    assert london.filtered_out and "outside search area" in london.filter_reason


def test_remote_role_outside_area_is_kept():
    pipeline.ingest([raw(title="Junior Developer (Remote)", location="London", lat=LONDON[0], lon=LONDON[1])], get_settings())
    job = all_jobs()[0]
    assert job.remote and not job.filtered_out


def test_seniority_and_experience_filters():
    pipeline.ingest(
        [
            raw(title="Senior Software Engineer", url="https://e.com/a"),
            raw(title="Software Engineer", url="https://e.com/b", description="You will have 6+ years of commercial experience."),
            raw(title="Leadership Programme Graduate", url="https://e.com/c"),  # 'lead' inside a word must not match
        ],
        get_settings(),
    )
    by_title = {j.title: j for j in all_jobs()}
    assert by_title["Senior Software Engineer"].filter_reason == "seniority: 'senior'"
    assert by_title["Software Engineer"].filter_reason == "asks for 6+ years"
    assert not by_title["Leadership Programme Graduate"].filtered_out


def test_old_adverts_filtered():
    pipeline.ingest([raw(posted_at=utcnow() - timedelta(days=60))], get_settings())
    assert all_jobs()[0].filter_reason.startswith("older than")


def test_salary_parsing():
    assert pipeline.parse_salary("£25,000 - £30,000 per annum") == (25000, 30000)
    assert pipeline.parse_salary("£28k") == (28000, 28000)
    assert pipeline.parse_salary("£12.50 per hour") == (12.5 * 1950, 12.5 * 1950)
    assert pipeline.parse_salary("Competitive") == (None, None)


def test_clean_url_strips_tracking():
    assert pipeline.clean_url("https://Example.com/job/1/?utm_source=a&id=5&gclid=x") == "https://example.com/job/1?id=5"


def test_haversine():
    assert 125 < geo.haversine_mi(*CARDIFF, *LONDON) < 140  # ~131 mi as the crow flies


def test_remote_roles_dropped_when_remote_switched_off():
    settings = get_settings()
    settings.search.include_remote = False
    pipeline.ingest([raw(title="Junior Developer (Remote)", location="London", lat=LONDON[0], lon=LONDON[1])], settings)
    job = all_jobs()[0]
    assert job.filtered_out and job.filter_reason == "remote roles switched off"


def test_salary_ranges_share_k_suffix():
    assert pipeline.parse_salary("£30-35k") == (30000, 35000)
    assert pipeline.parse_salary("£30k - £35k") == (30000, 35000)
    assert pipeline.parse_salary("£30k to 35") == (30000, 35000)
    assert pipeline.parse_salary("£12.50 - £14 per hour") == (12.5 * 1950, 14 * 1950)


def test_non_http_urls_are_not_ingested():
    stats = pipeline.ingest([raw(url="javascript:alert(1)")], get_settings())
    assert stats["new"] == 0 and not all_jobs()


def test_rescore_all_leaves_hidden_jobs_alone(monkeypatch):
    monkeypatch.setattr(pipeline, "score_pending", lambda *a, **k: 0)
    pipeline.ingest([raw(), raw(title="IT Support Technician", url="https://example.com/job/2")], get_settings())
    with session() as s:
        for j, status in zip(s.exec(select(Job).order_by(Job.id)).all(), ["new", "hidden"], strict=True):
            j.score, j.status = 50, status
            s.add(j)
        s.commit()
    pipeline.rescore_all()
    assert [j.score for j in sorted(all_jobs(), key=lambda j: j.id)] == [None, 50]
