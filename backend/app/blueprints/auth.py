# Spec §5.8 -- POST /auth/login, POST /auth/logout (dashboard JWT).
# Route logic is Phase 1 -- this is scaffolding only.
from flask import Blueprint

auth_bp = Blueprint("auth", __name__, url_prefix="/api/v1/auth")
