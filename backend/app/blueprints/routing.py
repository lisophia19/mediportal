# Spec §5.1 match-issue, §5.2 find-doctors, §5.9 directory redirect.
# No doctor/specialty/body-part logic is hardcoded -- routing comes from
# terms / term_eligibility / doctor_practices / directory tables (spec §1/§3.2).
import json
import math
import os
import re
from datetime import date

from anthropic import Anthropic
from flask import Blueprint, current_app, jsonify
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from ..auth_utils import require_agent_key
from ..extensions import db
from ..format_utils import format_doctor_name, parse_iso_date
from ..vogent_utils import coerce_int, get_agent_json
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

# §5.1 confidence threshold: at/above MATCH is confident, below needs a
# disambiguating question (never a no_match decline -- see match_issue).
MATCH_CONFIDENCE = 0.75
MATCH_GAP = 0.15  # top candidate must clear runner-up by this much
MAX_CANDIDATE_TERMS = 12

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

# Screening question asked only when a complaint is ambiguous between hip
# and spine (see match_issue) -- a front-desk heuristic, not a diagnosis.
HIP_SPINE_TRIAGE_QUESTION = (
    "Does the pain travel or shoot down into your leg, or does it mostly stay in one spot?"
)

# Clinical mapping lives in this prompt, not the Vogent flow -- the flow
# only ever speaks what the backend decided.
_TRIAGE_CLASSIFIER_PROMPT = """You are helping a phone-intake system triage an orthopedic \
patient between a hip specialist and a spine specialist, based on their answer to one \
screening question: "{question}"

Use these patterns (this is intake triage, not a diagnosis):
- Pain that travels, shoots, or radiates down into the leg (or arm, for neck pain) strongly \
indicates a SPINE issue (a nerve root being irritated).
- Pain that stays localized to one spot -- groin, buttock, thigh, hip -- with no radiation \
suggests a HIP issue.
- Pain triggered specifically by hip flexion motions (putting on socks/shoes, lifting a knee \
up) strongly indicates HIP.
- Pain that flares with coughing, sneezing, or straining strongly indicates SPINE (increased \
pressure on an irritated nerve root).
- Pain worse after prolonged standing/walking and relieved by sitting suggests SPINE (a \
spinal stenosis pattern); stiffness/pain in the first few steps after sitting or waking that \
loosens up with movement leans HIP (an osteoarthritis "warm-up" pattern), though this one \
overlaps somewhat with spine facet issues, so weight it less than the others.

Caller's answer: "{answer}"

Respond with ONLY one word: HIP, SPINE, or UNCLEAR if the answer genuinely does not point \
either way."""


def _classify_hip_or_spine_answer(question, answer_text):
    client = _anthropic_client()
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=10,
        messages=[
            {
                "role": "user",
                "content": _TRIAGE_CLASSIFIER_PROMPT.format(question=question, answer=answer_text),
            }
        ],
    )
    text_block = next((block for block in response.content if block.type == "text"), None)
    if text_block is None:
        return "UNCLEAR"
    verdict = _strip_markdown_fence(text_block.text).strip().upper()
    return verdict if verdict in ("HIP", "SPINE") else "UNCLEAR"

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


# Filler words excluded from the pre-filter's word-overlap scoring --
# without this, a complaint's own connective words can coincidentally
# exact-match a term's structural text (e.g. "and" in complaint text
# matching "and" inside the body_part "Foot and Ankle"), spuriously
# outranking real candidates that only score on the actual clinical word.
_STOPWORDS = {
    "the", "and", "for", "with", "have", "has", "had", "been", "not",
    "really", "just", "got", "get", "lot", "lately", "sure", "what",
    "when", "where", "that", "this", "these", "those", "kind", "sort",
    "little", "bit", "few", "some", "any", "all", "very", "more", "most",
    "now", "ago", "since", "still", "also", "like", "about", "than",
}


def _candidate_terms(complaint_text):
    """Cheap keyword pre-filter over term/body_part/category/patient_phrasing
    so the LLM prompt stays small (spec §5.1 step 1). Matches whole words,
    not substrings -- a naive `word in haystack` check let common words like
    "for" spuriously match inside unrelated term/category text (e.g. "for"
    is a substring of "Deformity"), crowding out real candidates like
    Pain-Knee for a real complaint mentioning "for a few months"."""
    words = {w.strip(".,!?").lower() for w in complaint_text.split() if len(w) > 2} - _STOPWORDS
    terms = Term.query.all()
    scored = []
    for term in terms:
        haystack_words = {
            w.lower()
            for phrase in filter(None, [term.term, term.body_part, term.category, *(term.patient_phrasing or [])])
            for w in re.split(r"[\s/-]+", phrase)
            if w
        }
        score = len(words & haystack_words)
        if score:
            scored.append((score, term))
    if scored:
        return _diversify_by_body_part(scored)[:MAX_CANDIDATE_TERMS]
    # No keyword overlap -- let the LLM reason instead of auto no_match.
    return terms[:MAX_CANDIDATE_TERMS]


def _diversify_by_body_part(scored):
    """Within each score tier, round-robins across body_part instead of the
    arbitrary id order Term.query.all() returns -- a vague complaint like
    "I have pain" ties every Pain-* term at the same score, and raw id
    order happened to cluster low ids on Back/Neck, pushing Pain-Hip just
    outside the candidate cutoff and silently breaking hip-vs-spine triage
    (it needs both body parts to even reach the LLM)."""
    tiers = {}
    for score, term in scored:
        tiers.setdefault(score, {}).setdefault(term.body_part, []).append(term)
    result = []
    for score in sorted(tiers, reverse=True):
        buckets = tiers[score]
        while any(buckets.values()):
            for body_part in list(buckets):
                if buckets[body_part]:
                    result.append(buckets[body_part].pop(0))
    return result


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
        "terms below. Never invent a term outside this list. Set ortho_relevant=false "
        "ONLY when the complaint clearly describes something non-orthopedic (a headache, "
        "chest pain, a skin rash, etc.) -- a real front-desk person would never turn "
        "away a caller just for being vague about a bone/joint/muscle complaint. If the "
        "complaint mentions pain, an injury, or a joint/limb/back issue but doesn't say "
        "specifically enough to be confident which one, still set ortho_relevant=true "
        "and return your best-guess matches from the list at low confidence (a real "
        "intake person would ask a follow-up question, not decline the patient) -- do "
        "not return an empty matches array unless the complaint is truly unrelated to "
        "orthopedics.\n\n"
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
    # content[0] isn't reliably the text block (a thinking block can precede
    # it) -- find it by type instead.
    text_block = next((block for block in response.content if block.type == "text"), None)
    if text_block is None:
        raise ValueError(f"no text block in Anthropic response: {response.content!r}")
    return json.loads(_strip_markdown_fence(text_block.text))


def _strip_markdown_fence(text):
    """Strips an optional ```json ... ``` fence the model sometimes adds."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    return text.strip()


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
    payload = get_agent_json()
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

    # ortho_relevant is the "should we engage at all" signal; confidence
    # only decides matched vs. needs_clarification/needs_triage below. A
    # real complaint ("a lot of pain, not sure what's causing it") can be
    # genuinely orthopedic but too vague to name a specific term -- that
    # should prompt a clarifying question, not a no_match decline (a real
    # front-desk person asks "where does it hurt?", they don't hang up).
    if not result.get("ortho_relevant") or not matches:
        return jsonify({"status": "no_match", "spoken_response": NO_MATCH_RESPONSE})

    top = matches[0]
    top_term = terms_by_id.get(top.get("term_id"))
    if top_term is None:
        return jsonify({"status": "no_match", "spoken_response": NO_MATCH_RESPONSE})

    gap_clears = len(matches) == 1 or (top.get("confidence", 0) - matches[1].get("confidence", 0)) >= MATCH_GAP
    if top.get("confidence", 0) >= MATCH_CONFIDENCE and gap_clears:
        confirm_prompt = f"It sounds like this is {top.get('spoken_label', top_term.term)} -- is that right?"
        # Flattened (not nested) so Vogent can use them as scalar inputs.
        alternates = {}
        for i, match in enumerate(matches[1:3], start=1):
            term = terms_by_id.get(match.get("term_id"))
            if term is not None:
                alternates[f"alternate_{i}_id"] = term.id
                alternates[f"alternate_{i}_label"] = match.get("spoken_label", term.term)
        return jsonify(
            {
                "status": "matched",
                "term": _term_payload(top_term),
                "confidence": top.get("confidence"),
                "confirm_prompt": confirm_prompt,
                "alternate_1_id": alternates.get("alternate_1_id"),
                "alternate_1_label": alternates.get("alternate_1_label"),
                "alternate_2_id": alternates.get("alternate_2_id"),
                "alternate_2_label": alternates.get("alternate_2_label"),
            }
        )

    # Hip-vs-spine ambiguity: vague "pain" often fits both -- ask a real
    # screening question instead of a weak "is it more like X or Y?".
    hip_match = next(
        (m for m in matches[:3] if getattr(terms_by_id.get(m.get("term_id")), "body_part", None) == "Hip"),
        None,
    )
    spine_match = next(
        (
            m
            for m in matches[:3]
            if getattr(terms_by_id.get(m.get("term_id")), "body_part", None) == "Back/Neck"
        ),
        None,
    )
    if hip_match and spine_match:
        hip_term = terms_by_id[hip_match["term_id"]]
        spine_term = terms_by_id[spine_match["term_id"]]
        return jsonify(
            {
                "status": "needs_triage",
                "triage_question": HIP_SPINE_TRIAGE_QUESTION,
                "hip_term_id": hip_term.id,
                "hip_label": hip_match.get("spoken_label", hip_term.term),
                "spine_term_id": spine_term.id,
                "spine_label": spine_match.get("spoken_label", spine_term.term),
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


@routing_bp.post("/resolve-triage")
@require_agent_key
def resolve_triage():
    """Resolves the hip-vs-spine screening answer (see needs_triage in
    match_issue) to one of the two candidate terms. Mirrors match_issue's
    "matched" response shape."""
    payload = get_agent_json()
    triage_answer = (payload.get("triage_answer") or "").strip()
    hip_term_id = coerce_int(payload.get("hip_term_id"))
    spine_term_id = coerce_int(payload.get("spine_term_id"))
    if not triage_answer or not hip_term_id or not spine_term_id:
        return (
            jsonify({"error": "triage_answer, hip_term_id, and spine_term_id are required"}),
            400,
        )

    hip_term = db.session.get(Term, hip_term_id)
    spine_term = db.session.get(Term, spine_term_id)
    if hip_term is None or spine_term is None:
        return jsonify({"status": "no_match", "spoken_response": NO_MATCH_RESPONSE})

    try:
        verdict = _classify_hip_or_spine_answer(HIP_SPINE_TRIAGE_QUESTION, triage_answer)
    except Exception:
        current_app.logger.exception("LLM resolve-triage call failed")
        return (
            jsonify(
                {
                    "status": "error",
                    "spoken_response": (
                        "I'm having trouble understanding right now -- let me have someone "
                        "from our office call you right back."
                    ),
                }
            ),
            503,
        )

    if verdict == "UNCLEAR":
        return jsonify(
            {
                "status": "still_unclear",
                "spoken_response": (
                    "I want to make sure I get you to the right specialist -- would you "
                    "like to see our hip specialist, or our spine specialist?"
                ),
            }
        )

    chosen, other = (hip_term, spine_term) if verdict == "HIP" else (spine_term, hip_term)
    region_label = "hip" if verdict == "HIP" else "spine"
    return jsonify(
        {
            "status": "matched",
            "term": _term_payload(chosen),
            "confirm_prompt": (
                f"Based on that, it sounds like our {region_label} team would be the "
                "right fit -- does that sound right?"
            ),
            "alternate_1_id": other.id,
            "alternate_1_label": other.term,
            "alternate_2_id": None,
            "alternate_2_label": None,
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
    payload = get_agent_json()
    term_id = coerce_int(payload.get("term_id"))
    dob_raw = payload.get("date_of_birth")
    zip_value = payload.get("zip")
    call_id = payload.get("call_id")

    # Fall back to the term recorded on the call -- multiple upstream flow
    # paths converge here and the call record is what they agree on.
    if not term_id and call_id:
        call = Call.query.filter_by(vogent_call_id=call_id).first()
        term_id = call.matched_term_id if call else None

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

    # Eager-load doctor -> doctor_practices -> practice to avoid N+1 queries.
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

    top = doctors_payload[0]
    return jsonify(
        {
            "status": "matched",
            "term_urgency": term.urgency,
            "doctors": doctors_payload,
            # Flat fields: Vogent can't index into the doctors array as a
            # scalar function input, only interpolate it into prompt text.
            "best_doctor_id": top["doctor_id"],
            "best_practice_id": top["practice"]["id"],
            "best_doctor_spoken_label": top["spoken_label"],
            "best_doctor_name": top["name"],  # short form, for mentions after the first
        }
    )
