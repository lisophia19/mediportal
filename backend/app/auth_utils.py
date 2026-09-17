# Shared auth helpers for the two audiences described in spec §5's intro:
# a shared-secret header (X-Agent-Key) for Vogent-facing routes, a JWT for
# dashboard routes. Both blueprint tracks import from here rather than each
# standing up their own auth-checking logic.
import datetime as dt
import hashlib
import hmac
from functools import wraps

import jwt
from flask import current_app, g, jsonify, request

TOKEN_TTL_HOURS = 12


def require_vogent_signature(view):
    """Guards the account-level Vogent webhook with its HMAC-SHA256
    signature (X-Elto-Signature, hex digest of the raw body)."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        signature = request.headers.get("X-Elto-Signature")
        secret = current_app.config["VOGENT_WEBHOOK_SECRET"].encode()
        expected = hmac.new(secret, request.get_data(), hashlib.sha256).hexdigest()
        if not signature or not hmac.compare_digest(signature, expected):
            return jsonify({"error": "unauthorized"}), 401
        return view(*args, **kwargs)

    return wrapped


def require_agent_key(view):
    """Guards Vogent-facing routes with the shared X-Agent-Key header."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        key = request.headers.get("X-Agent-Key")
        if not key or key != current_app.config["AGENT_KEY"]:
            return jsonify({"error": "unauthorized"}), 401
        return view(*args, **kwargs)

    return wrapped


def issue_token(user):
    """Issues an HS256 JWT for a dashboard user (spec §5.8)."""
    payload = {
        "user_id": user.id,
        "email": user.email,
        "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=TOKEN_TTL_HOURS),
    }
    return jwt.encode(payload, current_app.config["SECRET_KEY"], algorithm="HS256")


def require_jwt(view):
    """Guards dashboard routes with a bearer JWT (spec §5.8)."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "unauthorized"}), 401
        token = auth_header[len("Bearer "):]
        try:
            payload = jwt.decode(
                token, current_app.config["SECRET_KEY"], algorithms=["HS256"]
            )
        except jwt.PyJWTError:
            return jsonify({"error": "unauthorized"}), 401
        g.current_user_id = payload.get("user_id")
        return view(*args, **kwargs)

    return wrapped
