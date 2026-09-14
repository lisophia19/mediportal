# Spec §5.7 -- Vogent-facing call capture: POST /calls, PATCH
# /calls/{vogent_call_id}, POST /calls/{vogent_call_id}/complete.
# Route logic is Phase 1 -- this is scaffolding only.
from flask import Blueprint

calls_bp = Blueprint("calls", __name__, url_prefix="/api/v1/calls")
