# Spec §5.7 -- Vogent-facing call capture: POST /calls, PATCH
# /calls/{vogent_call_id}, POST /calls/{vogent_call_id}/complete.
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify

from ..auth_utils import require_agent_key
from ..vogent_utils import coerce_int, get_agent_json
from ..extensions import db
from ..models import Call

calls_bp = Blueprint("calls", __name__, url_prefix="/api/v1/calls")

ABANDONED_AFTER_MINUTES = 15


def sweep_abandoned_calls():
    """Spec §5.7: a call left `in_progress` with no update for 15 minutes is
    swept to `abandoned` -- e.g. the caller hung up and Vogent's end-of-call
    webhook never fired. No real background scheduler in this baseline;
    called lazily from dashboard.py's read routes instead, which is cheap
    enough at this data volume and means the dashboard is never more than
    one page-load stale."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=ABANDONED_AFTER_MINUTES)
    Call.query.filter(Call.status == "in_progress", Call.started_at < cutoff).update(
        {"status": "abandoned"}, synchronize_session=False
    )
    db.session.commit()


@calls_bp.post("")
@require_agent_key
def create_call():
    body = get_agent_json()
    vogent_call_id = body.get("vogent_call_id")
    if not vogent_call_id:
        return jsonify({"error": "vogent_call_id is required"}), 400

    existing = Call.query.filter_by(vogent_call_id=vogent_call_id).first()
    if existing:
        return jsonify({"call_id": existing.id}), 200

    call = Call(
        vogent_call_id=vogent_call_id,
        caller_phone=body.get("caller_phone"),
        status="in_progress",
    )
    db.session.add(call)
    db.session.commit()
    return jsonify({"call_id": call.id}), 201


@calls_bp.route("/<vogent_call_id>", methods=["PATCH", "POST"])
@require_agent_key
def update_call(vogent_call_id):
    call = Call.query.filter_by(vogent_call_id=vogent_call_id).first()
    if not call:
        return jsonify({"error": "call not found"}), 404

    body = get_agent_json()
    if "matched_term_id" in body:
        call.matched_term_id = coerce_int(body["matched_term_id"])
    if "raw_complaint" in body:
        call.raw_complaint = body["raw_complaint"]
    if "patient_id" in body:
        call.patient_id = coerce_int(body["patient_id"])
    if "appointment_id" in body:
        call.appointment_id = coerce_int(body["appointment_id"])
    if "status" in body:
        call.status = body["status"]

    db.session.commit()
    return jsonify({"call_id": call.id, "status": call.status})


@calls_bp.post("/<vogent_call_id>/complete")
@require_agent_key
def complete_call(vogent_call_id):
    call = Call.query.filter_by(vogent_call_id=vogent_call_id).first()
    if not call:
        return jsonify({"error": "call not found"}), 404

    # Idempotent: a repeated end-of-call webhook shouldn't overwrite an
    # already-completed call or error.
    if call.ended_at is not None:
        return jsonify({"call_id": call.id, "status": call.status}), 200

    body = get_agent_json()
    if "transcript" in body:
        call.transcript = body["transcript"]
    call.status = body.get("status", call.status)
    call.ended_at = datetime.now(timezone.utc)

    db.session.commit()
    return jsonify({"call_id": call.id, "status": call.status}), 200


# Vogent-facing aliases below: Vogent's function-calling always POSTs to one
# static apiPath per function -- it cannot template {vogent_call_id} into a
# URL path. These take the same call_id as a body field instead, delegating
# to the path-param views above so the update/complete logic lives once.


@calls_bp.post("/update")
@require_agent_key
def update_call_by_body_id():
    body = get_agent_json()
    vogent_call_id = body.get("vogent_call_id")
    if not vogent_call_id:
        return jsonify({"error": "vogent_call_id is required"}), 400
    return update_call(vogent_call_id)


@calls_bp.post("/complete")
@require_agent_key
def complete_call_by_body_id():
    body = get_agent_json()
    vogent_call_id = body.get("vogent_call_id")
    if not vogent_call_id:
        return jsonify({"error": "vogent_call_id is required"}), 400
    return complete_call(vogent_call_id)
