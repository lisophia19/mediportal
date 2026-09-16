# Repoints all 10 already-created Vogent functions at the real backend URL
# (BACKEND_PUBLIC_URL in .env) via PATCH, rather than recreating them --
# function IDs referenced by the live flow stay the same, so no flow
# rebuild/republish is needed after this.
import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(REPO_ROOT / ".env")

VOGENT_API = "https://api.vogent.ai/api"
API_KEY = os.environ["VOGENT_API_KEY"]
BASE_URL = os.environ["BACKEND_PUBLIC_URL"]
AGENT_KEY = os.environ.get("AGENT_KEY", "dev-agent-key")
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
WEBHOOK_HEADERS = [{"key": "X-Agent-Key", "value": AGENT_KEY}]

# name -> apiPath suffix, must match vogent_setup.py's FUNCTIONS list
PATHS = {
    "create_call": "/calls",
    "lookup_patient": "/patients/lookup",
    "create_patient": "/patients",
    "match_issue": "/routing/match-issue",
    "find_doctors": "/routing/find-doctors",
    "get_availability": "/availability",
    "book_appointment": "/appointments",
    "update_call": "/calls/update",
    "complete_call": "/calls/complete",
    "confirm_patient": "/patients/confirm",
}

function_ids = json.load(open(Path(__file__).resolve().parent / "vogent_function_ids.json"))

for name, suffix in PATHS.items():
    fid = function_ids[name]
    new_path = BASE_URL + suffix

    current = requests.get(f"{VOGENT_API}/functions/{fid}", headers=HEADERS)
    if not current.ok:
        print(f"FAILED to fetch {name}: {current.status_code} {current.text[:300]}")
        continue
    body = current.json()
    body.pop("id", None)  # not part of the update payload
    body["apiPath"] = new_path
    body["headers"] = WEBHOOK_HEADERS  # was null on the originally-created functions

    resp = requests.put(f"{VOGENT_API}/functions/{fid}", headers=HEADERS, json=body)
    if not resp.ok:
        print(f"FAILED {name}: {resp.status_code} {resp.text[:300]}")
        continue
    print(f"updated {name} -> {new_path}")

print("\nDone. Function IDs unchanged, so the live flow does not need republishing.")
