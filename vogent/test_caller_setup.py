# One-time (re-runnable) script that creates the synthetic "test caller"
# Vogent agent -- a separate outbound agent that roleplays a real patient
# so we can place real calls into mediportal-agent and check the resulting
# transcript/booking/dashboard data, instead of testing by hand every time.
#
# Not part of the Flask app; an ops script, mirroring vogent_setup.py.
import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_IDS_PATH = Path(__file__).resolve().parent / "test_caller_agent_id.json"

load_dotenv(REPO_ROOT / ".env")

VOGENT_API = "https://api.vogent.ai/api"
API_KEY = os.environ["VOGENT_API_KEY"]
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

# Reuses mediportal-agent's voice/model ids -- not load-bearing for this
# agent, just avoids needing to look up separate ones.
VOICE_ID = "b2355dc7-1bd9-4fb3-812f-af24c7228d7f"
AI_MODEL_ID = "27390747-6ebb-4d4a-af57-0ebf52f3324e"

PROMPT = """You are roleplaying as a real patient calling an orthopedic practice's \
phone scheduling line to test it. Stay fully in character as the patient for the \
whole call -- never mention that this is a test, never break character, never \
say you are an AI.

Your scenario for this call: {{scenario}}

Behave like a real caller: answer naturally in your own words (not scripted lines), \
react to whatever the agent actually asks rather than reciting facts out of order, \
and follow the scenario's intent even if the agent's exact wording differs from what \
you expected. If the agent asks something the scenario doesn't cover, improvise a \
reasonable, consistent answer and stick with it for the rest of the call. When the \
call reaches a natural end (booking confirmed, or the agent has clearly declined/ \
redirected you), say a brief goodbye and stop talking."""


def create_test_caller_agent():
    payload = {
        "name": "mediportal-test-caller",
        "defaultVoiceId": VOICE_ID,
        "defaultVersionedPrompt": {
            "aiModelId": AI_MODEL_ID,
            "agentType": "STANDARD",
            "name": "test-caller-prompt-v1",
            "prompt": PROMPT,
        },
        "inboundWebhookResponse": False,
        "inboundWebhookUrl": "",
    }
    resp = requests.post(f"{VOGENT_API}/agents", headers=HEADERS, json=payload)
    resp.raise_for_status()
    agent_id = resp.json()["id"]
    print(f"created mediportal-test-caller agent: {agent_id}")
    return agent_id


if __name__ == "__main__":
    agent_id = create_test_caller_agent()
    with open(AGENT_IDS_PATH, "w") as f:
        json.dump({"test_caller_agent_id": agent_id}, f, indent=2)
    print(f"\nWrote {AGENT_IDS_PATH}")
