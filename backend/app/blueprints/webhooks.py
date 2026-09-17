# Account-level Vogent webhook -- separate from the shared-secret function-call
# webhooks in the other blueprints. Vogent posts {event, payload} envelopes here
# for events the in-call flow has no way to produce itself (the full transcript
# only exists once the call has ended).
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from ..auth_utils import require_vogent_signature
from ..extensions import db
from ..models import Call

webhooks_bp = Blueprint("webhooks", __name__, url_prefix="/webhooks")


@webhooks_bp.post("/vogent")
@require_vogent_signature
def vogent_webhook():
    body = request.get_json(silent=True) or {}
    event = body.get("event")
    payload = body.get("payload") or {}
    dial_id = payload.get("dial_id")

    call = Call.query.filter_by(vogent_call_id=dial_id).first() if dial_id else None
    if call is None:
        # Unknown dial_id (e.g. a test dial never routed through our /calls
        # webhook) -- 200 anyway so Vogent doesn't disable the endpoint.
        return jsonify({"status": "ignored"}), 200

    if event == "dial.transcript":
        call.transcript = payload.get("transcript") or []
    elif event == "dial.updated" and call.ended_at is None:
        # The flow's own complete_call already set a terminal status for
        # scheduled/no_match/etc. calls -- only fill in the gap for calls
        # that never reached one (hangup, no pickup, dead air).
        call.status = "abandoned"
        call.ended_at = datetime.now(timezone.utc)

    db.session.commit()
    return jsonify({"status": "ok"}), 200
