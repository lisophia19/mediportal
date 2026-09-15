# Builds the mediportal-agent's real conversation flow (spec §6) and pushes
# it live as a new versioned prompt, replacing whatever's currently set as
# default (the stock "Shiny Smiles" demo template). Run after vogent_setup.py
# has created the function definitions (reads vogent_function_ids.json).
#
# Known simplifications in this first build (see the chat/report for why):
#  - Patient disambiguation retries only once (no excluded_patient_ids loop).
#  - Complaint clarification only retries once (spec allows up to 2 rounds).
#  - Only the single closest eligible doctor is tried (spec's "walk the
#    ranked list on no_slots" is not implemented -- one no_slots ends the call).
#  - A slot-taken race apologizes rather than auto-re-offering alternates.
#  - Dead-end calls do not call complete_call to record a final status (they
#    rely on the 15-minute abandoned sweep) -- keeps the node count down for
#    this first pass.
import json
import os

import requests
from dotenv import load_dotenv

load_dotenv()

VOGENT_API = "https://api.vogent.ai/api"
AGENT_ID = os.environ["VOGENT_AGENT_ID"]
API_KEY = os.environ["VOGENT_API_KEY"]
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

FN = json.load(open("vogent_function_ids.json"))


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
        started_message="Thanks for calling -- I can help get you scheduled today.",
        transitions=[always("ask_first_name")],
    ),
    question_node(
        "ask_first_name", "ask-first-name",
        "Could I get your first name?",
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
        transitions=[always("ask_zip")],
    ),
    question_node(
        "confirm_patient_readback", "confirm-patient-readback",
        "{{node.lookup_patient.confirm_prompt}}",
        answer_guidelines="If the caller confirms this is them (yes, correct, that's right, etc.), respond with exactly YES. Otherwise respond with exactly NO.",
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
        transitions=[always("ask_zip")],
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
        transitions=[always("ask_complaint")],
    ),
    question_node(
        "ask_complaint", "ask-complaint",
        "What's going on that brings you in today?",
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
        ],
        transitions=[
            equal("match_issue_fn", "status", "matched", "confirm_complaint"),
            equal("match_issue_fn", "status", "needs_clarification", "ask_clarify"),
            equal("match_issue_fn", "status", "no_match", "dead_end_no_match_direct"),
            always("dead_end_system_error"),
        ],
    ),
    question_node(
        "confirm_complaint", "confirm-complaint",
        "{{node.match_issue_fn.confirm_prompt}}",
        answer_guidelines="If the caller confirms (yes, correct, etc.) respond with exactly YES. Otherwise respond with exactly NO.",
        transitions=[
            equal("confirm_complaint", "answer", "YES", "save_term_matched"),
            always("ask_complaint"),
        ],
    ),
    question_node(
        "ask_clarify", "ask-clarify",
        "{{node.match_issue_fn.clarify_prompt}}",
        transitions=[always("match_issue_retry_fn")],
    ),
    function_node(
        "match_issue_retry_fn", "match-issue-retry-fn", "match_issue",
        inputs={"complaint_text": "{{node.ask_complaint.answer}} {{node.ask_clarify.answer}}"},
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
        answer_guidelines="If the caller confirms (yes, correct, etc.) respond with exactly YES. Otherwise respond with exactly NO.",
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
        transitions=[always("find_doctors_fn")],
    ),
    function_node(
        "save_term_clarified", "save-term-clarified", "update_call",
        inputs={
            "matched_term_id": "{{node.match_issue_retry_fn.term.id}}",
            "raw_complaint": "{{node.ask_complaint.answer}}",
        },
        outputs=[out("status", "STRING")],
        transitions=[always("find_doctors_fn")],
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
        ],
        started_message="I found {{node.find_doctors_fn.best_doctor_spoken_label}}. Let me check their availability.",
        transitions=[
            equal("check_availability_fn", "status", "slots_available", "present_slots"),
            equal("check_availability_fn", "status", "no_slots", "dead_end_no_slots"),
            always("dead_end_system_error"),
        ],
    ),
    question_node(
        "present_slots", "present-slots",
        (
            "Let the caller know you found some openings with "
            "{{node.find_doctors_fn.best_doctor_spoken_label}}. If "
            "{{node.check_availability_fn.urgent_window_met}} is false, first let the "
            "caller know honestly that you could not find anything within the next "
            "few days for this urgent issue, but here is the soonest opening you do "
            "have, before listing it. Read out up to 3 of the soonest options from "
            "this list, phrased conversationally -- never read a raw timestamp "
            "verbatim: {{node.check_availability_fn.slots}}"
        ),
        answer_guidelines=(
            "If the caller picks one of the listed times, respond with the exact "
            "slot_id integer of that slot, copied from the list above -- never a "
            "time string, only the integer id. If the caller has no preference, "
            "respond with the slot_id of the soonest slot. If the caller does not "
            "want any of the times offered, respond with exactly NONE."
        ),
        transitions=[
            equal("present_slots", "answer", "NONE", "dead_end_no_slots"),
            always("book_appointment_fn"),
        ],
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
        inputs={"status": "scheduled"},
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
    freeform_node(
        "dead_end_no_match_direct", "dead-end-no-match-direct",
        "Say exactly: {{node.match_issue_fn.spoken_response}} Then say <|hangup|>.",
    ),
    freeform_node(
        "dead_end_no_match_clarified", "dead-end-no-match-clarified",
        "Say exactly: {{node.match_issue_retry_fn.spoken_response}} Then say <|hangup|>.",
    ),
    freeform_node(
        "dead_end_no_doctor", "dead-end-no-doctor",
        "Say exactly: {{node.find_doctors_fn.spoken_response}} Then say <|hangup|>.",
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
        # Reused from the agent's existing (template) versioned prompt --
        # same model already available/valid on this account.
        "aiModelId": os.environ.get("VOGENT_AI_MODEL_ID", "c6c18d01-28ea-45f0-8a89-8154f7e00ad4"),
        "agentType": "CUSTOM_FLOW",
        "name": "mediportal-flow-v1",
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
