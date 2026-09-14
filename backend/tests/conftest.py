# Shared pytest fixtures for Phase 1 endpoint tests. Both implementation
# tracks (patients/routing and availability/appointments/calls/dashboard)
# import from here rather than each standing up their own app/DB plumbing.
#
# Uses a dedicated `mediportal_test` Postgres database (never the dev DB),
# with tables created once per test session via db.create_all() (not
# migrations -- faster, and migration-correctness is already covered by
# Phase 0's tests). Each test runs inside a SAVEPOINT that's rolled back
# afterward, so tests never see each other's data and never need manual
# cleanup.
import os

import pytest

from app import create_app
from app.config import Config
from app.extensions import db as _db


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "TEST_DATABASE_URL", "postgresql://localhost/mediportal_test"
    )
    AGENT_KEY = "test-agent-key"
    SECRET_KEY = "test-secret"


@pytest.fixture(scope="session")
def app():
    application = create_app(TestConfig)
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def db(app):
    """A SQLAlchemy session scoped to one test, rolled back afterward.

    Two things had to be true for this to actually isolate tests, and the
    original version of this fixture got both wrong (verified empirically --
    committed rows were leaking across tests and surviving rollback):

    1. `db.session` is a `scoped_session` proxy with no `__setattr__`
       override, so `_db.session.bind = connection` just sets an inert
       attribute on the proxy -- it never reaches the real Session. Fix:
       replace `db.session` outright with a fresh scoped_session for the
       duration of the test (`_db._make_scoped_session(...)`), then restore
       the original afterward.
    2. Flask-SQLAlchemy's `Session.get_bind()` (see `flask_sqlalchemy.session`)
       checks `self._db.engines` for a bind-key engine *before* falling back
       to the session's own configured bind, so passing `bind=connection` to
       the sessionmaker is silently ignored -- every query still goes through
       the real engine's connection pool. Fix: temporarily swap the app's
       registered default engine (`db._app_engines[app][None]`) for our
       connection, so `get_bind()` resolves to it. With that in place,
       `join_transaction_mode="create_savepoint"` makes route code's real
       `db.session.commit()` calls release/reopen a SAVEPOINT instead of
       ending our outer transaction, so the final `transaction.rollback()`
       actually undoes everything.
    """
    with app.app_context():
        connection = _db.engine.connect()
        transaction = connection.begin()

        engines = _db._app_engines[app]
        original_engine = engines[None]
        engines[None] = connection

        original_session = _db.session
        _db.session = _db._make_scoped_session({"join_transaction_mode": "create_savepoint"})

        yield _db.session

        _db.session.remove()
        _db.session = original_session
        engines[None] = original_engine
        transaction.rollback()
        connection.close()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def agent_headers(app):
    """Headers a Vogent-originated request presents -- see spec §5's intro
    on the shared-secret AGENT_KEY header, distinct from the dashboard's
    per-user JWT auth (§5.8)."""
    return {"X-Agent-Key": app.config["AGENT_KEY"]}
