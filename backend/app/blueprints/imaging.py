# Imaging (MRI) location, availability, and booking -- prerequisite
# follow-ups. Deliberately separate from routing.py/availability.py/
# appointments.py rather than overloading those: imaging is booked against
# a practice's MACHINE, not a physician, so there is no doctor anywhere in
# this file's queries.
from datetime import date, timedelta

from flask import Blueprint, jsonify

from ..auth_utils import require_agent_key
from ..blueprints.routing import _nearest_by_coords, _zip_coords, office_phrase
from ..extensions import db
from ..format_utils import format_spoken_datetime
from ..models import Call, ImagingAppointment, ImagingSlot, Practice
from ..providers.scheduling import DEFAULT_SLOT_LIMIT, NORMAL_WINDOW_DAYS
from ..vogent_utils import coerce_int, get_agent_json

imaging_bp = Blueprint("imaging", __name__, url_prefix="/api/v1/imaging")


@imaging_bp.post("/find-location")
@require_agent_key
def find_imaging_location():
    payload = get_agent_json()
    modality = (payload.get("modality") or "MRI").strip().upper()
    zip_value = payload.get("zip")

    mri_practices = Practice.query.filter_by(has_mri=True).all()
    if not mri_practices:
        return jsonify({"status": "no_location", "spoken_response": "I don't currently have an imaging location on file for that -- let me have someone from our office follow up."})

    caller_coords = _zip_coords(zip_value)
    best_practice, distance = _nearest_by_coords(mri_practices, caller_coords)
    if best_practice is None:
        best_practice, distance = mri_practices[0], None

    return jsonify(
        {
            "status": "matched",
            "modality": modality,
            "best_practice_id": best_practice.id,
            "best_practice_spoken_label": office_phrase(best_practice, distance),
        }
    )


@imaging_bp.post("/availability")
@require_agent_key
def imaging_availability():
    payload = get_agent_json()
    practice_id = coerce_int(payload.get("practice_id"))
    modality = (payload.get("modality") or "MRI").strip().upper()
    limit = coerce_int(payload.get("limit")) or DEFAULT_SLOT_LIMIT

    if not practice_id:
        return jsonify({"error": "practice_id is required"}), 400

    today = date.today()
    slots = (
        ImagingSlot.query.filter(
            ImagingSlot.practice_id == practice_id,
            ImagingSlot.modality == modality,
            ImagingSlot.status == "open",
            ImagingSlot.start_time >= today,
            ImagingSlot.start_time < today + timedelta(days=NORMAL_WINDOW_DAYS),
        )
        .order_by(ImagingSlot.start_time)
        .limit(limit)
        .all()
    )
    if not slots:
        return jsonify({"status": "no_slots"})

    practice = db.session.get(Practice, practice_id)
    return jsonify(
        {
            "status": "slots_available",
            "slots": [
                {
                    "slot_id": slot.id,
                    "start_time": slot.start_time.isoformat(),
                    "practice_name": practice.name if practice else None,
                    "spoken_label": format_spoken_datetime(slot.start_time),
                }
                for slot in slots
            ],
        }
    )


@imaging_bp.post("/book")
@require_agent_key
def book_imaging():
    payload = get_agent_json()
    slot_id = coerce_int(payload.get("slot_id"))
    patient_id = coerce_int(payload.get("patient_id"))
    call_id = payload.get("call_id")

    call = Call.query.filter_by(vogent_call_id=call_id).first() if call_id else None
    internal_call_id = call.id if call else None
    if not patient_id and call:
        patient_id = call.patient_id

    if not slot_id or not patient_id:
        return jsonify({"error": "slot_id and patient_id are required"}), 400

    # Locks the row so a concurrent booking attempt for the same slot blocks
    # here rather than racing past the status check, same as doctor-visit
    # booking (postgres_scheduling.py).
    slot = ImagingSlot.query.filter_by(id=slot_id).with_for_update().first()
    if slot is None or slot.status != "open":
        return jsonify({"status": "slot_taken"}), 409

    slot.status = "booked"
    appointment = ImagingAppointment(
        patient_id=patient_id, slot_id=slot.id, call_id=internal_call_id, status="scheduled"
    )
    db.session.add(appointment)
    db.session.commit()

    practice = db.session.get(Practice, slot.practice_id)
    return jsonify(
        {
            "status": "scheduled",
            "imaging_appointment_id": appointment.id,
            "confirmation": {
                "modality": slot.modality,
                "practice": practice.name if practice else None,
                "when": format_spoken_datetime(slot.start_time),
            },
        }
    )
