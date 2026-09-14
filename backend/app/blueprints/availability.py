# Spec §5.5 (GET /availability). Thin wrapper over the SchedulingProvider
# (§5.5a) -- no direct DB queries here, only urgency-window logic, which the
# spec keeps in the route layer since the provider shouldn't know what
# "urgent" means.
from datetime import date, timedelta

from flask import Blueprint, current_app, jsonify, request

from ..auth_utils import require_agent_key
from ..format_utils import slot_to_dict
from ..providers.scheduling import DEFAULT_SLOT_LIMIT, NORMAL_WINDOW_DAYS

availability_bp = Blueprint("availability", __name__, url_prefix="/api/v1")

URGENT_WINDOW_DAYS = 3


@availability_bp.get("/availability")
@require_agent_key
def get_availability():
    doctor_id = request.args.get("doctor_id", type=int)
    if not doctor_id:
        return jsonify({"error": "doctor_id is required"}), 400

    practice_id = request.args.get("practice_id", type=int)
    appointment_type = request.args.get("appointment_type")
    urgency = request.args.get("urgency")
    limit = request.args.get("limit", default=DEFAULT_SLOT_LIMIT, type=int)

    today = date.today()
    is_urgent = urgency == "URGENT"
    window_days = URGENT_WINDOW_DAYS if is_urgent else NORMAL_WINDOW_DAYS

    date_from_param = request.args.get("from")
    date_to_param = request.args.get("to")
    date_from = date.fromisoformat(date_from_param) if date_from_param else today
    date_to = date.fromisoformat(date_to_param) if date_to_param else today + timedelta(days=window_days)

    provider = current_app.extensions["scheduling_provider"]
    slots = provider.get_available_slots(
        doctor_id=doctor_id,
        practice_id=practice_id,
        appointment_type=appointment_type,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )

    urgent_window_met = None
    if is_urgent and not slots:
        # Widen to the full 14-day window in the same call, never a second
        # round trip from the flow's side.
        widened_to = today + timedelta(days=NORMAL_WINDOW_DAYS)
        slots = provider.get_available_slots(
            doctor_id=doctor_id,
            practice_id=practice_id,
            appointment_type=appointment_type,
            date_from=date_from,
            date_to=widened_to,
            limit=limit,
        )
        # This is the honest-but-unsatisfying outcome: an urgent patient with
        # nothing sooner than the wider window. Real resolution (schedule
        # rearrangement or a desk-number redirect for a human to sort out) is
        # deferred -- see "Urgent scheduling beyond a 3-day window" in
        # notes-final-product.md. Baseline just offers the best late slot
        # with the caller-facing caveat below.
        urgent_window_met = False
    elif is_urgent:
        urgent_window_met = True

    if not slots:
        return jsonify({"status": "no_slots"})

    response = {"status": "slots_available", "slots": [slot_to_dict(s) for s in slots]}
    if urgent_window_met is not None:
        response["urgent_window_met"] = urgent_window_met
    return jsonify(response)
