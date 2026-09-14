# Spec §5.5 (GET /availability). Route logic is Phase 1 -- this is
# scaffolding only.
from flask import Blueprint

availability_bp = Blueprint("availability", __name__, url_prefix="/api/v1")
