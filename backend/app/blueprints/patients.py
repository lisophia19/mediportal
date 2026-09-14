# Spec §5.3 (POST /patients/lookup), §5.4 (POST /patients).
# Route logic is Phase 1 -- this is scaffolding only.
from flask import Blueprint

patients_bp = Blueprint("patients", __name__, url_prefix="/api/v1/patients")
