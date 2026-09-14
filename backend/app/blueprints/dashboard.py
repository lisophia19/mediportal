# Spec §5.8 -- GET /calls (list), GET /calls/{id} (detail), dashboard-facing
# (JWT-gated, distinct audience from the Vogent-facing calls_bp). Login and
# logout live in auth.py, already scaffolded separately for that same
# audience -- see the "auth helper" note in the implementation report.
from flask import Blueprint, jsonify, request

from ..auth_utils import require_jwt
from ..blueprints.calls import sweep_abandoned_calls
from ..extensions import db
from ..format_utils import format_doctor_name, serialize_patient
from ..models import Appointment, AppointmentSlot, Call, Doctor, Patient, Practice, Term

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/api/v1")

PER_PAGE = 20


def _index_by_id(model, ids):
    if not ids:
        return {}
    return {row.id: row for row in model.query.filter(model.id.in_(ids)).all()}


def _appointment_summary(appointment, slot, doctor, practice):
    if not appointment:
        return None
    return {
        "doctor": format_doctor_name(doctor),
        "practice": practice.name if practice else None,
        "start_time": slot.start_time.isoformat() if slot else None,
        "appointment_type": appointment.appointment_type,
    }


def _appointment_summary_for_one(call):
    """Single-call version for get_call_detail, where a batch lookup isn't
    worth building for one row."""
    appointment = db.session.get(Appointment, call.appointment_id) if call.appointment_id else None
    slot = db.session.get(AppointmentSlot, appointment.slot_id) if appointment else None
    doctor = db.session.get(Doctor, slot.doctor_id) if slot else None
    practice = db.session.get(Practice, slot.practice_id) if slot else None
    return _appointment_summary(appointment, slot, doctor, practice)


def _call_summaries(calls):
    """Builds the list-view summary for a page of calls with one batched
    query per related table instead of Patient/Term/Appointment/Slot/
    Doctor/Practice gets per row (was up to ~6 queries x PER_PAGE)."""
    patients_by_id = _index_by_id(Patient, {c.patient_id for c in calls if c.patient_id})
    terms_by_id = _index_by_id(Term, {c.matched_term_id for c in calls if c.matched_term_id})
    appointments_by_id = _index_by_id(Appointment, {c.appointment_id for c in calls if c.appointment_id})
    slots_by_id = _index_by_id(AppointmentSlot, {a.slot_id for a in appointments_by_id.values()})
    doctors_by_id = _index_by_id(Doctor, {s.doctor_id for s in slots_by_id.values()})
    practices_by_id = _index_by_id(Practice, {s.practice_id for s in slots_by_id.values()})

    summaries = []
    for call in calls:
        patient = patients_by_id.get(call.patient_id)
        term = terms_by_id.get(call.matched_term_id)
        appointment = appointments_by_id.get(call.appointment_id)
        slot = slots_by_id.get(appointment.slot_id) if appointment else None
        doctor = doctors_by_id.get(slot.doctor_id) if slot else None
        practice = practices_by_id.get(slot.practice_id) if slot else None
        summaries.append(
            {
                "id": call.id,
                "started_at": call.started_at.isoformat() if call.started_at else None,
                "status": call.status,
                "caller_phone": call.caller_phone,
                "patient_name": f"{patient.first_name} {patient.last_name}" if patient else None,
                "raw_complaint": call.raw_complaint,
                "matched_term": term.term if term else None,
                "appointment": _appointment_summary(appointment, slot, doctor, practice),
            }
        )
    return summaries


@dashboard_bp.get("/calls")
@require_jwt
def list_calls():
    sweep_abandoned_calls()
    status = request.args.get("status")
    q = request.args.get("q")
    page = request.args.get("page", default=1, type=int) or 1

    query = Call.query
    if status:
        query = query.filter(Call.status == status)
    if q:
        like = f"%{q}%"
        query = query.outerjoin(Patient, Call.patient_id == Patient.id).filter(
            db.or_(
                Call.raw_complaint.ilike(like),
                Patient.first_name.ilike(like),
                Patient.last_name.ilike(like),
            )
        )

    query = query.order_by(Call.started_at.desc())
    total = query.count()
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    calls = query.offset((page - 1) * PER_PAGE).limit(PER_PAGE).all()

    return jsonify(
        {
            "calls": _call_summaries(calls),
            "page": page,
            "total_pages": total_pages,
            "total": total,
        }
    )


@dashboard_bp.get("/calls/<int:call_id>")
@require_jwt
def get_call_detail(call_id):
    sweep_abandoned_calls()
    call = db.session.get(Call, call_id)
    if not call:
        return jsonify({"error": "not found"}), 404

    patient = db.session.get(Patient, call.patient_id) if call.patient_id else None
    term = db.session.get(Term, call.matched_term_id) if call.matched_term_id else None
    appointment = _appointment_summary_for_one(call)

    return jsonify(
        {
            "id": call.id,
            "status": call.status,
            "started_at": call.started_at.isoformat() if call.started_at else None,
            "ended_at": call.ended_at.isoformat() if call.ended_at else None,
            "caller_phone": call.caller_phone,
            "patient": serialize_patient(patient),
            "matched_term": {
                "id": term.id,
                "term": term.term,
                "body_part": term.body_part,
                "category": term.category,
            }
            if term
            else None,
            "raw_complaint": call.raw_complaint,
            "appointment": appointment,
            "transcript": call.transcript or [],
        }
    )
