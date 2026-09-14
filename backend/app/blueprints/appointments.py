# Spec §5.6 (POST /appointments). Thin wrapper over
# SchedulingProvider.book_slot -- builds the full confirmation payload here
# (the provider's BookingResult only carries IDs) by joining
# doctors/practices/terms.
from flask import Blueprint, current_app, jsonify, request

from ..auth_utils import require_agent_key
from ..extensions import db
from ..format_utils import format_doctor_name, format_spoken_datetime, slot_to_dict
from ..models import Call, Doctor, Practice, Term
from ..providers.scheduling import SlotUnavailableError

appointments_bp = Blueprint("appointments", __name__, url_prefix="/api/v1/appointments")

APPOINTMENT_TYPE_LABELS = {
    "new_patient_consult": "New patient consultation",
    "follow_up": "Follow-up visit",
    "urgent": "Urgent visit",
}


@appointments_bp.post("")
@require_agent_key
def book_appointment():
    body = request.get_json(force=True) or {}
    slot_id = body.get("slot_id")
    patient_id = body.get("patient_id")
    term_id = body.get("term_id")
    vogent_call_id = body.get("call_id")

    if not slot_id or not patient_id or not term_id:
        return jsonify({"error": "slot_id, patient_id, and term_id are required"}), 400

    internal_call_id = None
    if vogent_call_id:
        call = Call.query.filter_by(vogent_call_id=vogent_call_id).first()
        internal_call_id = call.id if call else None

    provider = current_app.extensions["scheduling_provider"]
    try:
        booking = provider.book_slot(slot_id, patient_id, term_id, internal_call_id)
    except SlotUnavailableError as exc:
        return (
            jsonify(
                {
                    "status": "slot_taken",
                    "alternate_slots": [slot_to_dict(s) for s in exc.alternates],
                }
            ),
            409,
        )

    doctor = db.session.get(Doctor, booking.doctor_id)
    practice = db.session.get(Practice, booking.practice_id)
    term = db.session.get(Term, booking.term_id)
    appointment_type = term.default_appointment_type if term else None

    confirmation = {
        "doctor": format_doctor_name(doctor),
        "practice": practice.name if practice else None,
        "address": f"{practice.address}, {practice.name}" if practice else None,
        "when": format_spoken_datetime(booking.start_time),
        "appointment_type": APPOINTMENT_TYPE_LABELS.get(appointment_type, appointment_type),
    }

    return (
        jsonify(
            {
                "status": "scheduled",
                "appointment_id": booking.appointment_id,
                "confirmation": confirmation,
            }
        ),
        201,
    )
