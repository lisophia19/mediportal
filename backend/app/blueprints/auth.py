# Spec §5.8 -- POST /auth/login, POST /auth/logout (dashboard JWT). Kept
# separate from dashboard.py because it was already scaffolded that way
# (see the implementation report for why this file was picked up too).
from flask import Blueprint, jsonify, request
from werkzeug.security import check_password_hash

from ..auth_utils import issue_token, require_jwt
from ..models import User

auth_bp = Blueprint("auth", __name__, url_prefix="/api/v1/auth")


@auth_bp.post("/login")
def login():
    body = request.get_json(force=True) or {}
    email = body.get("email", "")
    password = body.get("password", "")

    user = User.query.filter_by(email=email).first()
    if not user or not check_password_hash(user.password_hash, password):
        return jsonify({"error": "invalid email or password"}), 401

    return jsonify({"token": issue_token(user)})


@auth_bp.post("/logout")
@require_jwt
def logout():
    # Stateless JWT -- nothing to invalidate server-side for this baseline.
    # A real deployment would want a token blocklist or short-lived refresh
    # tokens; noted as a deliberate simplification, not an oversight.
    return jsonify({"status": "ok"})
