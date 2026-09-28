from pathlib import Path

from app.sources.adzuna import Adzuna
from app.sources.brave import looks_like_posting, split_title
from app.sources.jobs_ac_uk import parse_search
from app.sources.reed import Reed

FIXTURES = Path(__file__).parent / "fixtures"


def test_jobs_ac_uk_parser():
    jobs = parse_search((FIXTURES / "jobs_ac_uk_search.html").read_text())
    assert len(jobs) >= 20
    first = jobs[0]
    assert first.title == "Software Engineer"
    assert first.company == "Imperial College London"
    assert first.location == "London"
    assert first.url == "https://www.jobs.ac.uk/job/DSW814/software-engineer"
    assert "£48,246" in first.salary_text


def test_adzuna_parse_ignores_predicted_salary():
    j = Adzuna()._parse({
        "title": "Junior Developer", "redirect_url": "https://adzuna/1", "company": {"display_name": "Acme"},
        "location": {"display_name": "Cardiff, South Glamorgan"}, "latitude": 51.48, "longitude": -3.18,
        "salary_min": 30000, "salary_max": 30000, "salary_is_predicted": "1", "created": "2026-09-20T10:00:00Z",
    })
    assert j.salary_min is None and j.salary_text == ""
    assert j.posted_at.year == 2026 and j.lat == 51.48


def test_reed_parse():
    j = Reed()._parse({"jobTitle": "IT Support", "jobUrl": "https://reed/1", "employerName": "X",
                       "locationName": "Newport", "minimumSalary": 24000, "maximumSalary": 26000, "date": "20/09/2026"})
    assert j.salary_text == "£24,000 – £26,000" and j.posted_at.day == 20


def test_brave_posting_heuristics():
    assert looks_like_posting("https://www.civilservicejobs.service.gov.uk/csr/jobs.cgi?jcode=1234567")
    assert looks_like_posting("https://boards.greenhouse.io/monzo/jobs/123456")
    assert looks_like_posting("https://acme.co.uk/careers/vacancies/graduate-engineer")
    assert not looks_like_posting("https://uk.indeed.com/jobs?q=developer&l=Cardiff")
    assert not looks_like_posting("https://www.example.com/about")
    assert split_title("Graduate Software Engineer - Acme Ltd | Careers") == ("Graduate Software Engineer", "Acme Ltd")


def test_workday_posted_on():
    from app.sources.watchlist import _workday_posted

    assert _workday_posted("Posted Today").date() == _workday_posted("Posted 0 Days Ago").date()
    assert (_workday_posted("Posted Yesterday").date() - _workday_posted("Posted 30+ Days Ago").date()).days == 29
    assert _workday_posted("") is None
