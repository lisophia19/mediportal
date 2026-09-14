# Config is entirely env-driven so local dev and (later) any hosted Postgres
# differ only by DATABASE_URL -- see docs/plans/2026-09-13-scheduling-flow-plan.md
# "Open decisions" for why no AWS-specific config exists here.
import os

from dotenv import load_dotenv

# Searches the current directory and its parents for a .env file (so this
# works whether Flask is launched from the repo root or from backend/) --
# python-dotenv was already a dependency but nothing was actually calling
# this, so a real .env file was silently ignored and every var had to be
# exported by hand.
load_dotenv()


class Config:
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "postgresql://localhost/mediportal"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")

    # Shared-secret header the Vogent flow sends on every call (spec §5 intro).
    AGENT_KEY = os.environ.get("AGENT_KEY", "dev-agent-key")

    # Selects the SchedulingProvider implementation registered in create_app.
    # "postgres" is the only implementation built so far (Phase 1).
    SCHEDULING_PROVIDER = os.environ.get("SCHEDULING_PROVIDER", "postgres")
