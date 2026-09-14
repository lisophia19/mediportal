# Spec §5.6 (POST /appointments). Route logic is Phase 1 -- this is
# scaffolding only.
from flask import Blueprint

appointments_bp = Blueprint("appointments", __name__, url_prefix="/api/v1/appointments")
