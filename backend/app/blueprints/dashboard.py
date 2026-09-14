# Spec §5.8 -- GET /calls (list), GET /calls/{id} (detail), dashboard-facing
# (JWT-gated, distinct audience from the Vogent-facing calls_bp). Route logic
# is Phase 1 -- this is scaffolding only.
from flask import Blueprint

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/api/v1")
