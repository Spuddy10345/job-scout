"""Fill a throwaway database with fictional jobs, applications and usage - for screenshots and UI work.

    JOBSCOUT_DATA=/tmp/jobscout-demo uv run python scripts/seed_demo.py
    JOBSCOUT_DATA=/tmp/jobscout-demo JOBSCOUT_DISABLE_SCHEDULER=1 uv run uvicorn app.main:app --port 8081

Every company, person and address here is made up.
"""

from __future__ import annotations

import os
import random
import sys
from datetime import timedelta
from pathlib import Path

if not os.environ.get("JOBSCOUT_DATA"):
    sys.exit("Set JOBSCOUT_DATA to an empty directory - this script must not touch your real database.")
os.environ.setdefault("JOBSCOUT_NO_DOTENV", "1")
os.environ.setdefault("JOBSCOUT_CONFIG", os.devnull + ".missing")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings, save_settings, set_kv  # noqa: E402
from sqlmodel import select  # noqa: E402

from app.db import Job, JobEvent, SourceRun, init_db, session, utcnow  # noqa: E402

random.seed(7)
now = utcnow()
PLACES = {"London": (51.5072, -0.1276), "Manchester": (53.4808, -2.2426), "Salford": (53.4875, -2.2901),
          "Stockport": (53.4106, -2.1575), "Croydon": (51.3762, -0.0982), "Watford": (51.6565, -0.3903)}
JOBS = [
    ("Graduate Software Engineer", "Copperleaf Software", "London", "SWE", 91, "watchlist", 32000, 36000),
    ("Junior Backend Developer (Python)", "Harbour Analytics", "Manchester", "SWE", 86, "reed", 30000, 34000),
    ("Security Operations Graduate", "Kestrel Security", "Salford", "Security", 84, "adzuna", 29000, 31000),
    ("Data Analyst - Graduate Scheme", "Brightline Health", "London", "AI-Data", 78, "brave", 31000, 31000),
    ("Research Software Engineer", "Ashgrove University", "Manchester", "SWE", 76, "jobs_ac_uk", 33966, 38205),
    ("Junior DevOps Engineer", "Northwind Robotics", "Stockport", "SWE", 74, "adzuna", 32000, 38000),
    ("QA Test Engineer", "Lumen Payments", "Remote (UK)", "SWE", 71, "reed", 28000, 32000),
    ("Embedded Software Graduate", "Northwind Robotics", "Stockport", "Hardware-Embedded", 69, "watchlist", 30000, 33000),
    ("IT Support Analyst", "Fenwick & Hale", "Croydon", "IT-Support", 58, "reed", 25000, 27000),
    ("Junior Web Developer", "Pixel & Thread Studio", "Manchester", "SWE", 64, "adzuna", 26000, 29000),
    ("Machine Learning Intern", "Harbour Analytics", "Remote (UK)", "AI-Data", 62, "brave", None, None),
    ("Service Desk Technician", "Ashgrove University", "Manchester", "IT-Support", 55, "jobs_ac_uk", 24500, 26000),
    ("Cloud Support Associate", "Stratus Hosting", "Watford", "IT-Support", 52, "adzuna", 27000, 30000),
    ("Technical Sales Graduate", "Lumen Payments", "London", "Adjacent", 34, "reed", 28000, 28000),
    ("Senior Platform Engineer", "Copperleaf Software", "London", "SWE", 22, "watchlist", 70000, 85000),
]
WHY = {
    "SWE": "Graduate-level engineering role that asks for Python and some cloud exposure - both on the profile.",
    "Security": "Entry-level security role with training; matches the security interest and home-lab experience.",
    "AI-Data": "Early-career data role using Python and SQL, with room to automate reporting.",
    "Hardware-Embedded": "Graduate embedded role; C is listed as a plus rather than a must-have.",
    "IT-Support": "Foot-in-the-door role with scope to script common fixes and improve the ticket workflow.",
    "Adjacent": "Customer-facing sales role that only loosely uses technical skills.",
}
STEPS = ["Tailor your CV to the requirements listed", "Apply through the employer's application page",
         "Answer the screening questions with one concrete example each"]


def main() -> None:
    init_db()
    s = get_settings()
    s.profile_md = ("- BSc Computer Science, 2026 - 2:1\n- Python, TypeScript, SQL, Docker\n"
                    "- Built a self-hosted job search tool (FastAPI, SQLite, LLM scoring)\n"
                    "- Interested in backend, data and security\n- Based in Manchester, open to London and remote")
    save_settings(s)
    set_kv("setup_done", True)
    with session() as db:
        for i, (title, company, loc, cat, score, source, smin, smax) in enumerate(JOBS * 3):
            copy = i // len(JOBS)
            if copy:
                title = f"{title} {'II' if copy == 1 else '(Contract)'}"
                score = max(5, score - 7 * copy)
            lat, lon = PLACES.get(loc, (None, None))
            remote = loc.startswith("Remote")
            seen = now - timedelta(days=random.randint(0, 55), hours=random.randint(0, 23))
            url = f"https://careers.example.com/{company.split()[0].lower()}/{i}"
            job = Job(
                fingerprint=f"demo{i}", title=title, company=company, location=loc, lat=lat, lon=lon, remote=remote,
                distance_mi=None if lat is None else round(random.uniform(0.5, 14), 1),
                nearest_centre=None if lat is None else ("London" if lon > -1 else "Manchester"),
                salary_min=smin, salary_max=smax, salary_text=f"£{smin:,} - £{smax:,}" if smin else "",
                description=f"{company} is hiring a {title}. You'll work in a small team shipping features weekly.",
                url=url, sources=[{"source": source, "url": url, "apply_url": "", "seen_at": seen.isoformat()}],
                posted_at=seen - timedelta(days=1), first_seen=seen, last_seen=now, score=score, category=cat,
                why=WHY[cat], cv_angle="Automated a weekly report with Python, cutting the time it took from 2 hours to 10 minutes",
                seniority_fit="entry" if score > 60 else "stretch", apply_method="company-ats", apply_steps=STEPS,
                red_flags=["asks for 5+ years"] if "Senior" in title else [],
                contacts=[{"kind": "email", "value": "talent@example.com", "label": "Recruitment team"}] if i % 4 == 0 else [],
                notified=True,
            )
            db.add(job)
        db.commit()
        jobs = db.exec(select(Job).order_by(Job.score.desc())).all()
        plan = {0: "interview", 1: "applied", 2: "applied", 3: "interested", 4: "interested", 5: "applied",
                6: "rejected", 8: "interested", 10: "offer"}
        for idx, status in plan.items():
            job = jobs[idx]
            path = ["interested", "applied", "interview", "offer"]
            stages = path[:path.index(status) + 1] if status in path else ["interested", "applied", "rejected"]
            prev, at = "new", job.first_seen
            for st in stages:
                at += timedelta(days=random.randint(1, 4))
                db.add(JobEvent(job_id=job.id, kind="status", text=f"{prev} → {st}", at=min(at, now)))
                prev = st
            job.status = status
            db.add(job)
        db.add(JobEvent(job_id=jobs[0].id, kind="note", text="Phone screen booked with the hiring manager for Thursday",
                        at=now - timedelta(days=1)))
        for name in ("adzuna", "reed", "jobs_ac_uk", "brave", "watchlist"):
            for h in (30, 6):
                db.add(SourceRun(source=name, started_at=now - timedelta(hours=h), finished_at=now - timedelta(hours=h) + timedelta(seconds=40),
                                 found=random.randint(40, 180), new=random.randint(2, 20), merged=random.randint(0, 8),
                                 filtered=random.randint(5, 60), scored=random.randint(2, 20)))
        db.commit()
    usage = {}
    for d in range(30):
        day = (now - timedelta(days=d)).date().isoformat()
        calls = random.randint(15, 70)
        usage[day] = {"calls": calls, "usd": round(calls * random.uniform(0.0022, 0.0035), 4), "in": calls * 1800,
                      "out": calls * 350, "cache_read": calls * 3000, "cache_write": 4000}
    set_kv("ai_usage", usage)
    set_kv("firecrawl_usage", {(now - timedelta(days=d)).date().isoformat(): random.randint(0, 12) for d in range(30)})
    print(f"Seeded {len(JOBS) * 3} demo jobs into {os.environ['JOBSCOUT_DATA']}")


if __name__ == "__main__":
    main()
