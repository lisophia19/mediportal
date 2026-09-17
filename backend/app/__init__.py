from flask import Flask

from .config import Config
from .extensions import db, migrate


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    migrate.init_app(app, db)

    with app.app_context():
        from . import models  # noqa: F401  -- registers tables with db.metadata

    _register_blueprints(app)
    _register_scheduling_provider(app)

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok"}

    return app


def _register_blueprints(app):
    from .blueprints.auth import auth_bp
    from .blueprints.availability import availability_bp
    from .blueprints.appointments import appointments_bp
    from .blueprints.calls import calls_bp
    from .blueprints.dashboard import dashboard_bp
    from .blueprints.patients import patients_bp
    from .blueprints.routing import routing_bp
    from .blueprints.webhooks import webhooks_bp

    app.register_blueprint(routing_bp)
    app.register_blueprint(patients_bp)
    app.register_blueprint(availability_bp)
    app.register_blueprint(appointments_bp)
    app.register_blueprint(calls_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(webhooks_bp)


def _register_scheduling_provider(app):
    """Selects the SchedulingProvider implementation per SCHEDULING_PROVIDER
    (spec §5.5a). Routes read it from current_app.extensions, never by
    importing a concrete provider class directly -- that's the whole point
    of the interface (a future real-EHR provider swaps in here only).

    "postgres" is the only implementation built so far -- see
    app/providers/postgres_scheduling.py.
    """
    if app.config.get("SCHEDULING_PROVIDER") == "postgres":
        from .providers.postgres_scheduling import PostgresSchedulingProvider

        app.extensions["scheduling_provider"] = PostgresSchedulingProvider()
    else:
        app.extensions.setdefault("scheduling_provider", None)
