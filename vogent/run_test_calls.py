# Places real outbound test calls from mediportal-test-caller into
# mediportal-agent -- each named scenario becomes one live phone call so we
# can check the resulting transcript/booking/dashboard data end-to-end
# instead of testing by hand. Costs real per-minute usage; run scenarios
# deliberately, not on every change.
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

VOGENT_API = "https://api.vogent.ai/api"
API_KEY = os.environ["VOGENT_API_KEY"]
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

TEST_CALLER_AGENT_ID = json.load(
    open(Path(__file__).resolve().parent / "test_caller_agent_id.json")
)["test_caller_agent_id"]
TEST_CALLER_NUMBER_ID = "e4ec8217-d430-4306-b81a-991ac4c97b22"  # +13322203540
MEDIPORTAL_NUMBER = "+17038808652"

SCENARIOS = {
    "triage": (
        "You are a NEW patient calling because you have been having pain, but you "
        "describe it only as 'pain' at first -- don't specify hip or back/spine "
        "unless asked a direct follow-up screening question. If asked whether the "
        "pain travels/shoots down your leg or stays in one spot, say it clearly "
        "stays in one spot and does not travel anywhere -- you are describing a "
        "hip problem. Your name is Alex Testcaller, date of birth March 3rd 1985, "
        "phone 703-555-0199, ZIP 11566. Go through the full booking and confirm "
        "the appointment when offered a slot."
    ),
    "standard_booking": (
        "You are a NEW patient calling because you fell yesterday and think you "
        "may have fractured your wrist -- clear, specific complaint from the "
        "start. Your name is Jamie Testcaller, date of birth July 14th 1990, "
        "phone 703-555-0142, ZIP 11530. Go through the full booking flow and "
        "confirm the first appointment slot offered."
    ),
    "no_match": (
        "You are a NEW patient calling this orthopedic practice about severe "
        "recurring migraines and headaches -- a neurological issue, not an "
        "orthopedic one. If the agent explains they don't treat that and offers "
        "to redirect you elsewhere, accept that gracefully and end the call."
    ),
    "edge_cases": (
        "You are a NEW patient calling about knee pain after a soccer injury. "
        "Be a slightly difficult, realistic caller: hesitate and use filler words "
        "('um', 'let me think'), give your date of birth in an unusual spoken "
        "format (e.g. 'the ninth of September, nineteen ninety-two'), ask the "
        "agent to repeat itself once, and mid-way through answering one question "
        "change your mind and correct yourself. Eventually settle down and "
        "complete the booking normally. Name: Morgan Testcaller, phone "
        "703-555-0177, ZIP 11753."
    ),
}


def place_call(scenario_name):
    scenario_text = SCENARIOS[scenario_name]
    payload = {
        "callAgentId": TEST_CALLER_AGENT_ID,
        "toNumber": MEDIPORTAL_NUMBER,
        "fromNumberId": TEST_CALLER_NUMBER_ID,
        "callAgentInput": {"scenario": scenario_text},
        "timeoutMinutes": 5,
    }
    resp = requests.post(f"{VOGENT_API}/dials", headers=HEADERS, json=payload)
    resp.raise_for_status()
    dial = resp.json()
    dial_id = dial.get("id") or dial.get("dialId") or dial.get("dial_id")
    print(f"[{scenario_name}] raw response: {dial}")
    print(f"[{scenario_name}] dial started: {dial_id}")
    return dial_id


if __name__ == "__main__":
    names = sys.argv[1:] or list(SCENARIOS)
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        print(f"Unknown scenario(s): {unknown}. Choices: {list(SCENARIOS)}")
        sys.exit(1)

    dial_ids = {}
    for name in names:
        dial_ids[name] = place_call(name)
        time.sleep(2)  # avoid hammering the API placing calls back-to-back

    print("\nDial IDs:")
    for name, dial_id in dial_ids.items():
        print(f"  {name}: {dial_id}")
