# Places real outbound test calls from mediportal-test-caller into
# mediportal-agent -- each named scenario becomes one live phone call so we
# can check the resulting transcript/booking/dashboard data end-to-end
# instead of testing by hand. Costs real per-minute usage; run scenarios
# deliberately, not on every change.
#
# Each agent-to-agent call consumes TWO concurrent Vogent call slots (the
# test-caller's outbound leg and mediportal-agent's inbound leg), so the
# runner caps itself well under the workspace concurrency limit.
#
# Verification is DB-side, not from the id printed here: Vogent creates a
# SEPARATE dial for the inbound (mediportal-agent) leg, and that inbound
# dial id is what lands in calls.vogent_call_id. See
# docs/testing/agent-call-test-checklist.md.
import json
import os
import sys
import threading
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

MAX_CONCURRENT_CALLS = 2  # 2 legs each, against a workspace limit of 5
TERMINAL_STATUSES = {"completed", "failed", "canceled", "busy", "no-answer"}

# Shared persona rules appended to every scenario so the test caller behaves
# like a real patient rather than reciting the scenario text back.
_PERSONA_SUFFIX = (
    " Speak naturally and only answer what you're actually asked, one "
    "question at a time -- never volunteer your whole story at once. If the "
    "agent asks something this scenario doesn't cover, improvise something "
    "reasonable and stay consistent for the rest of the call. When the call "
    "reaches a natural end (booking confirmed, or the agent has clearly "
    "declined or redirected you), say a brief goodbye and stop talking."
)

SCENARIOS = {
    # --- core happy path -------------------------------------------------
    "standard_booking": (
        "You are a NEW patient. You fell yesterday and think you may have "
        "fractured your wrist -- say that clearly when asked what's going "
        "on. Name Jamie Brennan, date of birth July 14th 1990, phone "
        "703-555-0142, ZIP 11530. Accept the first appointment time offered "
        "and complete the booking."
    ),
    # --- triage (hip vs spine) -------------------------------------------
    "triage_hip": (
        "You are a NEW patient. When asked what's going on, say only that "
        "you've been having a lot of pain and you're not sure what's "
        "causing it -- do NOT name a body part unless asked directly. If "
        "pressed for a location, say it's around your hip and lower back. "
        "If asked whether the pain travels or shoots down into your leg, "
        "say it clearly stays in one spot and does not travel. Name Pat "
        "Donnelly, date of birth November 5th 1958, phone 703-555-0163, "
        "ZIP 11566. Complete the booking."
    ),
    "triage_spine": (
        "You are a NEW patient. When asked what's going on, say only that "
        "you've been having a lot of pain and you're not sure what's "
        "causing it -- do NOT name a body part unless asked directly. If "
        "pressed for a location, say it's around your hip and lower back. "
        "If asked whether the pain travels or shoots down into your leg, "
        "say yes -- it shoots down your leg, especially when walking. Name "
        "Dana Marsh, date of birth March 22nd 1967, phone "
        "703-555-0164, ZIP 11566. Complete the booking."
    ),
    "triage_hip_vs_knee": (
        "You are a NEW patient. When asked what's going on, say only that "
        "you've been having a lot of pain and you're not sure what's "
        "causing it -- do NOT name a specific body part unless asked "
        "directly. If pressed, say it's somewhere around your hip and "
        "thigh, hard to pin down exactly. Answer whatever discriminating "
        "question the agent asks as naturally as you can, leaning toward "
        "answers that point to a hip issue (e.g. if asked about pain when "
        "putting on shoes/socks or getting in a car, say yes that's "
        "exactly it). Name Robin Castellano, date of birth August 14th "
        "1962, phone 703-555-0178, ZIP 11747. Complete the booking."
    ),
    "triage_neck_vs_shoulder": (
        "You are a NEW patient. When asked what's going on, say only that "
        "your neck and shoulder area has been bothering you and you're not "
        "sure exactly what it is -- do NOT name one specific body part "
        "unless asked directly. Answer whatever discriminating question "
        "the agent asks naturally, leaning toward answers that point to a "
        "neck issue (e.g. if asked whether it radiates down your arm or "
        "gets worse turning your head, say yes). Name Taylor Osei, date of "
        "birth May 9th 1979, phone 703-555-0179, ZIP 11566. Complete the "
        "booking."
    ),
    "triage_exhausted_callback": (
        "You are a NEW patient. When asked what's going on, say only that "
        "you've been having a lot of pain and you're not sure what's "
        "causing it -- do NOT name a body part unless asked directly. For "
        "EVERY discriminating question the agent asks trying to narrow it "
        "down, give a genuinely vague, noncommittal answer that doesn't "
        "clearly point either way (e.g. 'I'm not really sure, it's hard to "
        "say', 'maybe both, I don't know'). Keep doing this for the whole "
        "call -- never give a clear answer. If the agent eventually says "
        "someone from the office will call you back, accept that "
        "gracefully and end the call -- do NOT expect a booking to "
        "complete. Name Casey Fennimore, date of birth June 6th 1990, "
        "phone 703-555-0180, ZIP 11566."
    ),
    # --- clarification / no-signal ---------------------------------------
    "needs_clarification": (
        "You are a NEW patient. When asked what's going on, say only 'my "
        "knee hurts' and nothing more. If the agent asks whether it's more "
        "like an injury or arthritis, say it started after you twisted it "
        "playing tennis, so an injury. Name Alex Whitman, date of birth "
        "April 10th 1999, phone 703-555-0165, ZIP 11530. Complete the "
        "booking."
    ),
    "no_reason_given": (
        "You are a NEW patient. When asked what's going on, say ONLY 'I'd "
        "like to schedule an appointment, please' -- give no medical reason "
        "at all on that first answer. If the agent then asks what's "
        "bringing you in, say your shoulder has been aching for a couple of "
        "weeks. Name Sam Ferris, date of birth January 30th 1975, phone "
        "703-555-0166, ZIP 11530. Complete the booking."
    ),
    "no_reason_given_then_ambiguous": (
        "You are a NEW patient. When asked what's going on, say ONLY 'I'd "
        "like to schedule an appointment, please' -- give no medical reason "
        "at all on that first answer. If the agent then asks what's "
        "bringing you in, say only that you've been having a lot of pain "
        "and you're not sure what's causing it, without naming a specific "
        "body part unless asked directly. This is a compounding edge case "
        "(no signal on attempt 1, still vague on attempt 2) -- the agent "
        "may not be able to fully resolve it and might instead say someone "
        "from the office will call you back. If that happens, accept it "
        "gracefully; if the agent instead asks a follow-up question and "
        "resolves it, answer naturally and complete the booking. Name "
        "Morgan Delacroix, date of birth February 11th 1984, phone "
        "703-555-0181, ZIP 11530."
    ),
    # --- alternatives ----------------------------------------------------
    "more_slots": (
        "You are a NEW patient with wrist pain after a fall last week. When "
        "the agent offers you appointment times, do NOT pick one -- ask if "
        "there are any other times available. If the agent offers more "
        "times, pick one of those. If the agent says those are genuinely "
        "all the openings, accept the earliest one. Name Riley Hoffman, "
        "date of birth August 8th 1982, phone 703-555-0167, ZIP 11530."
    ),
    "other_doctor": (
        "You are a NEW patient with knee pain that's been building for "
        "months. When the agent names a doctor and offers times, ask "
        "whether you could see a different doctor instead. Accept whatever "
        "the agent then offers -- another doctor's times, or an honest "
        "explanation that there isn't another option. Name Jordan "
        "Vance, date of birth May 3rd 1970, phone 703-555-0168, ZIP "
        "11566."
    ),
    # --- patient identity ------------------------------------------------
    "returning_patient": (
        "You are a RETURNING patient who has been seen at this practice "
        "before. Your name is Maria Rodriguez, date of birth April 2nd "
        "1991, phone 516-555-0142, ZIP 11563. You're calling because your "
        "wrist is hurting again. Give your real details when asked -- the "
        "practice should already have your record. Complete the booking."
    ),
    "ambiguous_patient": (
        "You are a RETURNING patient. Your last name is Smith and your date "
        "of birth is May 12th 1985. Your first name is John. There is "
        "another patient with a very similar name and the same date of "
        "birth, so if the agent reads back a record to confirm it's you, "
        "listen carefully: confirm only if it says John Smith, and say no "
        "if it says Jonathan Smith. You're calling about ankle pain. "
        "Complete the booking if you can."
    ),
    # --- urgent ----------------------------------------------------------
    "urgent_injury": (
        "You are a NEW patient. You slipped on stairs about an hour ago and "
        "you think you may have broken your ankle -- it's very swollen and "
        "you can't put weight on it. Convey the urgency naturally. Name "
        "Casey Nolan, date of birth February 17th 1993, phone "
        "703-555-0169, ZIP 11530. Take the soonest appointment offered."
    ),
    # --- named doctor / office (call 3) -----------------------------------
    "named_doctor_right_office": (
        "You are a NEW patient with hip pain that's been bothering you for "
        "weeks. When asked if you have a specific doctor or office in mind, "
        "say you'd like to see Dr. Fracchia. Don't name an office unless "
        "asked. Name Terry Boland, date of birth June 9th 1975, phone "
        "703-555-0171, ZIP 11777. Complete the booking."
    ),
    "named_doctor_wrong_office": (
        "You are a NEW patient with hip pain. When asked if you have a "
        "specific doctor or office in mind, say you'd like to see Dr. "
        "Fracchia at the Southampton office. If the agent explains he's not "
        "at that office and offers you a choice between him at his real "
        "office or a different doctor who is at Southampton, pick the "
        "different doctor at Southampton. Name Casey Lindqvist, date of "
        "birth October 2nd 1980, phone 703-555-0172, ZIP 11968. Complete "
        "the booking."
    ),
    "named_doctor_age_restricted": (
        "You are a NEW patient calling about your 8 year old child's foot "
        "pain -- you are calling on the child's behalf but give the "
        "child's own date of birth when asked. When asked if you have a "
        "specific doctor in mind, say you'd like to see Dr. Yu. If the "
        "agent explains Dr. Yu doesn't see patients this age and offers a "
        "different doctor instead, accept that doctor. Name the patient "
        "Riley Yu-- no relation, phone 703-555-0173, ZIP 11777, date of "
        "birth should make them 8 years old today. Complete the booking."
    ),
    "gender_only_preference": (
        "You are a NEW patient with shoulder pain that's been bothering "
        "you for a couple of weeks. When asked if you have a specific "
        "doctor or office in mind, say you don't have anyone specific in "
        "mind, but you'd prefer to see a woman doctor if possible. Name "
        "Avery Lindqvist, date of birth March 3rd 1988, phone "
        "703-555-0174, ZIP 11747. Complete the booking."
    ),
    "named_doctor_and_gender_mismatch": (
        "You are a NEW patient with hip pain. When asked if you have a "
        "specific doctor or office in mind, say you'd like to see Dr. "
        "Fracchia, but mention you'd have preferred a woman doctor if one "
        "treated this. If the agent tells you they don't have a doctor of "
        "that gender who treats this but can still book you with Dr. "
        "Fracchia, accept that and complete the booking. Name Jordan "
        "Pruitt, date of birth July 19th 1985, phone 703-555-0175, ZIP "
        "11747."
    ),
    # --- imaging prerequisite (call 4) ------------------------------------
    "prerequisite_not_done": (
        "You are a RETURNING patient named James Whitfield, date of birth "
        "November 19th 1978, phone 516-555-0198, ZIP 11566. You're calling "
        "for a follow-up on your knee. If asked whether you've had an MRI "
        "done yet, say no, you haven't had a chance to get it done. Accept "
        "the first imaging appointment time offered and complete that "
        "booking."
    ),
    "prerequisite_done": (
        "You are a RETURNING patient named James Whitfield, date of birth "
        "November 19th 1978, phone 516-555-0198, ZIP 11566. You're calling "
        "for a follow-up on your knee. If asked whether you've had an MRI "
        "done yet, say yes, you had it done last week. Continue with the "
        "normal follow-up booking and complete it."
    ),
    # --- dead ends -------------------------------------------------------
    "no_match": (
        "You are a NEW patient calling this orthopedic practice about "
        "severe recurring migraines and headaches -- a neurological issue, "
        "not orthopedic. If the agent explains they don't treat that and "
        "redirects you elsewhere, accept that gracefully and end the call."
    ),
    # --- resilience ------------------------------------------------------
    "edge_cases": (
        "You are a NEW patient with knee pain after a soccer injury. Be a "
        "realistic, slightly difficult caller: hesitate and use filler "
        "words, give your date of birth in an unusual spoken format ('the "
        "ninth of September, nineteen ninety-two'), ask the agent to repeat "
        "itself once, and at one point start answering, pause mid-sentence, "
        "then correct yourself. Eventually settle down and complete the "
        "booking. Name Morgan Reid, phone 703-555-0177, ZIP 11753."
    ),
}


def place_call(scenario_name):
    payload = {
        "callAgentId": TEST_CALLER_AGENT_ID,
        "toNumber": MEDIPORTAL_NUMBER,
        "fromNumberId": TEST_CALLER_NUMBER_ID,
        "callAgentInput": {"scenario": SCENARIOS[scenario_name] + _PERSONA_SUFFIX},
        "timeoutMinutes": 6,
    }
    resp = requests.post(f"{VOGENT_API}/dials", headers=HEADERS, json=payload)
    resp.raise_for_status()
    # Vogent returns dialId (not id) -- the OUTBOUND leg only.
    return resp.json()["dialId"]


def dial_status(dial_id):
    resp = requests.get(f"{VOGENT_API}/dials/{dial_id}", headers=HEADERS)
    resp.raise_for_status()
    return resp.json().get("status")


def run_scenario(scenario_name, results, semaphore):
    with semaphore:
        try:
            dial_id = place_call(scenario_name)
        except Exception as exc:
            results[scenario_name] = {"error": str(exc)}
            print(f"[{scenario_name}] FAILED to place: {exc}", flush=True)
            return
        print(f"[{scenario_name}] started: {dial_id}", flush=True)

        deadline = time.time() + 420
        status = None
        while time.time() < deadline:
            time.sleep(8)
            try:
                status = dial_status(dial_id)
            except Exception:
                continue  # transient API error -- keep polling
            if status in TERMINAL_STATUSES:
                break
        results[scenario_name] = {"dial_id": dial_id, "status": status}
        print(f"[{scenario_name}] finished: {status} ({dial_id})", flush=True)


if __name__ == "__main__":
    names = sys.argv[1:] or list(SCENARIOS)
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        print(f"Unknown scenario(s): {unknown}\nChoices: {list(SCENARIOS)}")
        sys.exit(1)

    results = {}
    semaphore = threading.Semaphore(MAX_CONCURRENT_CALLS)
    threads = [
        threading.Thread(target=run_scenario, args=(name, results, semaphore))
        for name in names
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    print("\n=== Summary ===")
    for name in names:
        result = results.get(name, {})
        print(f"  {name}: {result.get('status') or result.get('error')} {result.get('dial_id', '')}")
    print(
        "\nVerify results DB-side (the ids above are the OUTBOUND leg):\n"
        "  see docs/testing/agent-call-test-checklist.md"
    )
