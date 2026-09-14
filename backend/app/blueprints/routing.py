# Spec §5.1 (POST /routing/match-issue), §5.2 (POST /routing/find-doctors),
# §5.9 (directory redirect, internal helper called from §5.2).
#
# Critical design constraint (spec §1/§3.2): no doctor name, specialty, or
# body-part branch is hardcoded here. Every routing decision comes from a
# query over terms / term_eligibility / doctor_practices / directory tables.
import json
import math
import os
from datetime import date

from anthropic import Anthropic
from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from ..auth_utils import require_agent_key
from ..extensions import db
from ..format_utils import format_doctor_name, parse_iso_date
from ..models import (
    Call,
    DirectoryEntry,
    DirectoryRedirectRule,
    Doctor,
    DoctorPractice,
    Practice,
    Term,
    TermEligibility,
    ZipCentroid,
)

routing_bp = Blueprint("routing", __name__, url_prefix="/api/v1/routing")

# §5.1 confidence thresholds -- below CLARIFY it's a dead end, above MATCH
# it's a confident single match, in between needs a disambiguating question.
MATCH_CONFIDENCE = 0.75
CLARIFY_CONFIDENCE = 0.35
MATCH_GAP = 0.15  # top candidate must clear the runner-up by this much
MAX_CANDIDATE_TERMS = 12

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

EARTH_RADIUS_MILES = 3958.8


def _haversine_miles(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return EARTH_RADIUS_MILES * 2 * math.asin(math.sqrt(a))


def _zip_coords(zip_value):
    if not zip_value:
        return None
    centroid = db.session.get(ZipCentroid, zip_value)
    if centroid is None:
        return None
    return float(centroid.latitude), float(centroid.longitude)


# --- §5.1 match-issue --------------------------------------------------------


def _candidate_terms(complaint_text):
    """Cheap keyword pre-filter over term/body_part/category/patient_phrasing
    so the LLM prompt stays small (spec §5.1 step 1)."""
    words = {w.strip(".,!?").lower() for w in complaint_text.split() if len(w) > 2}
    terms = Term.query.all()
    scored = []
    for term in terms:
        haystack = " ".join(
            filter(None, [term.term, term.body_part, term.category, *(term.patient_phrasing or [])])
        ).lower()
        score = sum(1 for word in words if word in haystack)
        if score:
            scored.append((score, term))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if scored:
        return [term for _, term in scored[:MAX_CANDIDATE_TERMS]]
    # No keyword overlap at all -- still give the LLM something to reason
    # about rather than auto-declaring no_match on a pre-filter miss.
    return terms[:MAX_CANDIDATE_TERMS]


def _anthropic_client():
    return Anthropic()  # reads ANTHROPIC_API_KEY from the environment


def _classify_complaint(complaint_text, candidates):
    """LLM classification call (spec §5.1 step 2). Returns parsed JSON:
    {"ortho_relevant": bool, "matches": [{"term_id", "confidence", "spoken_label"}]}.
    Raises on any transport/parse failure -- callers translate that to a 503."""
    term_lines = "\n".join(
        f'- id={t.id} term="{t.term}" body_part="{t.body_part}" category="{t.category}" '
        f"synonyms={list(t.patient_phrasing or [])}"
        for t in candidates
    )
    prompt = (
        "You are the intake matcher for an orthopedic practice's phone-booking agent. "
        "Match the patient's spoken complaint to ONE OR MORE of the candidate clinical "
        "terms below. Never invent a term outside this list. If nothing here is a "
        "reasonable orthopedic match, say so honestly instead of guessing.\n\n"
        f"Candidate terms:\n{term_lines}\n\n"
        f'Patient complaint: "{complaint_text}"\n\n'
        "Respond with ONLY JSON, no prose, in exactly this shape:\n"
        '{"ortho_relevant": true or false, "matches": '
        '[{"term_id": <int>, "confidence": <0.0-1.0>, "spoken_label": "<short lay '
        'phrase for this term, e.g. \'a possible wrist fracture\'>"}]}\n'
        "List at most 3 matches, best first. Empty matches array if ortho_relevant is false."
    )
    client = _anthropic_client()
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(response.content[0].text)


def _term_payload(term):
    return {
        "id": term.id,
        "term": term.term,
        "body_part": term.body_part,
        "category": term.category,
        "urgency": term.urgency,
        "appointment_type": term.default_appointment_type,
    }


NO_MATCH_RESPONSE = (
    "I'm sorry -- we're an orthopedic practice, so that's not something our doctors "
    "here treat. You'd want to start with your primary care doctor for that."
)


@routing_bp.post("/match-issue")
@require_agent_key
def match_issue():
    payload = request.get_json(silent=True) or {}
    complaint_text = (payload.get("complaint_text") or "").strip()
    call_id = payload.get("call_id")
    if not complaint_text or not call_id:
        return jsonify({"error": "complaint_text and call_id are required"}), 400

    candidates = _candidate_terms(complaint_text)
    if not candidates:
        return jsonify({"status": "no_match", "spoken_response": NO_MATCH_RESPONSE})

    try:
        result = _classify_complaint(complaint_text, candidates)
    except Exception:
        current_app.logger.exception("LLM match-issue call failed")
        return jsonify({"status": "error", "spoken_response": (
            "I'm having trouble understanding right now -- let me have someone from "
            "our office call you right back."
        )}), 503

    matches = sorted(result.get("matches") or [], key=lambda m: m.get("confidence", 0), reverse=True)
    terms_by_id = {t.id: t for t in candidates}

    if not result.get("ortho_relevant") or not matches or matches[0].get("confidence", 0) < CLARIFY_CONFIDENCE:
        return jsonify({"status": "no_match", "spoken_response": NO_MATCH_RESPONSE})

    top = matches[0]
    top_term = terms_by_id.get(top.get("term_id"))
    if top_term is None:
        return jsonify({"status": "no_match", "spoken_response": NO_MATCH_RESPONSE})

    gap_clears = len(matches) == 1 or (top.get("confidence", 0) - matches[1].get("confidence", 0)) >= MATCH_GAP
    if top.get("confidence", 0) >= MATCH_CONFIDENCE and gap_clears:
        confirm_prompt = f"It sounds like this is {top.get('spoken_label', top_term.term)} -- is that right?"
        return jsonify(
            {
                "status": "matched",
                "term": _term_payload(top_term),
                "confidence": top.get("confidence"),
                "confirm_prompt": confirm_prompt,
            }
        )

    # Ambiguous between the top 1-2 candidates -- ask a clarifying question.
    clarify_candidates = matches[:2]
    candidate_payload = []
    for match in clarify_candidates:
        term = terms_by_id.get(match.get("term_id"))
        if term is not None:
            candidate_payload.append(
                {"id": term.id, "term": term.term, "label": match.get("spoken_label", term.term)}
            )
    if len(candidate_payload) < 2:
        return jsonify({"status": "no_match", "spoken_response": NO_MATCH_RESPONSE})

    clarify_prompt = (
        f"Is this more like {candidate_payload[0]['label']}, or {candidate_payload[1]['label']}?"
    )
    return jsonify(
        {
            "status": "needs_clarification",
            "candidates": candidate_payload,
            "clarify_prompt": clarify_prompt,
        }
    )


# --- §5.2 find-doctors + §5.9 directory redirect -----------------------------


def _age_from_dob(dob):
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def _nearest_by_coords(practices, caller_coords):
    """Returns (practice, distance_miles) for whichever of `practices` is
    closest to caller_coords, or (None, None) if caller_coords is unknown or
    none of `practices` has lat/lon set. Shared by both callers below, which
    differ only in what they fall back to when this comes up empty."""
    if caller_coords is None:
        return None, None
    best_practice, best_distance = None, None
    for practice in practices:
        if practice.latitude is None or practice.longitude is None:
            continue
        distance = _haversine_miles(
            caller_coords[0], caller_coords[1], float(practice.latitude), float(practice.longitude)
        )
        if best_distance is None or distance < best_distance:
            best_practice, best_distance = practice, distance
    return best_practice, best_distance


def _nearest_practice_for_doctor(doctor, caller_coords):
    """Returns (practice, distance_miles_or_None) -- the doctor's closest
    practice to the caller, or their primary practice if distance can't be
    computed (missing caller ZIP or missing practice coordinates)."""
    doctor_practices = [dp for dp in doctor.doctor_practices]
    if not doctor_practices:
        return None, None

    best_practice, best_distance = _nearest_by_coords(
        [dp.practice for dp in doctor_practices], caller_coords
    )
    if best_practice is not None:
        return best_practice, best_distance

    primary = next((dp for dp in doctor_practices if dp.is_primary), doctor_practices[0])
    return primary.practice, None


def _spoken_label(doctor, practice, distance):
    parts = [format_doctor_name(doctor)]
    if doctor.specialty:
        parts.append(f"one of our {doctor.specialty.lower()} specialists")
    office = f"at our {practice.name} office"
    if distance is not None:
        office += f" about {distance:.1f} miles from you"
    parts.append(office)
    return ", ".join(parts)


def _fallback_main_phone(caller_coords):
    practices = Practice.query.all()
    if not practices:
        return None
    best_practice, _ = _nearest_by_coords(practices, caller_coords)
    return (best_practice or practices[0]).main_phone


def _directory_redirect_for(term):
    """§5.9 -- hand-curated category/body_part -> directory contact lookup.
    A rule matches when every non-null field on it equals the term's
    corresponding field; the most specific matching rule (most non-null
    fields) wins."""
    best_rule, best_specificity = None, -1
    for rule in DirectoryRedirectRule.query.all():
        if rule.category and rule.category != term.category:
            continue
        if rule.body_part and rule.body_part != term.body_part:
            continue
        specificity = (1 if rule.category else 0) + (1 if rule.body_part else 0)
        if specificity > best_specificity:
            best_rule, best_specificity = rule, specificity
    if best_rule is None:
        return None
    entry = DirectoryEntry.query.filter(func.lower(DirectoryEntry.contact) == best_rule.contact.lower()).first()
    if entry is None:
        return None
    return {"contact": entry.contact, "desk_number": entry.desk_number}


def _no_eligible_doctor_response(reason, term, caller_coords):
    if reason == "age_restricted":
        base = "Our doctors who treat that only see patients in a different age range than you."
    else:
        base = "We don't have a doctor here who treats that."

    redirect = _directory_redirect_for(term)
    if redirect:
        spoken = f"{base} Let me give you the number for our {redirect['contact']} line -- that's {redirect['desk_number']}."
    else:
        phone = _fallback_main_phone(caller_coords)
        if phone:
            spoken = f"{base} Let me give you our main office number so we can get you to the right place -- that's {phone}."
        else:
            spoken = f"{base} Let me have someone from our office call you back to help find the right place for you."

    return jsonify(
        {
            "status": "no_eligible_doctor",
            "reason": reason,
            "directory_redirect": redirect,
            "spoken_response": spoken,
        }
    )


@routing_bp.post("/find-doctors")
@require_agent_key
def find_doctors():
    payload = request.get_json(silent=True) or {}
    term_id = payload.get("term_id")
    dob_raw = payload.get("date_of_birth")
    zip_value = payload.get("zip")
    call_id = payload.get("call_id")
    if not term_id or not dob_raw or not call_id:
        return jsonify({"error": "term_id, date_of_birth, and call_id are required"}), 400

    term = db.session.get(Term, term_id)
    if term is None:
        return jsonify({"error": "unknown term_id"}), 400

    dob = parse_iso_date(dob_raw)
    if dob is None:
        return jsonify({"error": "date_of_birth must be YYYY-MM-DD"}), 400

    caller_coords = _zip_coords(zip_value)
    age = _age_from_dob(dob)

    # Eager-load doctor -> doctor_practices -> practice in this one query --
    # ranking below touches every eligible doctor's full practice list, which
    # would otherwise be a lazy-loaded round trip per doctor and per practice.
    all_eligibility = (
        TermEligibility.query.filter(
            TermEligibility.term_id == term.id, TermEligibility.doctor.has(active=True)
        )
        .options(
            joinedload(TermEligibility.doctor)
            .joinedload(Doctor.doctor_practices)
            .joinedload(DoctorPractice.practice)
        )
        .all()
    )
    if not all_eligibility:
        return _no_eligible_doctor_response("not_covered", term, caller_coords)

    age_eligible = [e for e in all_eligibility if e.min_age <= age <= e.max_age]
    if not age_eligible:
        return _no_eligible_doctor_response("age_restricted", term, caller_coords)

    ranked = []
    for eligibility in age_eligible:
        doctor = eligibility.doctor
        practice, distance = _nearest_practice_for_doctor(doctor, caller_coords)
        if practice is None:
            continue  # doctor has no seeded practice -- skip defensively
        ranked.append((doctor, practice, distance))

    if not ranked:
        return _no_eligible_doctor_response("not_covered", term, caller_coords)

    ranked.sort(key=lambda triple: (triple[2] is None, triple[2] if triple[2] is not None else 0, triple[0].id))

    doctors_payload = [
        {
            "doctor_id": doctor.id,
            "name": format_doctor_name(doctor),
            "specialty": doctor.specialty,
            "practice": {
                "id": practice.id,
                "name": practice.name,
                "address": practice.address,
                "zip": practice.zip,
            },
            "distance_miles": round(distance, 1) if distance is not None else None,
            "spoken_label": _spoken_label(doctor, practice, distance),
        }
        for doctor, practice, distance in ranked
    ]

    return jsonify({"status": "matched", "term_urgency": term.urgency, "doctors": doctors_payload})
