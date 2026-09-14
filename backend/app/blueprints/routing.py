# Spec §5.1 (POST /routing/match-issue), §5.2 (POST /routing/find-doctors).
# Route logic is Phase 1 -- this is scaffolding only.
from flask import Blueprint

routing_bp = Blueprint("routing", __name__, url_prefix="/api/v1/routing")
