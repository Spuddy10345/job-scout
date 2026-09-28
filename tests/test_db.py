import sqlite3

from sqlalchemy import inspect

from app import db


def test_missing_columns_are_added_to_old_databases():
    with db.engine.begin() as conn:
        conn.exec_driver_sql('ALTER TABLE job DROP COLUMN score_attempts')
    assert "score_attempts" not in {c["name"] for c in inspect(db.engine).get_columns("job")}
    db.init_db()
    cols = {c["name"]: c for c in inspect(db.engine).get_columns("job")}
    assert "score_attempts" in cols
    with db.engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO job (fingerprint, title, company, location, remote, salary_text, description, url, "
                             "apply_url, sources, first_seen, last_seen, status, filtered_out, filter_reason, why, cv_angle, "
                             "seniority_fit, red_flags, apply_method, apply_steps, contacts, score_hash, company_website, "
                             "company_linkedin, cover_note, notified) VALUES ('f','t','','',0,'','','u','','[]','2026-01-01',"
                             "'2026-01-01','new',0,'','','','','[]','','[]','[]','','','','',0)")
        assert conn.exec_driver_sql("SELECT score_attempts FROM job").scalar() == 0
    assert sqlite3.sqlite_version_info >= (3, 35)  # DROP COLUMN above needs 3.35+
