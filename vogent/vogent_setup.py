# One-time (re-runnable) script that builds the mediportal Vogent agent's
# functions and flow against the live Vogent API, from the backend's real
# spec §5 contracts. Not part of the Flask app; a deployment/ops script.
#
# BASE_URL is a placeholder until the backend has a real public URL (ngrok
# or a deployment) -- swap it and re-run function creation (or PATCH each
# function's apiPath) once one exists.
import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
FUNCTION_IDS_PATH = Path(__file__).resolve().parent / "vogent_function_ids.json"

load_dotenv(REPO_ROOT / ".env")

VOGENT_API = "https://api.vogent.ai/api"
AGENT_ID = os.environ["VOGENT_AGENT_ID"]
API_KEY = os.environ["VOGENT_API_KEY"]
BASE_URL = os.environ.get("BACKEND_PUBLIC_URL", "https://REPLACE-ME.ngrok.app/api/v1")
AGENT_KEY = os.environ.get("AGENT_KEY", "dev-agent-key")

HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
WEBHOOK_HEADERS = [{"key": "X-Agent-Key", "value": AGENT_KEY}]


def _schema(properties, required):
    return json.dumps(
        {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
    )


# (name, description, apiPath suffix, input schema)
FUNCTIONS = [
    (
        "create_call",
        "Start a call record at the beginning of the call.",
        "/calls",
        _schema(
            {
                "vogent_call_id": {"type": "string", "description": "Vogent's call ID"},
                "caller_phone": {"type": "string", "description": "Caller ID / ANI, e.g. {{toNumber}}"},
            },
            ["vogent_call_id"],
        ),
    ),
    (
        "lookup_patient",
        "Look up an existing patient by last name and date of birth (spec §5.3).",
        "/patients/lookup",
        _schema(
            {
                "last_name": {"type": "string"},
                "date_of_birth": {"type": "string", "description": "YYYY-MM-DD"},
                "first_name": {"type": "string", "description": "Soft signal only, not a hard filter"},
                "call_id": {"type": "string", "description": "vogent_call_id, used to pull caller_phone"},
                "excluded_patient_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Candidate IDs already rejected via a 'no' to confirm_prompt",
                },
            },
            ["last_name", "date_of_birth"],
        ),
    ),
    (
        "create_patient",
        "Create a new patient record (spec §5.4).",
        "/patients",
        _schema(
            {
                "first_name": {"type": "string"},
                "last_name": {"type": "string"},
                "date_of_birth": {"type": "string", "description": "YYYY-MM-DD"},
                "phone": {"type": "string"},
                "home_zip": {"type": "string"},
            },
            ["first_name", "last_name", "date_of_birth", "phone"],
        ),
    ),
    (
        "update_patient_zip",
        "Persist the ZIP asked later in the call onto the patient this call resolved to.",
        "/patients/update-zip",
        _schema(
            {
                "zip": {"type": "string"},
                "patient_id": {"type": "integer"},
                "call_id": {"type": "string"},
            },
            ["zip"],
        ),
    ),
    (
        "match_issue",
        "Match the caller's free-text complaint to a clinical term (spec §5.1).",
        "/routing/match-issue",
        _schema(
            {
                "complaint_text": {"type": "string", "description": "Caller's complaint, verbatim"},
                "call_id": {"type": "string"},
            },
            ["complaint_text"],
        ),
    ),
    (
        "find_doctors",
        "Rank eligible doctors for a matched term by age and proximity (spec §5.2/§5.9).",
        "/routing/find-doctors",
        _schema(
            {
                "term_id": {"type": "integer"},
                "date_of_birth": {"type": "string", "description": "YYYY-MM-DD"},
                "zip": {"type": "string", "description": "Caller's ZIP for proximity ranking"},
                "call_id": {"type": "string"},
            },
            ["term_id", "date_of_birth"],
        ),
    ),
    (
        "get_availability",
        "Get open slots for a doctor (spec §5.5). Pass urgency='URGENT' for urgent terms.",
        "/availability",
        _schema(
            {
                "doctor_id": {"type": "integer"},
                "practice_id": {"type": "integer"},
                "appointment_type": {"type": "string"},
                "urgency": {"type": "string", "description": "'URGENT' or omit"},
                "limit": {"type": "integer"},
            },
            ["doctor_id"],
        ),
    ),
    (
        "book_appointment",
        "Book a specific slot for a patient (spec §5.6).",
        "/appointments",
        _schema(
            {
                "slot_id": {"type": "integer"},
                "patient_id": {"type": "integer"},
                "term_id": {"type": "integer"},
                "call_id": {"type": "string"},
            },
            ["slot_id", "patient_id", "term_id"],
        ),
    ),
    (
        "update_call",
        "Record incremental call progress (matched term, complaint, patient) mid-call.",
        "/calls/update",
        _schema(
            {
                "vogent_call_id": {"type": "string"},
                "matched_term_id": {"type": "integer"},
                "raw_complaint": {"type": "string"},
                "patient_id": {"type": "integer"},
                "appointment_id": {"type": "integer"},
                "status": {"type": "string"},
            },
            ["vogent_call_id"],
        ),
    ),
    (
        "complete_call",
        "End-of-call webhook: final status and transcript (spec §5.7).",
        "/calls/complete",
        _schema(
            {
                "vogent_call_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["scheduled", "no_match", "no_slots", "abandoned", "failed"],
                },
                "transcript": {"type": "array", "items": {"type": "object"}},
            },
            ["vogent_call_id"],
        ),
    ),
    (
        "resolve_triage",
        "Resolve a hip-vs-spine screening answer to one of the two candidate terms (spec §5.1 needs_triage).",
        "/routing/resolve-triage",
        _schema(
            {
                "triage_answer": {"type": "string", "description": "Caller's answer to the screening question"},
                "hip_term_id": {"type": "integer"},
                "spine_term_id": {"type": "integer"},
                "call_id": {"type": "string"},
            },
            ["triage_answer", "hip_term_id", "spine_term_id"],
        ),
    ),
]


def create_functions():
    """Creates all functions fresh and returns {name: function_id}. Safe to
    re-run: Vogent doesn't dedupe by name, so re-running creates duplicates --
    intended for a clean first run or after manually clearing old ones."""
    ids = {}
    for name, description, path_suffix, input_schema in FUNCTIONS:
        payload = {
            "name": name,
            "description": description,
            "type": "api",
            "apiPath": BASE_URL + path_suffix,
            "headers": WEBHOOK_HEADERS,
            "inputJsonSchema": input_schema,
        }
        resp = requests.post(f"{VOGENT_API}/functions", headers=HEADERS, json=payload)
        resp.raise_for_status()
        function_id = resp.json()["id"]
        ids[name] = function_id
        print(f"created {name}: {function_id}")
    return ids


if __name__ == "__main__":
    function_ids = create_functions()
    with open(FUNCTION_IDS_PATH, "w") as f:
        json.dump(function_ids, f, indent=2)
    print(f"\nWrote {FUNCTION_IDS_PATH}")
