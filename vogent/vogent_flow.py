# Builds the mediportal-agent's real conversation flow (spec §6) and pushes
# it live as a new versioned prompt, replacing whatever's currently set as
# default (the stock "Shiny Smiles" demo template). Run after vogent_setup.py
# has created the function definitions (reads vogent_function_ids.json).
#
# Known simplifications in this first build (see the chat/report for why):
#  - Patient disambiguation retries only once (no excluded_patient_ids loop).
#  - The rare "complaint had zero clinical signal words at all" case (e.g.
#    "I'd like an appointment please") retries once via ask_clarify /
#    match_issue_retry_fn; an ambiguous-but-real complaint instead enters
#    the fluid triage loop (up to 3 rounds, see the "Fluid triage" nodes
#    below), which is the actual answer to the spec's "needs_clarification"
#    case now. If THAT retry itself comes back needs_triage (real but
#    still ambiguous), it doesn't get its own triage loop -- falls through
#    to an honest callback instead (see match_issue_retry_fn's comment).
#    Never a forced guess either way, just not the fullest possible
#    resolution for this one compounding edge case.
#  - Only the single closest eligible doctor is tried (spec's "walk the
#    ranked list on no_slots" is not implemented -- one no_slots ends the call).
#  - A slot-taken race apologizes rather than auto-re-offering alternates.
#  - Dead-end calls do not call complete_call to record a final status (they
#    rely on the 15-minute abandoned sweep) -- keeps the node count down for
#    this first pass.
#  - check_prerequisite_fn checks a returning patient for ANY unsatisfied
#    prerequisite, not specifically one tied to what they're calling about
#    today -- a patient with an old, unrelated pending MRI requirement gets
#    asked about it even if this call is about something else entirely, and
#    a "no" ends the call booking that imaging instead of ever reaching
#    their actual complaint. Real fix is scoping the check against the
#    call's matched_term_id (or a shared body_part/category), not just
#    patient_id -- deferred for this first pass (skeptic-flagged).
import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(REPO_ROOT / ".env")

VOGENT_API = "https://api.vogent.ai/api"
AGENT_ID = os.environ["VOGENT_AGENT_ID"]
API_KEY = os.environ["VOGENT_API_KEY"]
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

FN = json.load(open(Path(__file__).resolve().parent / "vogent_function_ids.json"))


def always(target):
    return {
        "conditionType": "always",
        "arrayConditionType": None,
        "field": None,
        "value": None,
        "values": None,
        "transitionNodeId": target,
    }


def equal(node_id, field, value, target):
    return {
        "conditionType": "equal",
        "arrayConditionType": None,
        "field": f"node.{node_id}.{field}",
        "value": value,
        "values": None,
        "transitionNodeId": target,
    }


def question_node(node_id, name, question, transitions, question_type="freeform",
                   options=None, answer_guidelines=None):
    if question_type == "multiple_choice":
        answer_schema = {"type": "string", "enum": options, "additionalProperties": False}
    else:
        answer_schema = {"type": "string", "additionalProperties": False}
    return {
        "id": node_id,
        "name": name,
        "type": "question",
        "nodeData": {
            "question": question,
            "questionType": question_type,
            "options": options,
            "answerGuidelines": answer_guidelines,
            "clarificationDetails": None,
            "additionalDetails": None,
            "questionSchemaId": None,
        },
        "outputSchema": json.dumps(
            {"properties": {"answer": answer_schema}, "additionalProperties": False}
        ),
        "transitionRules": transitions,
    }


def out(name, type_, description="", nullable=False, custom_schema=None):
    return {
        "type": type_,
        "nullable": nullable,
        "name": name,
        "description": description,
        "customFieldTs": None,
        "customFieldJsonSchema": json.dumps(custom_schema) if custom_schema else None,
    }


def function_node(node_id, name, function_name, inputs, outputs, transitions, started_message=None):
    return {
        "id": node_id,
        "name": name,
        "type": "function",
        "nodeData": {
            "functionId": FN[function_name],
            "functionStartedMessage": started_message,
            "includeTranscript": False,
            "inputs": [{"name": k, "value": v} for k, v in inputs.items()],
            "outputs": outputs,
        },
        "outputSchema": json.dumps(
            {
                "properties": {o["name"]: {"type": o["type"].lower()} for o in outputs},
                "additionalProperties": False,
                "type": "object",
            }
        ),
        "transitionRules": transitions,
    }


def freeform_node(node_id, name, prompt):
    return {
        "id": node_id,
        "name": name,
        "type": "freeform",
        "nodeData": {"prompt": prompt, "outputs": None},
        "outputSchema": None,
        "transitionRules": [],
    }


PATIENT_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "first_name": {"type": "string"},
        "last_name": {"type": "string"},
        "date_of_birth": {"type": "string"},
        "phone": {"type": "string"},
        "home_zip": {"type": "string"},
    },
    "required": ["id"],
    "additionalProperties": False,
}

TERM_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "term": {"type": "string"},
        "body_part": {"type": "string"},
        "category": {"type": "string"},
        "urgency": {"type": "string"},
        "appointment_type": {"type": "string"},
    },
    "required": ["id"],
    "additionalProperties": False,
}

DOCTORS_ARRAY_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "doctor_id": {"type": "integer"},
            "name": {"type": "string"},
            "specialty": {"type": "string"},
            "distance_miles": {"type": ["number", "null"]},
            "spoken_label": {"type": "string"},
        },
    },
}

SLOTS_ARRAY_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "slot_id": {"type": "integer"},
            "start_time": {"type": "string"},
            "duration_minutes": {"type": "integer"},
            "practice_name": {"type": "string"},
            "spoken_label": {"type": "string"},
        },
    },
}

CONFIRMATION_SCHEMA = {
    "type": "object",
    "properties": {
        "doctor": {"type": "string"},
        "practice": {"type": "string"},
        "address": {"type": "string"},
        "when": {"type": "string"},
        "appointment_type": {"type": "string"},
    },
}

PREREQUISITE_SCHEMA = {
    "type": "object",
    "properties": {
        "prerequisite_id": {"type": "integer"},
        "requirement": {"type": "string"},
        "term_id": {"type": "integer"},
        "term_label": {"type": "string"},
    },
}

IMAGING_CONFIRMATION_SCHEMA = {
    "type": "object",
    "properties": {
        "modality": {"type": "string"},
        "practice": {"type": "string"},
        "when": {"type": "string"},
    },
}

GLOBAL_CONTEXT = (
    "You are a scheduling agent for an orthopedic practice (Long Island Bone and "
    "Joint). Speak naturally and warmly, like a real front-desk person. Never say "
    "'no results found', mention IDs, statuses, or any routing/system mechanics out "
    "loud -- always translate backend results into plain, front-desk language. Never "
    "read a raw timestamp or JSON verbatim; phrase dates/times conversationally. You "
    "do not know which doctors treat which issues from your own knowledge -- that is "
    "always resolved by the backend systems you call, never something you guess at."
)

nodes = [
    function_node(
        "init_call", "init-call", "create_call",
        inputs={"caller_phone": "{{toNumber}}"},
        outputs=[out("call_id", "INTEGER", "internal call id")],
        started_message="Thanks for calling!",
        transitions=[always("ask_complaint")],
    ),
    question_node(
        "ask_first_name", "ask-first-name",
        "Great, let's get you booked in. Could I get your first name?",
        transitions=[always("ask_last_name")],
    ),
    question_node(
        "ask_last_name", "ask-last-name",
        "And your last name?",
        transitions=[always("ask_dob")],
    ),
    question_node(
        "ask_dob", "ask-dob",
        "And your date of birth?",
        answer_guidelines="Respond with the date in YYYY-MM-DD format, converting whatever format the caller used.",
        transitions=[always("lookup_patient")],
    ),
    function_node(
        "lookup_patient", "lookup-patient", "lookup_patient",
        inputs={
            "last_name": "{{node.ask_last_name.answer}}",
            "date_of_birth": "{{node.ask_dob.answer}}",
            "first_name": "{{node.ask_first_name.answer}}",
        },
        outputs=[
            out("status", "STRING"),
            out("patient", "CUSTOM", nullable=True, custom_schema=PATIENT_SCHEMA),
            out("candidate_patient_id", "INTEGER", nullable=True),
            out("confirm_prompt", "STRING", nullable=True),
            out("spoken_response", "STRING", nullable=True),
        ],
        started_message="One moment while I look up your information.",
        transitions=[
            equal("lookup_patient", "status", "found", "save_patient_found"),
            equal("lookup_patient", "status", "not_found", "ask_new_phone"),
            equal("lookup_patient", "status", "confirm", "confirm_patient_readback"),
            equal("lookup_patient", "status", "ambiguous_unresolved", "dead_end_ambiguous"),
            always("dead_end_system_error"),
        ],
    ),
    function_node(
        "save_patient_found", "save-patient-found", "update_call",
        inputs={"patient_id": "{{node.lookup_patient.patient.id}}"},
        outputs=[out("status", "STRING")],
        transitions=[always("check_prerequisite_fn")],
    ),
    question_node(
        "confirm_patient_readback", "confirm-patient-readback",
        "{{node.lookup_patient.confirm_prompt}}",
        answer_guidelines="Classify the caller's reply as YES or NO for the answer field only -- never say the word YES or NO out loud yourself.",
        transitions=[
            equal("confirm_patient_readback", "answer", "YES", "confirm_patient_fn"),
            always("dead_end_ambiguous"),
        ],
    ),
    function_node(
        "confirm_patient_fn", "confirm-patient-fn", "confirm_patient",
        inputs={"patient_id": "{{node.lookup_patient.candidate_patient_id}}"},
        outputs=[
            out("status", "STRING"),
            out("patient", "CUSTOM", nullable=True, custom_schema=PATIENT_SCHEMA),
        ],
        transitions=[
            equal("confirm_patient_fn", "status", "found", "save_patient_confirmed"),
            always("dead_end_system_error"),
        ],
    ),
    function_node(
        "save_patient_confirmed", "save-patient-confirmed", "update_call",
        inputs={"patient_id": "{{node.confirm_patient_fn.patient.id}}"},
        outputs=[out("status", "STRING")],
        transitions=[always("check_prerequisite_fn")],
    ),
    # Clinical prerequisite check (e.g. "needs an MRI before their
    # follow-up") -- only ever reachable for a RETURNING patient (a brand
    # new one, via create_patient_fn, skips straight to ask_zip and never
    # touches this). No explicit inputs: both save_patient_found and
    # save_patient_confirmed already wrote patient_id onto the call via
    # update_call, so this resolves it the same way every other
    # late-in-call endpoint does (call_id fallback) -- letting ONE node
    # serve both upstream "patient found" paths instead of needing two.
    function_node(
        "check_prerequisite_fn", "check-prerequisite-fn", "check_prerequisite",
        inputs={},
        outputs=[
            out("status", "STRING"),
            out("pending_prerequisite", "CUSTOM", nullable=True, custom_schema=PREREQUISITE_SCHEMA),
        ],
        transitions=[
            equal("check_prerequisite_fn", "status", "has_prerequisite", "ask_prerequisite_done"),
            always("ask_zip"),
        ],
    ),
    question_node(
        "ask_prerequisite_done", "ask-prerequisite-done",
        (
            "Before we continue, I see a note that you needed to get a "
            "{{node.check_prerequisite_fn.pending_prerequisite.requirement}} done "
            "before your follow-up -- have you had that done?"
        ),
        answer_guidelines=(
            "Classify the caller's reply as YES or NO for the answer field only -- "
            "never say the word YES or NO out loud yourself."
        ),
        transitions=[always("resolve_prerequisite_fn")],
    ),
    function_node(
        "resolve_prerequisite_fn", "resolve-prerequisite-fn", "resolve_prerequisite",
        inputs={
            "prerequisite_id": "{{node.check_prerequisite_fn.pending_prerequisite.prerequisite_id}}",
            "satisfied": "{{node.ask_prerequisite_done.answer}}",
        },
        outputs=[
            out("status", "STRING"),
            out("modality", "STRING", nullable=True),
        ],
        transitions=[
            equal("resolve_prerequisite_fn", "status", "cleared", "ask_zip"),
            equal("resolve_prerequisite_fn", "status", "needs_imaging", "find_imaging_location_fn"),
            always("dead_end_system_error"),
        ],
    ),
    # The prerequisite isn't satisfied -- book the imaging instead of the
    # follow-up. Deliberately ends the call here rather than continuing to
    # the original visit: real clinical sequencing means the follow-up
    # can't happen until the imaging is done, so it gets booked in a
    # separate call after that. Not proximity-ranked against the caller's
    # ZIP (that's only asked later in the normal flow, and this patient's
    # own home_zip is out of reach here for the same two-upstream-sources
    # reason check_prerequisite_fn exists) -- picks any MRI-capable
    # practice. Real ranking is a reasonable follow-up, not required for
    # this to work correctly.
    function_node(
        "find_imaging_location_fn", "find-imaging-location-fn", "find_imaging_location",
        inputs={"modality": "{{node.resolve_prerequisite_fn.modality}}"},
        outputs=[
            out("status", "STRING"),
            out("modality", "STRING", nullable=True),
            out("best_practice_id", "INTEGER", nullable=True),
            out("best_practice_spoken_label", "STRING", nullable=True),
            out("spoken_response", "STRING", nullable=True),
        ],
        transitions=[
            equal("find_imaging_location_fn", "status", "matched", "check_imaging_availability_fn"),
            always("dead_end_no_imaging_location"),
        ],
    ),
    freeform_node(
        "dead_end_no_imaging_location", "dead-end-no-imaging-location",
        "Say exactly: {{node.find_imaging_location_fn.spoken_response}} Then say <|hangup|>.",
    ),
    function_node(
        "check_imaging_availability_fn", "check-imaging-availability-fn", "get_imaging_availability",
        inputs={
            "practice_id": "{{node.find_imaging_location_fn.best_practice_id}}",
            "modality": "{{node.resolve_prerequisite_fn.modality}}",
        },
        outputs=[
            out("status", "STRING"),
            out("slots", "CUSTOM", nullable=True, custom_schema=SLOTS_ARRAY_SCHEMA),
        ],
        started_message="Let me check availability at {{node.find_imaging_location_fn.best_practice_spoken_label}}.",
        transitions=[
            equal("check_imaging_availability_fn", "status", "slots_available", "present_imaging_slots"),
            equal("check_imaging_availability_fn", "status", "no_slots", "dead_end_no_imaging_slots"),
            always("dead_end_system_error"),
        ],
    ),
    freeform_node(
        "dead_end_no_imaging_slots", "dead-end-no-imaging-slots",
        (
            "Say exactly: I'm sorry, I don't have any imaging openings right now -- "
            "let me have someone from our office call you back to get that scheduled. "
            "Then say <|hangup|>."
        ),
    ),
    question_node(
        "present_imaging_slots", "present-imaging-slots",
        (
            "Let the caller know you found some openings for their "
            "{{node.resolve_prerequisite_fn.modality}} at "
            "{{node.find_imaging_location_fn.best_practice_spoken_label}}. Read out up "
            "to 3 of the soonest options from this list, phrased conversationally -- "
            "never read a raw timestamp verbatim: "
            "{{node.check_imaging_availability_fn.slots}}"
        ),
        answer_guidelines=(
            "If the caller CLEARLY picks one of the listed times, respond with the "
            "exact slot_id integer of that slot. If they have no preference, respond "
            "with the slot_id of the soonest slot. If they don't want any of them, "
            "respond with exactly NONE."
        ),
        transitions=[
            equal("present_imaging_slots", "answer", "NONE", "dead_end_no_imaging_slots"),
            always("book_imaging_fn"),
        ],
    ),
    function_node(
        "book_imaging_fn", "book-imaging-fn", "book_imaging",
        inputs={"slot_id": "{{node.present_imaging_slots.answer}}"},
        outputs=[
            out("status", "STRING"),
            out("imaging_appointment_id", "INTEGER", nullable=True),
            out("confirmation", "CUSTOM", nullable=True, custom_schema=IMAGING_CONFIRMATION_SCHEMA),
        ],
        started_message="Great, let me get that booked for you.",
        transitions=[
            equal("book_imaging_fn", "status", "scheduled", "log_scheduled_imaging_fn"),
            equal("book_imaging_fn", "status", "slot_taken", "dead_end_no_imaging_slots"),
            always("dead_end_system_error"),
        ],
    ),
    function_node(
        "log_scheduled_imaging_fn", "log-scheduled-imaging-fn", "complete_call",
        inputs={
            "status": "scheduled",
            "imaging_appointment_id": "{{node.book_imaging_fn.imaging_appointment_id}}",
        },
        outputs=[out("status", "STRING")],
        transitions=[always("confirm_imaging_booking")],
    ),
    freeform_node(
        "confirm_imaging_booking", "confirm-imaging-booking",
        (
            "Confirm the booking to the caller: their "
            "{{node.book_imaging_fn.confirmation.modality}} at "
            "{{node.book_imaging_fn.confirmation.practice}}, "
            "{{node.book_imaging_fn.confirmation.when}}. Read it back naturally and "
            "clearly. Let them know that once that's done, they should call back to "
            "get their follow-up visit scheduled. Then say <|hangup|>."
        ),
    ),
    question_node(
        "ask_new_phone", "ask-new-phone",
        "I don't see an existing record for you -- let's get you set up. What's the best phone number for you?",
        transitions=[always("create_patient_fn")],
    ),
    function_node(
        "create_patient_fn", "create-patient-fn", "create_patient",
        inputs={
            "first_name": "{{node.ask_first_name.answer}}",
            "last_name": "{{node.ask_last_name.answer}}",
            "date_of_birth": "{{node.ask_dob.answer}}",
            "phone": "{{node.ask_new_phone.answer}}",
        },
        outputs=[
            out("status", "STRING"),
            out("patient", "CUSTOM", nullable=True, custom_schema=PATIENT_SCHEMA),
        ],
        started_message="Great, one moment while I create your record.",
        transitions=[always("save_patient_created")],
    ),
    function_node(
        "save_patient_created", "save-patient-created", "update_call",
        inputs={"patient_id": "{{node.create_patient_fn.patient.id}}"},
        outputs=[out("status", "STRING")],
        transitions=[always("ask_zip")],
    ),
    question_node(
        "ask_zip", "ask-zip",
        "What's your ZIP code, or the town you're in?",
        answer_guidelines="If the caller gives a 5-digit ZIP code, respond with just those 5 digits. Otherwise respond with the town or city name they gave.",
        transitions=[always("save_zip")],
    ),
    function_node(
        "save_zip", "save-zip", "update_patient_zip",
        inputs={"zip": "{{node.ask_zip.answer}}"},
        outputs=[out("status", "STRING")],
        transitions=[always("confirm_details")],
    ),
    question_node(
        "confirm_details", "confirm-details",
        (
            "Before continuing, read back what you have collected so far to make sure "
            "it is all correct: name {{node.ask_first_name.answer}} "
            "{{node.ask_last_name.answer}}, date of birth {{node.ask_dob.answer}}, "
            "location {{node.ask_zip.answer}}, and the reason for the visit "
            "{{node.ask_complaint.answer}}. Ask the caller to confirm all of that is "
            "right."
        ),
        answer_guidelines=(
            "If the caller confirms everything is correct, respond with exactly "
            "YES. If they correct how their NAME was heard or spelled, respond "
            "with exactly NAME. For any other correction respond with exactly NO."
        ),
        transitions=[
            equal("confirm_details", "answer", "YES", "ask_doctor_preference"),
            equal("confirm_details", "answer", "NAME", "correct_name"),
            always("correct_name"),
        ],
    ),
    # A correction must NOT send the caller back through intake: doing that
    # re-ran patient creation and produced duplicate records, replayed every
    # "one moment while I look up your information" line, and stretched a
    # two-minute call past three. Fix the name in place and carry on.
    question_node(
        "correct_name", "correct-name",
        (
            "Apologize briefly for getting that wrong and ask the caller to say "
            "their first and last name once more, slowly."
        ),
        answer_guidelines=(
            "Respond with just the corrected full name as 'First Last', spelled the "
            "way the caller gave it, and nothing else."
        ),
        transitions=[always("save_corrected_name")],
    ),
    function_node(
        "save_corrected_name", "save-corrected-name", "update_patient_name",
        inputs={"full_name": "{{node.correct_name.answer}}"},
        outputs=[out("status", "STRING")],
        transitions=[always("ask_doctor_preference")],
    ),
    # spec §5.1a: give the caller a chance to name a specific doctor and/or
    # office before auto-picking by distance. Most callers have no
    # preference (NONE), which reaches find_doctors_fn completely unchanged
    # from before this feature existed.
    question_node(
        "ask_doctor_preference", "ask-doctor-preference",
        (
            "Before I check availability, did you have a specific doctor or office "
            "in mind, or a preference for a male or female doctor, or would you like "
            "me to find the right specialist for you?"
        ),
        answer_guidelines=(
            "If the caller has no preference of any kind -- no doctor, no office, no "
            "gender preference -- respond with exactly NONE. Otherwise respond with "
            "what they said, verbatim, even if it's only a gender preference and no "
            "doctor or office."
        ),
        transitions=[
            equal("ask_doctor_preference", "answer", "NONE", "find_doctors_fn"),
            always("find_requested_doctor_fn"),
        ],
    ),
    function_node(
        "find_requested_doctor_fn", "find-requested-doctor-fn", "find_doctor_by_name",
        inputs={
            "doctor_office_text": "{{node.ask_doctor_preference.answer}}",
            "date_of_birth": "{{node.ask_dob.answer}}",
            "zip": "{{node.ask_zip.answer}}",
        },
        outputs=[
            out("status", "STRING"),
            out("term_urgency", "STRING", nullable=True),
            out("best_doctor_id", "INTEGER", nullable=True),
            out("best_practice_id", "INTEGER", nullable=True),
            out("best_doctor_spoken_label", "STRING", nullable=True),
            out("best_doctor_name", "STRING", nullable=True),
            out("requested_practice_id", "INTEGER", nullable=True),
            out("option_a_doctor_id", "INTEGER", nullable=True),
            out("option_a_doctor_name", "STRING", nullable=True),
            out("option_b_doctor_id", "INTEGER", nullable=True),
            out("option_b_doctor_name", "STRING", nullable=True),
            out("spoken_response", "STRING", nullable=True),
        ],
        transitions=[
            equal("find_requested_doctor_fn", "status", "matched", "check_requested_availability_fn"),
            equal("find_requested_doctor_fn", "status", "needs_choice", "choose_requested_doctor_option"),
            equal("find_requested_doctor_fn", "status", "no_eligible_doctor", "dead_end_no_requested_doctor"),
            always("dead_end_system_error"),
        ],
    ),
    function_node(
        "check_requested_availability_fn", "check-requested-availability-fn", "get_availability",
        inputs={
            "doctor_id": "{{node.find_requested_doctor_fn.best_doctor_id}}",
            "practice_id": "{{node.find_requested_doctor_fn.best_practice_id}}",
            "urgency": "{{node.find_requested_doctor_fn.term_urgency}}",
        },
        outputs=[
            out("status", "STRING"),
            out("slots", "CUSTOM", nullable=True, custom_schema=SLOTS_ARRAY_SCHEMA),
            out("urgent_window_met", "BOOLEAN", nullable=True),
            out("different_practice_name", "STRING", nullable=True),
        ],
        started_message="Great, let me check {{node.find_requested_doctor_fn.best_doctor_spoken_label}}'s availability.",
        transitions=[
            equal("check_requested_availability_fn", "status", "slots_available", "present_requested_slots"),
            equal("check_requested_availability_fn", "status", "no_slots", "dead_end_no_slots"),
            always("dead_end_system_error"),
        ],
    ),
    question_node(
        "present_requested_slots", "present-requested-slots",
        (
            "Let the caller know you found some openings with "
            "{{node.find_requested_doctor_fn.best_doctor_name}}. This visit is NOT "
            "urgent unless urgent_window_met is present and literally the boolean "
            "false -- if blank, missing, or not present, never mention urgency. Only "
            "when {{node.check_requested_availability_fn.urgent_window_met}} is "
            "explicitly false, first say honestly you could not find anything within "
            "the next few days for this urgent issue, before listing the soonest "
            "opening. Read out up to 3 of the soonest options, phrased "
            "conversationally, never a raw timestamp: "
            "{{node.check_requested_availability_fn.slots}}. If "
            "{{node.check_requested_availability_fn.different_practice_name}} is not "
            "blank, these openings are at that office rather than the one just "
            "discussed -- say so plainly before listing the times."
        ),
        answer_guidelines=(
            "If the caller CLEARLY picks one of the listed times, respond with the "
            "exact slot_id integer. If they have no preference, respond with the "
            "slot_id of the soonest slot. Otherwise, respond with exactly NONE."
        ),
        transitions=[
            equal("present_requested_slots", "answer", "NONE", "dead_end_no_slots"),
            always("book_requested_appointment_fn"),
        ],
    ),
    function_node(
        "book_requested_appointment_fn", "book-requested-appointment-fn", "book_appointment",
        inputs={"slot_id": "{{node.present_requested_slots.answer}}"},
        outputs=[
            out("status", "STRING"),
            out("appointment_id", "INTEGER", nullable=True),
            out("confirmation", "CUSTOM", nullable=True, custom_schema=CONFIRMATION_SCHEMA),
        ],
        started_message="Great, let me get that booked for you.",
        transitions=[
            equal("book_requested_appointment_fn", "status", "scheduled", "log_scheduled_requested_fn"),
            equal("book_requested_appointment_fn", "status", "slot_taken", "dead_end_no_slots"),
            always("dead_end_system_error"),
        ],
    ),
    function_node(
        "log_scheduled_requested_fn", "log-scheduled-requested-fn", "complete_call",
        inputs={
            "status": "scheduled",
            "appointment_id": "{{node.book_requested_appointment_fn.appointment_id}}",
        },
        outputs=[out("status", "STRING")],
        transitions=[always("confirm_booking_requested")],
    ),
    freeform_node(
        "confirm_booking_requested", "confirm-booking-requested",
        (
            "Confirm the booking to the caller: "
            "{{node.book_requested_appointment_fn.confirmation.doctor}} at "
            "{{node.book_requested_appointment_fn.confirmation.practice}}, "
            "{{node.book_requested_appointment_fn.confirmation.when}}, a "
            "{{node.book_requested_appointment_fn.confirmation.appointment_type}} "
            "appointment. Read it back naturally and clearly. Then ask if there is "
            "anything else you can help with. Wait for their response. Once they "
            "say there is nothing else, thank them and say <|hangup|>."
        ),
    ),
    # The caller named a real doctor AND a real office, but that doctor
    # isn't at that office ("call 3"). find_requested_doctor_fn already
    # worked out both a real fix (this doctor's own office) and, if one
    # exists, a different eligible doctor who really is at the requested
    # office -- the caller picks between them.
    question_node(
        "choose_requested_doctor_option", "choose-requested-doctor-option",
        "{{node.find_requested_doctor_fn.spoken_response}}",
        answer_guidelines=(
            "If the caller wants to stick with "
            "{{node.find_requested_doctor_fn.option_a_doctor_name}} at their own "
            "office, respond with the exact integer "
            "{{node.find_requested_doctor_fn.option_a_doctor_id}}. If they'd rather "
            "see {{node.find_requested_doctor_fn.option_b_doctor_name}} at the "
            "office they originally asked for, respond with the exact integer "
            "{{node.find_requested_doctor_fn.option_b_doctor_id}}. If neither works "
            "for them, respond with exactly NONE."
        ),
        transitions=[
            equal("choose_requested_doctor_option", "answer", "NONE", "dead_end_no_doctor_choice"),
            always("resolve_chosen_doctor_fn"),
        ],
    ),
    freeform_node(
        "dead_end_no_doctor_choice", "dead-end-no-doctor-choice",
        (
            "Say exactly: I understand -- let me have someone from our office call "
            "you back to help find the right fit. Then say <|hangup|>."
        ),
    ),
    function_node(
        # preferred_practice_id is the ORIGINALLY requested office in both
        # cases (option A or B) -- find_doctors only pins to it when the
        # chosen doctor actually practices there, and falls back to that
        # doctor's own nearest real office otherwise, so this one node
        # resolves correctly no matter which option the caller picked.
        "resolve_chosen_doctor_fn", "resolve-chosen-doctor-fn", "find_doctors",
        inputs={
            "date_of_birth": "{{node.ask_dob.answer}}",
            "zip": "{{node.ask_zip.answer}}",
            "preferred_doctor_id": "{{node.choose_requested_doctor_option.answer}}",
            "preferred_practice_id": "{{node.find_requested_doctor_fn.requested_practice_id}}",
        },
        outputs=[
            out("status", "STRING"),
            out("term_urgency", "STRING", nullable=True),
            out("best_doctor_id", "INTEGER", nullable=True),
            out("best_practice_id", "INTEGER", nullable=True),
            out("best_doctor_spoken_label", "STRING", nullable=True),
            out("best_doctor_name", "STRING", nullable=True),
            out("spoken_response", "STRING", nullable=True),
        ],
        transitions=[
            equal("resolve_chosen_doctor_fn", "status", "matched", "check_chosen_availability_fn"),
            equal("resolve_chosen_doctor_fn", "status", "no_eligible_doctor", "dead_end_no_chosen_doctor"),
            always("dead_end_system_error"),
        ],
    ),
    function_node(
        "check_chosen_availability_fn", "check-chosen-availability-fn", "get_availability",
        inputs={
            "doctor_id": "{{node.resolve_chosen_doctor_fn.best_doctor_id}}",
            "practice_id": "{{node.resolve_chosen_doctor_fn.best_practice_id}}",
            "urgency": "{{node.resolve_chosen_doctor_fn.term_urgency}}",
        },
        outputs=[
            out("status", "STRING"),
            out("slots", "CUSTOM", nullable=True, custom_schema=SLOTS_ARRAY_SCHEMA),
            out("urgent_window_met", "BOOLEAN", nullable=True),
            out("different_practice_name", "STRING", nullable=True),
        ],
        started_message="Let me check {{node.resolve_chosen_doctor_fn.best_doctor_spoken_label}}'s availability.",
        transitions=[
            equal("check_chosen_availability_fn", "status", "slots_available", "present_chosen_slots"),
            equal("check_chosen_availability_fn", "status", "no_slots", "dead_end_no_slots"),
            always("dead_end_system_error"),
        ],
    ),
    question_node(
        "present_chosen_slots", "present-chosen-slots",
        (
            "Let the caller know you found some openings with "
            "{{node.resolve_chosen_doctor_fn.best_doctor_name}}. This visit is NOT "
            "urgent unless urgent_window_met is present and literally the boolean "
            "false -- if blank, missing, or not present, never mention urgency. Only "
            "when {{node.check_chosen_availability_fn.urgent_window_met}} is "
            "explicitly false, first say honestly you could not find anything within "
            "the next few days for this urgent issue, before listing the soonest "
            "opening. Read out up to 3 of the soonest options, phrased "
            "conversationally, never a raw timestamp: "
            "{{node.check_chosen_availability_fn.slots}}. If "
            "{{node.check_chosen_availability_fn.different_practice_name}} is not "
            "blank, these openings are at that office rather than the one just "
            "discussed -- say so plainly before listing the times."
        ),
        answer_guidelines=(
            "If the caller CLEARLY picks one of the listed times, respond with the "
            "exact slot_id integer. If they have no preference, respond with the "
            "slot_id of the soonest slot. Otherwise, respond with exactly NONE."
        ),
        transitions=[
            equal("present_chosen_slots", "answer", "NONE", "dead_end_no_slots"),
            always("book_chosen_appointment_fn"),
        ],
    ),
    function_node(
        "book_chosen_appointment_fn", "book-chosen-appointment-fn", "book_appointment",
        inputs={"slot_id": "{{node.present_chosen_slots.answer}}"},
        outputs=[
            out("status", "STRING"),
            out("appointment_id", "INTEGER", nullable=True),
            out("confirmation", "CUSTOM", nullable=True, custom_schema=CONFIRMATION_SCHEMA),
        ],
        started_message="Great, let me get that booked for you.",
        transitions=[
            equal("book_chosen_appointment_fn", "status", "scheduled", "log_scheduled_chosen_fn"),
            equal("book_chosen_appointment_fn", "status", "slot_taken", "dead_end_no_slots"),
            always("dead_end_system_error"),
        ],
    ),
    function_node(
        "log_scheduled_chosen_fn", "log-scheduled-chosen-fn", "complete_call",
        inputs={
            "status": "scheduled",
            "appointment_id": "{{node.book_chosen_appointment_fn.appointment_id}}",
        },
        outputs=[out("status", "STRING")],
        transitions=[always("confirm_booking_chosen")],
    ),
    freeform_node(
        "confirm_booking_chosen", "confirm-booking-chosen",
        (
            "Confirm the booking to the caller: "
            "{{node.book_chosen_appointment_fn.confirmation.doctor}} at "
            "{{node.book_chosen_appointment_fn.confirmation.practice}}, "
            "{{node.book_chosen_appointment_fn.confirmation.when}}, a "
            "{{node.book_chosen_appointment_fn.confirmation.appointment_type}} "
            "appointment. Read it back naturally and clearly. Then ask if there is "
            "anything else you can help with. Wait for their response. Once they "
            "say there is nothing else, thank them and say <|hangup|>."
        ),
    ),
    question_node(
        "ask_complaint", "ask-complaint",
        "Welcome to Long Island Bone and Joint -- what can we help you with today?",
        answer_guidelines="Capture what the caller says about their issue as close to verbatim as possible -- do not paraphrase or summarize.",
        transitions=[always("match_issue_fn")],
    ),
    function_node(
        "match_issue_fn", "match-issue-fn", "match_issue",
        inputs={"complaint_text": "{{node.ask_complaint.answer}}"},
        outputs=[
            out("status", "STRING"),
            out("term", "CUSTOM", nullable=True, custom_schema=TERM_SCHEMA),
            out("confirm_prompt", "STRING", nullable=True),
            out("clarify_prompt", "STRING", nullable=True),
            out("spoken_response", "STRING", nullable=True),
            out("alternate_1_id", "INTEGER", nullable=True),
            out("alternate_1_label", "STRING", nullable=True),
            out("alternate_2_id", "INTEGER", nullable=True),
            out("alternate_2_label", "STRING", nullable=True),
            out("triage_question", "STRING", nullable=True),
            out("term_a_id", "INTEGER", nullable=True),
            out("term_a_label", "STRING", nullable=True),
            out("term_b_id", "INTEGER", nullable=True),
            out("term_b_label", "STRING", nullable=True),
        ],
        transitions=[
            equal("match_issue_fn", "status", "matched", "confirm_complaint"),
            equal("match_issue_fn", "status", "needs_triage", "ask_triage_question_1"),
            equal("match_issue_fn", "status", "needs_clarification", "ask_clarify"),
            equal("match_issue_fn", "status", "no_match", "log_no_match_fn"),
            always("dead_end_system_error"),
        ],
    ),
    question_node(
        "confirm_complaint", "confirm-complaint",
        "{{node.match_issue_fn.confirm_prompt}}",
        answer_guidelines="Classify the caller's reply as YES or NO for the answer field only -- never say the word YES or NO out loud yourself.",
        transitions=[
            equal("confirm_complaint", "answer", "YES", "save_term_matched"),
            always("offer_alternates"),
        ],
    ),
    question_node(
        "offer_alternates", "offer-alternates",
        (
            "The caller said that wasn't right. Apologize briefly, then ask if it could "
            "instead be {{node.match_issue_fn.alternate_1_label}}, or "
            "{{node.match_issue_fn.alternate_2_label}}. If neither of those were "
            "mentioned (they came back empty), skip straight to: 'Okay, can you tell me "
            "a bit more about what's going on?'"
        ),
        answer_guidelines=(
            "If the caller agrees it is the FIRST option you offered, respond with "
            "exactly ALT1. If they agree it is the SECOND option, respond with exactly "
            "ALT2. If they describe their issue differently, say neither fits, or no "
            "options were offered, respond with exactly OTHER."
        ),
        transitions=[
            equal("offer_alternates", "answer", "ALT1", "save_term_alternate_1"),
            equal("offer_alternates", "answer", "ALT2", "save_term_alternate_2"),
            always("ask_complaint"),
        ],
    ),
    function_node(
        "save_term_alternate_1", "save-term-alternate-1", "update_call",
        inputs={
            "matched_term_id": "{{node.match_issue_fn.alternate_1_id}}",
            "raw_complaint": "{{node.ask_complaint.answer}}",
        },
        outputs=[out("status", "STRING")],
        transitions=[always("ask_first_name")],
    ),
    function_node(
        "save_term_alternate_2", "save-term-alternate-2", "update_call",
        inputs={
            "matched_term_id": "{{node.match_issue_fn.alternate_2_id}}",
            "raw_complaint": "{{node.ask_complaint.answer}}",
        },
        outputs=[out("status", "STRING")],
        transitions=[always("ask_first_name")],
    ),
    # --- Fluid triage (spec §5.1 needs_triage) -------------------------------
    # No hardcoded pair or fixed question -- match_issue_fn generates a real
    # discriminating question live for whichever two candidates are
    # ambiguous, and resolve_triage classifies the answer against those same
    # two. Up to 3 rounds (Vogent nodes are static per-node templates, so a
    # real loop means 3 literal round-pairs, not a runtime loop -- the
    # established pattern in this file); round 3's still_unclear goes to an
    # honest callback dead end instead of ever forcing a guess.
    question_node(
        "ask_triage_question_1", "ask-triage-question-1",
        "{{node.match_issue_fn.triage_question}}",
        transitions=[always("resolve_triage_fn")],
    ),
    function_node(
        "resolve_triage_fn", "resolve-triage-fn", "resolve_triage",
        inputs={
            "triage_answer": "{{node.ask_triage_question_1.answer}}",
            "triage_question": "{{node.match_issue_fn.triage_question}}",
            "term_a_id": "{{node.match_issue_fn.term_a_id}}",
            "term_b_id": "{{node.match_issue_fn.term_b_id}}",
        },
        outputs=[
            out("status", "STRING"),
            out("term", "CUSTOM", nullable=True, custom_schema=TERM_SCHEMA),
            out("confirm_prompt", "STRING", nullable=True),
            out("alternate_1_id", "INTEGER", nullable=True),
            out("alternate_1_label", "STRING", nullable=True),
            out("triage_question", "STRING", nullable=True),
        ],
        transitions=[
            equal("resolve_triage_fn", "status", "matched", "confirm_triage_1"),
            equal("resolve_triage_fn", "status", "still_unclear", "ask_triage_question_2"),
            always("dead_end_system_error"),
        ],
    ),
    question_node(
        "confirm_triage_1", "confirm-triage-1",
        "{{node.resolve_triage_fn.confirm_prompt}}",
        answer_guidelines="Classify the caller's reply as YES or NO for the answer field only -- never say the word YES or NO out loud yourself.",
        transitions=[
            equal("confirm_triage_1", "answer", "YES", "save_term_triaged_1"),
            always("ask_complaint"),
        ],
    ),
    function_node(
        "save_term_triaged_1", "save-term-triaged-1", "update_call",
        inputs={
            "matched_term_id": "{{node.resolve_triage_fn.term.id}}",
            "raw_complaint": "{{node.ask_complaint.answer}}",
        },
        outputs=[out("status", "STRING")],
        transitions=[always("ask_first_name")],
    ),
    question_node(
        "ask_triage_question_2", "ask-triage-question-2",
        "{{node.resolve_triage_fn.triage_question}}",
        transitions=[always("resolve_triage_retry_fn")],
    ),
    function_node(
        "resolve_triage_retry_fn", "resolve-triage-retry-fn", "resolve_triage",
        inputs={
            "triage_answer": "{{node.ask_triage_question_2.answer}}",
            "triage_question": "{{node.resolve_triage_fn.triage_question}}",
            "term_a_id": "{{node.match_issue_fn.term_a_id}}",
            "term_b_id": "{{node.match_issue_fn.term_b_id}}",
        },
        outputs=[
            out("status", "STRING"),
            out("term", "CUSTOM", nullable=True, custom_schema=TERM_SCHEMA),
            out("confirm_prompt", "STRING", nullable=True),
            out("alternate_1_id", "INTEGER", nullable=True),
            out("alternate_1_label", "STRING", nullable=True),
            out("triage_question", "STRING", nullable=True),
        ],
        transitions=[
            equal("resolve_triage_retry_fn", "status", "matched", "confirm_triage_2"),
            equal("resolve_triage_retry_fn", "status", "still_unclear", "ask_triage_question_3"),
            always("dead_end_system_error"),
        ],
    ),
    question_node(
        "confirm_triage_2", "confirm-triage-2",
        "{{node.resolve_triage_retry_fn.confirm_prompt}}",
        answer_guidelines="Classify the caller's reply as YES or NO for the answer field only -- never say the word YES or NO out loud yourself.",
        transitions=[
            equal("confirm_triage_2", "answer", "YES", "save_term_triaged_2"),
            always("ask_complaint"),
        ],
    ),
    function_node(
        "save_term_triaged_2", "save-term-triaged-2", "update_call",
        inputs={
            "matched_term_id": "{{node.resolve_triage_retry_fn.term.id}}",
            "raw_complaint": "{{node.ask_complaint.answer}}",
        },
        outputs=[out("status", "STRING")],
        transitions=[always("ask_first_name")],
    ),
    question_node(
        "ask_triage_question_3", "ask-triage-question-3",
        "{{node.resolve_triage_retry_fn.triage_question}}",
        transitions=[always("resolve_triage_retry_2_fn")],
    ),
    function_node(
        "resolve_triage_retry_2_fn", "resolve-triage-retry-2-fn", "resolve_triage",
        inputs={
            "triage_answer": "{{node.ask_triage_question_3.answer}}",
            "triage_question": "{{node.resolve_triage_retry_fn.triage_question}}",
            "term_a_id": "{{node.match_issue_fn.term_a_id}}",
            "term_b_id": "{{node.match_issue_fn.term_b_id}}",
            # No round 4 to use a follow-up question -- tells the backend
            # not to bother generating one on a 3rd unclear answer.
            "final_round": "true",
        },
        outputs=[
            out("status", "STRING"),
            out("term", "CUSTOM", nullable=True, custom_schema=TERM_SCHEMA),
            out("confirm_prompt", "STRING", nullable=True),
            out("alternate_1_id", "INTEGER", nullable=True),
            out("alternate_1_label", "STRING", nullable=True),
        ],
        transitions=[
            equal("resolve_triage_retry_2_fn", "status", "matched", "confirm_triage_3"),
            # 3rd unclear answer in a row -- give up honestly rather than
            # asking a 4th time or forcing a guess.
            always("dead_end_triage_exhausted"),
        ],
    ),
    question_node(
        "confirm_triage_3", "confirm-triage-3",
        "{{node.resolve_triage_retry_2_fn.confirm_prompt}}",
        answer_guidelines="Classify the caller's reply as YES or NO for the answer field only -- never say the word YES or NO out loud yourself.",
        transitions=[
            equal("confirm_triage_3", "answer", "YES", "save_term_triaged_3"),
            always("ask_complaint"),
        ],
    ),
    function_node(
        "save_term_triaged_3", "save-term-triaged-3", "update_call",
        inputs={
            "matched_term_id": "{{node.resolve_triage_retry_2_fn.term.id}}",
            "raw_complaint": "{{node.ask_complaint.answer}}",
        },
        outputs=[out("status", "STRING")],
        transitions=[always("ask_first_name")],
    ),
    freeform_node(
        "dead_end_triage_exhausted", "dead-end-triage-exhausted",
        (
            "Apologize that you're having trouble pinning down exactly what's going "
            "on. Let the caller know someone from the office will call them back to "
            "help sort it out. Thank them and say <|hangup|>."
        ),
    ),
    question_node(
        "ask_clarify", "ask-clarify",
        "{{node.match_issue_fn.clarify_prompt}}",
        transitions=[always("match_issue_retry_fn")],
    ),
    function_node(
        # Deliberate scope limit, not an oversight: this retry can now
        # legitimately come back needs_triage too (match_issue no longer
        # forces a guess on a second call), but wiring that into its own
        # 4th triage entry point would mean duplicating round 1's node
        # pair again for a rare, compounding edge case (zero clinical
        # signal on attempt 1 AND still genuinely ambiguous on attempt 2).
        # Any non-"matched" outcome here -- no_match, needs_triage, a
        # second needs_clarification -- falls through to the same honest
        # callback dead end. That's a real behavior change from before
        # this session (previously final_attempt forced a low-confidence
        # guess instead), but strictly a better one: never a forced guess,
        # just occasionally a callback where a fuller triage loop could in
        # principle have resolved it. Revisit if this path proves common
        # in real calls.
        "match_issue_retry_fn", "match-issue-retry-fn", "match_issue",
        inputs={
            "complaint_text": "{{node.ask_complaint.answer}} {{node.ask_clarify.answer}}",
        },
        outputs=[
            out("status", "STRING"),
            out("term", "CUSTOM", nullable=True, custom_schema=TERM_SCHEMA),
            out("confirm_prompt", "STRING", nullable=True),
            out("clarify_prompt", "STRING", nullable=True),
            out("spoken_response", "STRING", nullable=True),
        ],
        transitions=[
            equal("match_issue_retry_fn", "status", "matched", "confirm_complaint_2"),
            always("dead_end_no_match_clarified"),
        ],
    ),
    question_node(
        "confirm_complaint_2", "confirm-complaint-2",
        "{{node.match_issue_retry_fn.confirm_prompt}}",
        answer_guidelines="Classify the caller's reply as YES or NO for the answer field only -- never say the word YES or NO out loud yourself.",
        transitions=[
            equal("confirm_complaint_2", "answer", "YES", "save_term_clarified"),
            always("dead_end_no_match_clarified"),
        ],
    ),
    function_node(
        "save_term_matched", "save-term-matched", "update_call",
        inputs={
            "matched_term_id": "{{node.match_issue_fn.term.id}}",
            "raw_complaint": "{{node.ask_complaint.answer}}",
        },
        outputs=[out("status", "STRING")],
        transitions=[always("ask_first_name")],
    ),
    function_node(
        "save_term_clarified", "save-term-clarified", "update_call",
        inputs={
            "matched_term_id": "{{node.match_issue_retry_fn.term.id}}",
            "raw_complaint": "{{node.ask_complaint.answer}}",
        },
        outputs=[out("status", "STRING")],
        transitions=[always("ask_first_name")],
    ),
    function_node(
        "find_doctors_fn", "find-doctors-fn", "find_doctors",
        inputs={
            "date_of_birth": "{{node.ask_dob.answer}}",
            "zip": "{{node.ask_zip.answer}}",
        },
        outputs=[
            out("status", "STRING"),
            out("doctors", "CUSTOM", nullable=True, custom_schema=DOCTORS_ARRAY_SCHEMA),
            out("term_urgency", "STRING", nullable=True),
            out("best_doctor_id", "INTEGER", nullable=True),
            out("best_practice_id", "INTEGER", nullable=True),
            out("best_doctor_spoken_label", "STRING", nullable=True),
            out("best_doctor_name", "STRING", nullable=True),
            out("spoken_response", "STRING", nullable=True),
        ],
        transitions=[
            equal("find_doctors_fn", "status", "matched", "check_availability_fn"),
            equal("find_doctors_fn", "status", "no_eligible_doctor", "dead_end_no_doctor"),
            always("dead_end_system_error"),
        ],
    ),
    function_node(
        "check_availability_fn", "check-availability-fn", "get_availability",
        inputs={
            "doctor_id": "{{node.find_doctors_fn.best_doctor_id}}",
            "practice_id": "{{node.find_doctors_fn.best_practice_id}}",
            "urgency": "{{node.find_doctors_fn.term_urgency}}",
        },
        outputs=[
            out("status", "STRING"),
            out("slots", "CUSTOM", nullable=True, custom_schema=SLOTS_ARRAY_SCHEMA),
            out("urgent_window_met", "BOOLEAN", nullable=True),
            out("different_practice_name", "STRING", nullable=True),
        ],
        started_message="I found {{node.find_doctors_fn.best_doctor_spoken_label}}. Let me check their availability.",
        transitions=[
            equal("check_availability_fn", "status", "slots_available", "present_slots"),
            equal("check_availability_fn", "status", "no_slots", "find_next_doctor_fn"),
            always("dead_end_system_error"),
        ],
    ),
    question_node(
        "present_slots", "present-slots",
        (
            "Let the caller know you found some openings with "
            "{{node.find_doctors_fn.best_doctor_name}} -- just the name, you already "
            "gave their specialty and office a moment ago, do not repeat it. This "
            "visit is NOT urgent unless urgent_window_met is present and literally "
            "the boolean false -- if urgent_window_met is blank, missing, or not "
            "present at all (the normal case for a routine visit), never mention "
            "urgency, being unable to find something sooner, or apologize for the "
            "timing at all, just offer the slots plainly. Only when "
            "{{node.check_availability_fn.urgent_window_met}} is explicitly false, "
            "first let the caller know honestly that you could not find anything "
            "within the next few days for this urgent issue, before listing the "
            "soonest opening you do have. Read out up to 3 of the soonest options "
            "from this list, phrased conversationally -- never read a raw timestamp "
            "verbatim: {{node.check_availability_fn.slots}}. If "
            "{{node.check_availability_fn.different_practice_name}} is not blank, "
            "these openings are at that office rather than the one you just named "
            "-- say so plainly before listing the times (for example, 'the openings "
            "I have are actually at our <office> location'), because the caller "
            "would otherwise turn up at the wrong address."
        ),
        answer_guidelines=(
            "If the caller CLEARLY picks one of the listed times with no question or "
            "hesitation attached, respond with the exact slot_id integer of that slot, "
            "copied from the list above -- never a time string, only the integer id. "
            "If the caller has no preference at all, respond with the slot_id of the "
            "soonest slot. If the caller explicitly asks for OTHER, MORE, or DIFFERENT "
            "time options (not a flat decline, they want alternatives), respond with "
            "exactly MORE. If the caller does not want any of the times offered and "
            "isn't asking for alternatives, respond with exactly NONE. If the caller "
            "asks a question, raises a concern or doubt (e.g. about the doctor, the "
            "practice, or whether this is the right fit), or otherwise mixes a time "
            "preference with something unresolved rather than a clean confirmation, "
            "respond with exactly CONTINUE -- never book on an unclear or mixed answer."
        ),
        transitions=[
            equal("present_slots", "answer", "NONE", "dead_end_no_slots"),
            equal("present_slots", "answer", "MORE", "offer_more_slots_fn"),
            equal("present_slots", "answer", "CONTINUE", "address_concern"),
            always("book_appointment_fn"),
        ],
    ),
    function_node(
        "offer_more_slots_fn", "offer-more-slots-fn", "get_availability",
        inputs={
            "doctor_id": "{{node.find_doctors_fn.best_doctor_id}}",
            "practice_id": "{{node.find_doctors_fn.best_practice_id}}",
            "urgency": "{{node.find_doctors_fn.term_urgency}}",
            "limit": "6",
        },
        outputs=[
            out("status", "STRING"),
            out("slots", "CUSTOM", nullable=True, custom_schema=SLOTS_ARRAY_SCHEMA),
        ],
        transitions=[
            equal("offer_more_slots_fn", "status", "slots_available", "present_more_slots"),
            always("dead_end_no_slots"),
        ],
    ),
    question_node(
        "present_more_slots", "present-more-slots",
        (
            "The caller asked for other time options beyond the ones already "
            "offered ({{node.check_availability_fn.slots}}). Compare those against "
            "this fuller list: {{node.offer_more_slots_fn.slots}}. If the fuller "
            "list has options that were NOT already mentioned, read out only those "
            "new ones, phrased conversationally, never a raw timestamp, and ask "
            "which works. If every option was already mentioned, say honestly that "
            "those are all the openings currently available with this doctor, then "
            "re-read the original times and ask which of them works best -- the "
            "caller only asked what else was available, they have NOT declined "
            "these times, so never end the call here or apologize as though there "
            "were nothing to offer. These times all belong to "
            "{{node.find_doctors_fn.best_doctor_name}} -- that is the ONLY doctor "
            "you may name here; never mention any other doctor or office."
        ),
        answer_guidelines=(
            "If the caller picks one of the times (from either list), respond with "
            "the exact slot_id integer of that slot. If they ask for a different "
            "DOCTOR or provider rather than a different time, respond with exactly "
            "OTHER_DOCTOR. Respond with exactly NONE only if the caller clearly "
            "does not want ANY of the times offered -- never merely because there "
            "were no additional options."
        ),
        transitions=[
            equal("present_more_slots", "answer", "NONE", "dead_end_no_slots"),
            equal("present_more_slots", "answer", "OTHER_DOCTOR", "find_next_doctor_fn"),
            always("book_more_appointment_fn"),
        ],
    ),
    function_node(
        "book_more_appointment_fn", "book-more-appointment-fn", "book_appointment",
        inputs={"slot_id": "{{node.present_more_slots.answer}}"},
        outputs=[
            out("status", "STRING"),
            out("appointment_id", "INTEGER", nullable=True),
            out("confirmation", "CUSTOM", nullable=True, custom_schema=CONFIRMATION_SCHEMA),
        ],
        started_message="Great, let me get that booked for you.",
        transitions=[
            equal("book_more_appointment_fn", "status", "scheduled", "log_scheduled_more_fn"),
            equal("book_more_appointment_fn", "status", "slot_taken", "dead_end_no_slots"),
            always("dead_end_system_error"),
        ],
    ),
    function_node(
        "log_scheduled_more_fn", "log-scheduled-more-fn", "complete_call",
        inputs={
            "status": "scheduled",
            "appointment_id": "{{node.book_more_appointment_fn.appointment_id}}",
        },
        outputs=[out("status", "STRING")],
        transitions=[always("confirm_booking_more")],
    ),
    freeform_node(
        "confirm_booking_more", "confirm-booking-more",
        (
            "Confirm the booking to the caller: "
            "{{node.book_more_appointment_fn.confirmation.doctor}} at "
            "{{node.book_more_appointment_fn.confirmation.practice}}, "
            "{{node.book_more_appointment_fn.confirmation.when}}, a "
            "{{node.book_more_appointment_fn.confirmation.appointment_type}} "
            "appointment. Read it back naturally and clearly. Then ask if there is "
            "anything else you can help with. Wait for their response. Once they say "
            "there is nothing else, thank them and say <|hangup|>."
        ),
    ),
    question_node(
        "address_concern", "address-concern",
        (
            "The caller asked a question or raised a concern instead of clearly "
            "confirming a time -- do NOT book anything yet. If they are explicitly "
            "asking for a DIFFERENT DOCTOR (not just a different time), acknowledge "
            "that and say you'll check who else is available. Otherwise, address "
            "whatever they asked as best you can using only what you already know "
            "from this call (e.g. if they are unsure about the doctor's fit, "
            "reassure them briefly that {{node.find_doctors_fn.best_doctor_name}}'s "
            "office does see patients for this type of issue according to our "
            "scheduling records -- never invent clinical detail you were not given), "
            "then ask again which of the times already mentioned works, or if they'd "
            "like something else."
        ),
        answer_guidelines=(
            "If the caller is explicitly asking for a different doctor or provider "
            "(not just a different time with the same doctor), respond with exactly "
            "OTHER_DOCTOR. If they now pick one of the times, respond with the "
            "exact slot_id integer of that slot. If they decline every time "
            "offered, respond with exactly NONE."
        ),
        # Never routes back to present_slots: that cycle let one unresolved
        # concern bounce between the two nodes indefinitely -- a real call
        # produced nine consecutive "I'll check that before we lock it in"
        # turns and never booked. One concern round, then a decision.
        transitions=[
            equal("address_concern", "answer", "OTHER_DOCTOR", "find_next_doctor_fn"),
            equal("address_concern", "answer", "NONE", "dead_end_no_slots"),
            always("book_after_concern_fn"),
        ],
    ),
    function_node(
        "book_after_concern_fn", "book-after-concern-fn", "book_appointment",
        inputs={"slot_id": "{{node.address_concern.answer}}"},
        outputs=[
            out("status", "STRING"),
            out("appointment_id", "INTEGER", nullable=True),
            out("confirmation", "CUSTOM", nullable=True, custom_schema=CONFIRMATION_SCHEMA),
        ],
        started_message="Great, let me get that booked for you.",
        transitions=[
            equal("book_after_concern_fn", "status", "scheduled", "log_scheduled_after_concern_fn"),
            equal("book_after_concern_fn", "status", "slot_taken", "dead_end_no_slots"),
            always("dead_end_system_error"),
        ],
    ),
    function_node(
        "log_scheduled_after_concern_fn", "log-scheduled-after-concern-fn", "complete_call",
        inputs={
            "status": "scheduled",
            "appointment_id": "{{node.book_after_concern_fn.appointment_id}}",
        },
        outputs=[out("status", "STRING")],
        transitions=[always("confirm_booking_after_concern")],
    ),
    freeform_node(
        "confirm_booking_after_concern", "confirm-booking-after-concern",
        (
            "Confirm the booking to the caller: "
            "{{node.book_after_concern_fn.confirmation.doctor}} at "
            "{{node.book_after_concern_fn.confirmation.practice}}, "
            "{{node.book_after_concern_fn.confirmation.when}}, a "
            "{{node.book_after_concern_fn.confirmation.appointment_type}} "
            "appointment. Read it back naturally and clearly. Then ask if there is "
            "anything else you can help with. Wait for their response. Once they "
            "say there is nothing else, thank them and say <|hangup|>."
        ),
    ),
    function_node(
        "find_next_doctor_fn", "find-next-doctor-fn", "find_doctors",
        inputs={
            "date_of_birth": "{{node.ask_dob.answer}}",
            "zip": "{{node.ask_zip.answer}}",
            "excluded_doctor_id": "{{node.find_doctors_fn.best_doctor_id}}",
        },
        outputs=[
            out("status", "STRING"),
            out("term_urgency", "STRING", nullable=True),
            out("best_doctor_id", "INTEGER", nullable=True),
            out("best_practice_id", "INTEGER", nullable=True),
            out("best_doctor_spoken_label", "STRING", nullable=True),
            out("best_doctor_name", "STRING", nullable=True),
            out("spoken_response", "STRING", nullable=True),
        ],
        transitions=[
            equal("find_next_doctor_fn", "status", "matched", "check_next_doctor_availability_fn"),
            equal("find_next_doctor_fn", "status", "no_eligible_doctor", "no_other_doctor"),
            always("dead_end_system_error"),
        ],
    ),
    function_node(
        "check_next_doctor_availability_fn", "check-next-doctor-availability-fn", "get_availability",
        inputs={
            "doctor_id": "{{node.find_next_doctor_fn.best_doctor_id}}",
            "practice_id": "{{node.find_next_doctor_fn.best_practice_id}}",
            "urgency": "{{node.find_next_doctor_fn.term_urgency}}",
        },
        outputs=[
            out("status", "STRING"),
            out("slots", "CUSTOM", nullable=True, custom_schema=SLOTS_ARRAY_SCHEMA),
            out("urgent_window_met", "BOOLEAN", nullable=True),
            out("different_practice_name", "STRING", nullable=True),
        ],
        started_message="Let me check with {{node.find_next_doctor_fn.best_doctor_spoken_label}} instead.",
        transitions=[
            equal("check_next_doctor_availability_fn", "status", "slots_available", "present_next_doctor_slots"),
            equal("check_next_doctor_availability_fn", "status", "no_slots", "no_other_doctor"),
            always("dead_end_system_error"),
        ],
    ),
    question_node(
        "present_next_doctor_slots", "present-next-doctor-slots",
        (
            "Let the caller know you found some openings with "
            "{{node.find_next_doctor_fn.best_doctor_name}}. This visit is NOT urgent "
            "unless urgent_window_met is present and literally the boolean false -- "
            "if blank, missing, or not present, never mention urgency. Only when "
            "{{node.check_next_doctor_availability_fn.urgent_window_met}} is "
            "explicitly false, first let the caller know honestly you could not find "
            "anything within the next few days for this urgent issue, before listing "
            "the soonest opening. Read out up to 3 of the soonest options, phrased "
            "conversationally, never a raw timestamp: "
            "{{node.check_next_doctor_availability_fn.slots}}. If "
            "{{node.check_next_doctor_availability_fn.different_practice_name}} is "
            "not blank, these openings are at that office rather than the one you "
            "just named -- say so plainly before listing the times."
        ),
        answer_guidelines=(
            "If the caller CLEARLY picks one of the listed times, respond with the "
            "exact slot_id integer. If they have no preference, respond with the "
            "slot_id of the soonest slot. Otherwise (they don't want any of these "
            "either), respond with exactly NONE."
        ),
        transitions=[
            equal("present_next_doctor_slots", "answer", "NONE", "dead_end_no_slots"),
            always("book_next_doctor_appointment_fn"),
        ],
    ),
    function_node(
        "book_next_doctor_appointment_fn", "book-next-doctor-appointment-fn", "book_appointment",
        inputs={"slot_id": "{{node.present_next_doctor_slots.answer}}"},
        outputs=[
            out("status", "STRING"),
            out("appointment_id", "INTEGER", nullable=True),
            out("confirmation", "CUSTOM", nullable=True, custom_schema=CONFIRMATION_SCHEMA),
        ],
        started_message="Great, let me get that booked for you.",
        transitions=[
            equal("book_next_doctor_appointment_fn", "status", "scheduled", "log_scheduled_next_doctor_fn"),
            equal("book_next_doctor_appointment_fn", "status", "slot_taken", "dead_end_no_slots"),
            always("dead_end_system_error"),
        ],
    ),
    function_node(
        "log_scheduled_next_doctor_fn", "log-scheduled-next-doctor-fn", "complete_call",
        inputs={
            "status": "scheduled",
            "appointment_id": "{{node.book_next_doctor_appointment_fn.appointment_id}}",
        },
        outputs=[out("status", "STRING")],
        transitions=[always("confirm_booking_next_doctor")],
    ),
    freeform_node(
        "confirm_booking_next_doctor", "confirm-booking-next-doctor",
        (
            "Confirm the booking to the caller: "
            "{{node.book_next_doctor_appointment_fn.confirmation.doctor}} at "
            "{{node.book_next_doctor_appointment_fn.confirmation.practice}}, "
            "{{node.book_next_doctor_appointment_fn.confirmation.when}}, a "
            "{{node.book_next_doctor_appointment_fn.confirmation.appointment_type}} "
            "appointment. Read it back naturally and clearly. Then ask if there is "
            "anything else you can help with. Wait for their response. Once they say "
            "there is nothing else, thank them and say <|hangup|>."
        ),
    ),
    # Asking for a different doctor must never end the call: the caller
    # never declined the original times, they only asked what else existed.
    question_node(
        "no_other_doctor", "no-other-doctor",
        (
            "Let the caller know honestly that there isn't another doctor "
            "available for this -- either no one else here treats it, or the "
            "others have nothing open right now. Then look at "
            "{{node.find_doctors_fn.best_doctor_name}}'s times: "
            "{{node.check_availability_fn.slots}}. If that list has times in "
            "it, re-read them conversationally (never a raw timestamp) and ask "
            "whether one would work after all. If that list is EMPTY, do not "
            "stall, apologize repeatedly, or say you cannot see the times -- "
            "say plainly that there is nothing available to book right now and "
            "that someone from the office will call them back, then stop."
        ),
        answer_guidelines=(
            "If the caller picks one of the times, respond with the exact slot_id "
            "integer of that slot. If there were no times to offer, or the caller "
            "declines them all, respond with exactly NONE."
        ),
        transitions=[
            equal("no_other_doctor", "answer", "NONE", "dead_end_no_slots"),
            always("book_fallback_appointment_fn"),
        ],
    ),
    function_node(
        # Books from no_other_doctor's OWN answer. Routing this back to
        # book_appointment_fn sent Vogent the literal, unresolved string
        # "{{node.present_slots.answer}}" -- present_slots never ran on this
        # path -- which 500'd and ended the call on "something went wrong".
        "book_fallback_appointment_fn", "book-fallback-appointment-fn", "book_appointment",
        inputs={"slot_id": "{{node.no_other_doctor.answer}}"},
        outputs=[
            out("status", "STRING"),
            out("appointment_id", "INTEGER", nullable=True),
            out("confirmation", "CUSTOM", nullable=True, custom_schema=CONFIRMATION_SCHEMA),
        ],
        started_message="Great, let me get that booked for you.",
        transitions=[
            equal("book_fallback_appointment_fn", "status", "scheduled", "log_scheduled_fallback_fn"),
            equal("book_fallback_appointment_fn", "status", "slot_taken", "dead_end_no_slots"),
            always("dead_end_system_error"),
        ],
    ),
    function_node(
        "log_scheduled_fallback_fn", "log-scheduled-fallback-fn", "complete_call",
        inputs={
            "status": "scheduled",
            "appointment_id": "{{node.book_fallback_appointment_fn.appointment_id}}",
        },
        outputs=[out("status", "STRING")],
        transitions=[always("confirm_booking_fallback")],
    ),
    freeform_node(
        "confirm_booking_fallback", "confirm-booking-fallback",
        (
            "Confirm the booking to the caller: "
            "{{node.book_fallback_appointment_fn.confirmation.doctor}} at "
            "{{node.book_fallback_appointment_fn.confirmation.practice}}, "
            "{{node.book_fallback_appointment_fn.confirmation.when}}, a "
            "{{node.book_fallback_appointment_fn.confirmation.appointment_type}} "
            "appointment. Read it back naturally and clearly. Then ask if there is "
            "anything else you can help with. Wait for their response. Once they "
            "say there is nothing else, thank them and say <|hangup|>."
        ),
    ),
    function_node(
        "book_appointment_fn", "book-appointment-fn", "book_appointment",
        inputs={"slot_id": "{{node.present_slots.answer}}"},
        outputs=[
            out("status", "STRING"),
            out("appointment_id", "INTEGER", nullable=True),
            out("confirmation", "CUSTOM", nullable=True, custom_schema=CONFIRMATION_SCHEMA),
        ],
        started_message="Great, let me get that booked for you.",
        transitions=[
            equal("book_appointment_fn", "status", "scheduled", "log_scheduled_fn"),
            equal("book_appointment_fn", "status", "slot_taken", "dead_end_no_slots"),
            always("dead_end_system_error"),
        ],
    ),
    function_node(
        "log_scheduled_fn", "log-scheduled-fn", "complete_call",
        inputs={
            "status": "scheduled",
            "appointment_id": "{{node.book_appointment_fn.appointment_id}}",
        },
        outputs=[out("status", "STRING")],
        transitions=[always("confirm_booking")],
    ),
    freeform_node(
        "confirm_booking", "confirm-booking",
        (
            "Confirm the booking to the caller: "
            "{{node.book_appointment_fn.confirmation.doctor}} at "
            "{{node.book_appointment_fn.confirmation.practice}}, "
            "{{node.book_appointment_fn.confirmation.when}}, a "
            "{{node.book_appointment_fn.confirmation.appointment_type}} appointment. "
            "Read it back naturally and clearly. Then ask if there is anything else "
            "you can help with. Wait for their response. Once they say there is "
            "nothing else, thank them and say <|hangup|>."
        ),
    ),
    function_node(
        # Without this the dashboard showed a cleanly-declined call as
        # "in_progress" until the 15-minute abandoned sweep mislabelled it.
        "log_no_match_fn", "log-no-match-fn", "complete_call",
        inputs={"status": "no_match"},
        outputs=[out("status", "STRING")],
        transitions=[always("dead_end_no_match_direct")],
    ),
    freeform_node(
        "dead_end_no_match_direct", "dead-end-no-match-direct",
        "Say exactly: {{node.match_issue_fn.spoken_response}} Then say <|hangup|>.",
    ),
    freeform_node(
        # Reached for every non-matched retry outcome (no_match,
        # needs_clarification, needs_triage, error) -- match_issue_retry_fn
        # only populates spoken_response for no_match/error, so referencing
        # it directly here would speak an unresolved template variable
        # literally for the other statuses. A static message is always
        # safe regardless of which status actually came back.
        "dead_end_no_match_clarified", "dead-end-no-match-clarified",
        (
            "Say exactly: I'm sorry, I'm still having trouble pinning down what's "
            "going on -- let me have someone from our office call you back to help "
            "sort this out. Then say <|hangup|>."
        ),
    ),
    freeform_node(
        "dead_end_no_doctor", "dead-end-no-doctor",
        "Say exactly: {{node.find_doctors_fn.spoken_response}} Then say <|hangup|>.",
    ),
    # Separate dead ends per source node: this text is a static template
    # baked in at flow-build time, not dynamically resolved per call, so a
    # single shared node can only ever speak ONE specific upstream node's
    # spoken_response. Reusing dead_end_no_doctor here spoke a blank/broken
    # reference on every call that reached "no eligible doctor" via the
    # doctor/office-request path instead of the normal by-issue path --
    # caught by skeptic review before either of these two saw a live call.
    freeform_node(
        "dead_end_no_requested_doctor", "dead-end-no-requested-doctor",
        "Say exactly: {{node.find_requested_doctor_fn.spoken_response}} Then say <|hangup|>.",
    ),
    freeform_node(
        "dead_end_no_chosen_doctor", "dead-end-no-chosen-doctor",
        "Say exactly: {{node.resolve_chosen_doctor_fn.spoken_response}} Then say <|hangup|>.",
    ),
    freeform_node(
        "dead_end_ambiguous", "dead-end-ambiguous",
        "Say exactly: {{node.lookup_patient.spoken_response}} Then say <|hangup|>.",
    ),
    freeform_node(
        "dead_end_no_slots", "dead-end-no-slots",
        (
            "Apologize that you don't currently have an opening that works. Let the "
            "caller know someone from the office will call them back to help find a "
            "time. Thank them and say <|hangup|>."
        ),
    ),
    freeform_node(
        "dead_end_system_error", "dead-end-system-error",
        (
            "Apologize that something went wrong on our end. Let the caller know "
            "someone will call them back shortly to help. Thank them and say "
            "<|hangup|>."
        ),
    ),
]


def build_and_publish():
    flow_definition = {
        "nodes": nodes,
        "globalContext": GLOBAL_CONTEXT,
        "openingLineType": "INBOUND_ONLY",
        "aiOpen": False,
    }

    payload = {
        # GPT-5.5 Flow -- a GPT-based model on this account purpose-built
        # for Flow Builder agents (name pattern matches Vogent's own
        # flow-oriented models, e.g. "Vogent Survey v4"). See `GET /models`
        # for the full list available on this account.
        "aiModelId": os.environ.get("VOGENT_AI_MODEL_ID", "27390747-6ebb-4d4a-af57-0ebf52f3324e"),
        "agentType": "CUSTOM_FLOW",
        "name": "mediportal-flow-v2",
        "prompt": None,
        "flowDefinition": flow_definition,
    }
    resp = requests.post(
        f"{VOGENT_API}/agents/{AGENT_ID}/versioned_prompts", headers=HEADERS, json=payload
    )
    if not resp.ok:
        print("FAILED:", resp.status_code, resp.text[:2000])
        resp.raise_for_status()
    prompt_id = resp.json()["id"]
    print("created versioned prompt:", prompt_id)

    # Set it as the agent's default so it's what a real call actually runs.
    agent_resp = requests.get(f"{VOGENT_API}/agents/{AGENT_ID}", headers=HEADERS)
    agent_resp.raise_for_status()
    agent = agent_resp.json()
    agent["defaultVersionedPromptId"] = prompt_id
    # idleMessageConfig comes back from GET with zeroed fields when disabled,
    # which PUT's stricter validation rejects (durations/counts must be >=
    # their minimums even when the feature is off) -- omit it to leave it
    # untouched rather than round-tripping an invalid value back.
    agent.pop("idleMessageConfig", None)
    update_resp = requests.put(f"{VOGENT_API}/agents/{AGENT_ID}", headers=HEADERS, json=agent)
    if not update_resp.ok:
        print("FAILED to set default:", update_resp.status_code, update_resp.text[:2000])
        update_resp.raise_for_status()
    print("agent default versioned prompt updated to:", prompt_id)
    return prompt_id


if __name__ == "__main__":
    build_and_publish()
