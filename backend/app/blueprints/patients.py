# Spec §5.3 (POST /patients/lookup) and §5.4 (POST /patients).
from flask import Blueprint, jsonify
from rapidfuzz import fuzz

from ..auth_utils import require_agent_key
from ..vogent_utils import coerce_int, get_agent_json
from ..extensions import db
from ..format_utils import parse_iso_date, serialize_patient
from ..models import Call, Patient

patients_bp = Blueprint("patients", __name__, url_prefix="/api/v1/patients")

# First-name soft-score thresholds (spec §5.3 step b) -- a candidate must
# both score reasonably well AND clearly beat the runner-up to resolve
# without asking the caller to confirm.
NAME_SCORE_FLOOR = 60
NAME_SCORE_GAP = 15

AMBIGUOUS_RESPONSE = (
    "I'm having trouble finding the exact record -- let me have someone from our "
    "office call you back to get you booked."
)


def _confirm_prompt(candidate):
    return (
        f"I have a record for a {candidate.first_name} {candidate.last_name} born on "
        "that date -- does that sound right?"
    )


@patients_bp.post("/lookup")
@require_agent_key
def lookup_patient():
    payload = get_agent_json()
    last_name = (payload.get("last_name") or "").strip()
    dob_raw = payload.get("date_of_birth")
    first_name = (payload.get("first_name") or "").strip()
    call_id = payload.get("call_id")
    # Flow-supplied on a repeat call after a "no" to a confirm_prompt (§5.3
    # step c) -- excludes the rejected candidate(s) so the next layer runs
    # against the remaining pool. Not itself named in the spec's JSON, but
    # required to implement the described "re-calls excluded" behavior.
    excluded_ids = {coerce_int(x) for x in (payload.get("excluded_patient_ids") or [])}

    if not last_name or not dob_raw:
        return jsonify({"error": "last_name and date_of_birth are required"}), 400
    dob = parse_iso_date(dob_raw)
    if dob is None:
        return jsonify({"error": "date_of_birth must be YYYY-MM-DD"}), 400

    all_matches = Patient.query.filter(
        db.func.lower(Patient.last_name) == last_name.lower(), Patient.date_of_birth == dob
    ).all()

    if not all_matches:
        return jsonify({"status": "not_found"})

    candidates = [p for p in all_matches if p.id not in excluded_ids]

    if excluded_ids and not candidates:
        # Every known candidate has already been rejected by the caller.
        return jsonify({"status": "ambiguous_unresolved", "spoken_response": AMBIGUOUS_RESPONSE})

    if len(candidates) == 1:
        return jsonify({"status": "found", "patient": serialize_patient(candidates[0])})

    # (a) Silent caller-ID match against calls.caller_phone.
    call = None
    if call_id:
        call = Call.query.filter_by(vogent_call_id=call_id).first()
    caller_phone = call.caller_phone if call else None
    if caller_phone:
        phone_matches = [p for p in candidates if p.phone and p.phone == caller_phone]
        if len(phone_matches) == 1:
            return jsonify({"status": "found", "patient": serialize_patient(phone_matches[0])})

    # (b) First-name fuzzy soft-score tiebreak.
    top_candidate = candidates[0]
    if first_name:
        scored = sorted(
            ((fuzz.WRatio(first_name, p.first_name), p) for p in candidates),
            key=lambda pair: pair[0],
            reverse=True,
        )
        top_score, top_candidate = scored[0]
        runner_up_score = scored[1][0] if len(scored) > 1 else 0
        if top_score >= NAME_SCORE_FLOOR and (top_score - runner_up_score) >= NAME_SCORE_GAP:
            return jsonify({"status": "found", "patient": serialize_patient(top_candidate)})

    # (c) Still tied -- ask the caller to confirm the best-guess candidate.
    return jsonify(
        {
            "status": "confirm",
            "candidate_patient_id": top_candidate.id,
            "confirm_prompt": _confirm_prompt(top_candidate),
        }
    )


@patients_bp.post("/confirm")
@require_agent_key
def confirm_patient():
    """Resolves a candidate_patient_id from a §5.3 'confirm' response into
    the same {status: found, patient: {...}} shape lookup_patient returns
    directly -- exists so the Vogent flow's confirm-by-readback branch and
    its direct-match branch converge on one consistent response shape for
    downstream nodes, rather than each needing a different field reference
    for "the patient" depending on which path the call took."""
    payload = get_agent_json()
    patient_id = coerce_int(payload.get("patient_id")) or coerce_int(payload.get("candidate_patient_id"))
    patient = db.session.get(Patient, patient_id) if patient_id else None
    if patient is None:
        return jsonify({"status": "not_found"})
    return jsonify({"status": "found", "patient": serialize_patient(patient)})


@patients_bp.post("")
@require_agent_key
def create_patient():
    payload = get_agent_json()
    first_name = (payload.get("first_name") or "").strip()
    last_name = (payload.get("last_name") or "").strip()
    dob_raw = payload.get("date_of_birth")
    phone = (payload.get("phone") or "").strip()
    home_zip = payload.get("home_zip")

    # Minimum patient data per CLAUDE.md: name, DOB, phone.
    if not first_name or not last_name or not dob_raw or not phone:
        return jsonify({"error": "first_name, last_name, date_of_birth, and phone are required"}), 400
    dob = parse_iso_date(dob_raw)
    if dob is None:
        return jsonify({"error": "date_of_birth must be YYYY-MM-DD"}), 400

    # Idempotent on phone digits + DOB. A caller correcting one detail sends
    # the flow back through intake, which called this again and created a
    # second record for the same person -- real calls produced three
    # near-identical rows differing only in how the name was transcribed
    # ("Testcaller" / "Tess Coller" / "test collar"), so name is not a
    # reliable key here but the number they just gave is.
    existing = _find_by_phone_and_dob(phone, dob)
    if existing is not None:
        return jsonify({"status": "created", "patient": serialize_patient(existing)}), 200

    patient = Patient(
        first_name=first_name,
        last_name=last_name,
        date_of_birth=dob,
        phone=phone,
        home_zip=home_zip,
    )
    db.session.add(patient)
    db.session.commit()
    return jsonify({"status": "created", "patient": serialize_patient(patient)}), 201


def _find_by_phone_and_dob(phone, dob):
    digits = _phone_digits(phone)
    if not digits:
        return None
    for candidate in Patient.query.filter(Patient.date_of_birth == dob).all():
        if _phone_digits(candidate.phone) == digits:
            return candidate
    return None


def _phone_digits(value):
    return "".join(ch for ch in (value or "") if ch.isdigit())


@patients_bp.post("/update-name")
@require_agent_key
def update_patient_name():
    """Applies a caller's correction to the name already on the record, so
    a misheard spelling doesn't have to send the whole intake back to the
    start (which used to duplicate the patient)."""
    payload = get_agent_json()
    first_name = (payload.get("first_name") or "").strip()
    last_name = (payload.get("last_name") or "").strip()
    # The flow sends one scalar ("First Last") -- Vogent can't pass two
    # related values without asking two separate questions.
    full_name = (payload.get("full_name") or "").strip()
    if full_name and not (first_name or last_name):
        parts = full_name.split()
        first_name = parts[0]
        last_name = " ".join(parts[1:])
    patient_id = coerce_int(payload.get("patient_id"))
    call_id = payload.get("call_id")

    if not patient_id and call_id:
        call = Call.query.filter_by(vogent_call_id=call_id).first()
        patient_id = call.patient_id if call else None
    if not patient_id or not (first_name or last_name):
        return jsonify({"error": "patient_id (or call_id) and a name are required"}), 400

    patient = db.session.get(Patient, patient_id)
    if patient is None:
        return jsonify({"error": "unknown patient_id"}), 400
    if first_name:
        patient.first_name = first_name
    if last_name:
        patient.last_name = last_name
    db.session.commit()
    return jsonify({"status": "ok", "patient": serialize_patient(patient)})


@patients_bp.post("/update-zip")
@require_agent_key
def update_patient_zip():
    """Persists the ZIP asked later in the call (spec §6 step 2c) onto
    whichever patient this call resolved to -- new or returning, via
    call_id, since the flow asks ZIP after patient lookup/creation."""
    payload = get_agent_json()
    zip_value = (payload.get("zip") or "").strip()
    patient_id = coerce_int(payload.get("patient_id"))
    call_id = payload.get("call_id")

    if not patient_id and call_id:
        call = Call.query.filter_by(vogent_call_id=call_id).first()
        patient_id = call.patient_id if call else None

    if not patient_id or not zip_value:
        return jsonify({"error": "patient_id (or call_id) and zip are required"}), 400

    patient = db.session.get(Patient, patient_id)
    if patient is None:
        return jsonify({"error": "unknown patient_id"}), 400

    patient.home_zip = zip_value
    db.session.commit()
    return jsonify({"status": "ok"})
