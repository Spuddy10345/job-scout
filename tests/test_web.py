"""Pages render and the list/tracker actions work."""

import csv
import io

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.config import get_settings, save_settings, set_kv
from app.db import Job, JobEvent, session
from app.main import app


@pytest.fixture
def client():
    save_settings(get_settings())  # a configured install, so / doesn't redirect to /setup
    return TestClient(app)


def add_jobs(n: int, **kw) -> list[int]:
    with session() as s:
        jobs = [Job(fingerprint=f"f{i}", title=kw.get("title", f"Developer {i}"), company="Acme", url=f"https://e.com/{i}",
                    score=kw.get("score", 50 + i % 50)) for i in range(n)]
        s.add_all(jobs)
        s.commit()
        return [j.id for j in jobs]


@pytest.mark.parametrize("path", ["/", "/tracker", "/runs", "/settings", "/stats", "/setup", "/?limit=abc&offset=x"])
def test_pages_render(client, path):
    add_jobs(3)
    assert client.get(path).status_code == 200


def test_fresh_install_starts_at_setup():
    from app.config import settings_saved

    assert not settings_saved()  # each test starts with an empty database
    c = TestClient(app)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    c.post("/setup/done")
    assert c.get("/", follow_redirects=False).status_code == 200


def test_first_page_then_load_more(client):
    add_jobs(130)
    page = client.get("/").text
    assert page.count('<tr data-id=') == 100 and "30 left" in page
    more = client.get("/jobs/rows?offset=100").text
    assert more.count('<tr data-id=') == 30 and 'hx-swap-oob="true"' in more and "left)" not in more


def test_bulk_status_change_records_history(client):
    ids = add_jobs(3)
    r = client.post("/jobs/bulk", data={"ids": [str(i) for i in ids[:2]], "action": "interested"})
    assert r.status_code == 200
    with session() as s:
        statuses = [j.status for j in s.exec(select(Job).order_by(Job.id)).all()]
        events = s.exec(select(JobEvent)).all()
    assert statuses == ["interested", "interested", "new"] and len(events) == 2


def test_csv_export_neutralises_formulas(client):
    add_jobs(1, title="=HYPERLINK(\"http://evil\")")
    r = client.get("/export/jobs.csv")
    assert r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert rows[0]["title"].startswith("'=")


def test_saved_views(client):
    r = client.post("/views", data={"name": "Strong remote", "query": "?min_score=70&remote=only"})
    assert "Strong remote" in r.text and 'value="min_score=70&amp;remote=only"' in r.text
    r = client.post("/views/delete", data={"name": "Strong remote"})
    assert "Strong remote" not in r.text


def test_stats_funnel_counts_furthest_stage(client):
    ids = add_jobs(2)
    with session() as s:
        j = s.get(Job, ids[0])
        j.status = "rejected"
        s.add(j)
        s.add(JobEvent(job_id=ids[0], kind="status", text="applied → interview"))
        s.commit()
    from app.web.extras import _stats

    funnel = {r["label"]: r["value"] for r in _stats()["funnel"]}
    assert funnel == {"Interested": 1, "Applied": 1, "Interview": 1, "Offer": 0}


def test_tracker_export(client):
    ids = add_jobs(1)
    client.post(f"/job/{ids[0]}/status", data={"status": "applied"})
    rows = list(csv.DictReader(io.StringIO(client.get("/export/tracker.csv").text)))
    assert rows[0]["status"] == "applied" and "new → applied" in rows[0]["history"]


def test_run_all_only_queues_ready_sources(client, monkeypatch):
    queued = []
    monkeypatch.setattr("app.scheduler.run_now", queued.append)
    set_kv("setup_done", True)
    client.post("/runs")
    assert "jobs_ac_uk" in queued and "adzuna" not in queued  # no Adzuna keys in tests
