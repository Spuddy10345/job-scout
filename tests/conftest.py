import os
import tempfile

# Point the app at a throwaway database before anything imports app.db.
os.environ["JOBSCOUT_DATA"] = tempfile.mkdtemp(prefix="jobscout-test-")

import pytest  # noqa: E402

from app import db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    db.SQLModel.metadata.drop_all(db.engine)
    db.init_db()
    from app import config

    config._cache = None
    yield
