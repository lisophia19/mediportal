# Agent-to-Agent Call Test Checklist

Live end-to-end tests: a synthetic patient agent (`mediportal-test-caller`)
phones the real scheduling agent at **(703) 880-8652** and plays a scenario.
Everything under test is real — real call, real LLM matching, real Postgres
writes, real booking.

```bash
source .venv/bin/activate
python3 vogent/run_test_calls.py                    # all scenarios
python3 vogent/run_test_calls.py triage_hip no_match  # named subset
```

Each call burns real Vogent minutes. Run deliberately, not on every change.

---

## Reading the results (important)

The dial id printed by the runner is the **outbound** (test-caller) leg.
Vogent creates a **second, separate dial** for the inbound leg that
`mediportal-agent` actually answers, and *that* id is what lands in
`calls.vogent_call_id`. Looking up the printed id in our database will
always come back empty — that is not a bug.

Verify against the database instead (test calls arrive from
`+13322203540`):

```bash
ssh -i deploy/mediportal-key.pem ubuntu@98.81.86.177 \
  "cd /opt/mediportal && sudo docker compose exec -T postgres \
   psql -U mediportal -d mediportal -c \"
     SELECT id, vogent_call_id, status, raw_complaint, matched_term_id,
            patient_id, appointment_id, jsonb_array_length(transcript) AS turns
     FROM calls WHERE caller_phone = '+13322203540'
     ORDER BY id DESC LIMIT 10;\""
```

The dashboard at <https://98-81-86-177.nip.io> shows the same data per call,
including the Vogent call id for cross-referencing in Vogent's own UI.

---

## Run log — 2026-09-18

Bugs this pass found and fixed (all deployed, all with regression tests):

| # | Bug | Found by |
|---|-----|----------|
| 1 | Claude replied with only a thinking block, no text → 503 → call dropped with "something went wrong on our end". Latent in every matching call; the triage classifier's `max_tokens=10` made it near-certain there. | `other_doctor` |
| 2 | A name correction sent the flow back through intake, re-creating the patient each pass — three rows for one person — and replaying every "one moment…" line. | `other_doctor` |
| 3 | Clarification retry had nowhere to send a second `needs_clarification`, so "my shoulder has been aching" dead-ended. | `no_reason_given` |
| 4 | "Any other times?" ended the call even though the caller never declined anything. | `more_slots` |
| 5 | "A different doctor?" ended the call instead of falling back. | `other_doctor` |
| 6 | Transcripts never stored — the account webhook delivers `dial.inbound` but never `dial.transcript`. Now pulled from Vogent's API instead. | webhook logging |
| 7 | **`find_doctors` returned the doctor's *closest* office, but their open slots were at another one.** `get_availability` hard-filtered on it and reported `no_slots` for a doctor with 30 open slots — the caller was told nothing was available, then that no other doctor could help. `practice_id` is now a preference with fallback. | `triage_spine`, `returning_patient` |
| 8 | `no_other_doctor` re-read a slot list that was empty on the no-slots path — the agent stalled for minutes repeating "I'm not seeing the exact times in front of me". | `triage_spine` |
| 9 | That node's booking fallback used `{{node.present_slots.answer}}`, a node that never ran on that path; Vogent sent the literal template string, the API 500'd and the call died. Books from its own answer now. | `triage_spine` |
| 10 | `address_concern` routed back to `present_slots`, so one unresolved concern bounced between them — nine consecutive "I'll check that before we lock it in" turns, never booked. | `returning_patient` |
| 11 | Dead ends never recorded a terminal status, so a cleanly-declined call showed as `in_progress` until the sweep mislabelled it `abandoned`. | `no_match` |

Agent config changed: endpoint detection `SIMPLE` → `SEMANTIC` (was cutting
callers off mid-sentence), utterance detector aligned to the reference agent.

**All 12 scenarios passed as of 2026-09-19**, against the flow as it existed then.
Since then: Call 3 (named doctor/office), Call 4 (imaging prerequisite), the gender
filter, the office-only routing fix, and the fluid triage rewrite have all shipped
without a live re-run. `triage_hip`/`triage_spine`/`needs_clarification` specifically
need re-verification (their underlying mechanism changed); the rest are lower-risk but
unverified against current code. Re-run after any flow or routing change.

## Scenario checklist

### Core booking

- [x] **`standard_booking`** *(passed 2026-09-18)* — clear complaint (wrist fracture), new patient.
      *Expect:* matched confidently, new patient created, doctor + slot
      offered, booking confirmed. `status = scheduled`, `appointment_id` set.

### Triage (fluid, LLM-generated -- replaces the old hardcoded hip/spine-only mechanism)

`triage_hip`/`triage_spine` passed against the **old** hardcoded hip/spine special
case; the triage mechanism was rewritten (no hardcoded pairs, question generated live,
up to 3 rounds instead of 1 retry) so these need re-verification against the new code,
not just carried forward as passing.

- [ ] **`triage_hip`** — vague "pain", answers *stays in one spot*.
      *Expect:* a real discriminating question asked (not necessarily the old fixed
      wording), routed to a **hip** term.
- [ ] **`triage_spine`** — vague "pain", answers *shoots down my leg*.
      *Expect:* routed to a **spine** term.
- [ ] **`triage_hip_vs_knee`** — vague pain plausibly hip or knee, answers lean hip.
      *Expect:* the same mechanism handles a pair that was never hardcoded, no code
      path specific to this pair.
- [ ] **`triage_neck_vs_shoulder`** — vague pain plausibly neck or shoulder, answers lean neck.
      *Expect:* same as above.
- [ ] **`triage_exhausted_callback`** — genuinely unclear answers for all 3 rounds.
      *Expect:* an honest "someone from the office will call you back" after round 3,
      never a forced booking on a low-confidence guess.

### Clarification

- [ ] **`needs_clarification`** — "my knee hurts" has real signal words, so this now
      likely enters the fluid triage loop (needs_triage) rather than the old
      zero-signal-words `ask_clarify` path -- needs re-verification against the new
      code, not just carried forward as passing.
      *Expect:* one real discriminating question, then a confident match. Never a
      `no_match` decline.
- [x] **`no_reason_given`** *(passed 2026-09-18 after fix #3)* — "I'd like an appointment" with no reason.
      *Expect:* the agent asks what's bringing them in (deterministic, no
      LLM guess), then proceeds normally. Never a decline or hangup.
- [ ] **`no_reason_given_then_ambiguous`** — no signal on attempt 1, still vague on
      attempt 2 (a deliberate, documented scope limit -- see `match_issue_retry_fn`'s
      comment in vogent_flow.py: this compounding case falls to an honest callback
      rather than getting its own triage loop).
      *Expect:* either a real resolution, or an honest "someone will call you back" --
      never a forced guess, never a crash or unresolved template variable spoken aloud.

### Alternatives

- [x] **`more_slots`** *(passed 2026-09-19)* — caller asks for other times.
      *Expect:* additional times offered, or an honest "those are all the
      openings" — never a silent repeat of the same list, never a hangup.
- [x] **`other_doctor`** *(passed 2026-09-19 after fix #7)* — caller asks for a different doctor.
      *Expect:* the next eligible doctor by distance, or an honest "that's
      the only doctor who treats this" — never the same doctor again.

### Patient identity

- [x] **`returning_patient`** *(passed 2026-09-19 after fix #10; existing record reused, no duplicate)* — Maria Rodriguez, already in the database.
      *Expect:* existing record found, no duplicate patient created.
- [x] **`ambiguous_patient`** *(passed 2026-09-19; correctly chose John over Jonathan)* — John Smith vs. Jonathan Smith, same DOB.
      *Expect:* confirm-by-readback; caller rejects the wrong record and
      the right one is used (or an honest callback offer).

### Urgency

- [x] **`urgent_injury`** *(passed 2026-09-19 after fix #7)* — acute ankle fracture, one hour ago.
      *Expect:* urgent term matched, 3-day window honoured. The urgent
      caveat is spoken **only** when no slot exists inside that window.

### Dead ends

- [x] **`no_match`** *(passed 2026-09-19 after fix #11)* — migraines (genuinely not orthopedic).
      *Expect:* plain-language decline plus a redirect. `status = no_match`.

### Named doctor / office (call 3)

- [x] **`named_doctor_right_office`** *(passed 2026-09-23)* — names a doctor who is real, eligible, and at an office they actually practice at.
      *Expect:* pinned directly, no ranking prompt, no age/eligibility bypass.
- [x] **`named_doctor_wrong_office`** *(passed 2026-09-23)* — names a real doctor at an office they don't practice at.
      *Expect:* `needs_choice` -- offered that doctor's real office, or a different eligible doctor at the requested office.
- [x] **`named_doctor_age_restricted`** *(passed 2026-09-24, after fixing the scenario's own false premise -- Dr. Yu's real data has no age restriction; switched to Dr. Munn, who genuinely has one)* — names a doctor who doesn't treat patients this age.
      *Expect:* honest age-restricted explanation, falls back to an eligible doctor instead of silently booking or swapping without saying why.
- [ ] **`office_only_no_doctor_named`** — names an office but no doctor.
      *Expect:* pinned to an eligible doctor who's actually at that office (the office-only routing fix), not the generic by-distance pick.
- [ ] **`named_doctor_concern_retry`** — names a real doctor who genuinely doesn't treat this (wrong specialty, not an extraction miss), then explicitly pushes back once asking to check a different specialist.
      *Expect:* honest substitution reason spoken (not silently skipped, not leaked as literal instruction text), then the concern-retry path finds a real shoulder/upper-extremity specialist on the second attempt.

### Gender preference

- [x] **`gender_only_preference`** *(passed 2026-09-23)* — states a gender preference, no specific doctor or office.
      *Expect:* pinned to an eligible doctor of that gender, not the generic by-distance pick.
- [~] **`named_doctor_and_gender_mismatch`** *(ran 2026-09-23/24 -- surfaced and fixed 3 real bugs: a major anthropic SDK incompatibility causing intermittent extraction failures, the honest-explanation-never-spoken bug, and the missing concern-retry path. Final run: explanation spoke cleanly and the concern-retry path engaged correctly, but real speech-to-text mishearing of "Fracchia" as "Fratia"/"Frakia" -- both below the fuzzy-match floor -- prevented a clean resolution. Not a code bug; a real STT-accuracy limit. Re-run if worth confirming against a clearer-sounding name.)* — names a real, eligible doctor whose gender doesn't match the stated preference.
      *Expect:* still books the named doctor, but says honestly that the gender preference wasn't matched rather than silently ignoring it.

### Imaging prerequisite (call 4)

- [ ] **`prerequisite_not_done`** — returning patient (James Whitfield) with a pending MRI prerequisite, says it's not done.
      *Expect:* routed to imaging location + slot booking; `imaging_appointment_id` set, not `appointment_id`.
- [ ] **`prerequisite_done`** — same patient, says the MRI is done.
      *Expect:* prerequisite marked satisfied, flow continues into the normal follow-up doctor booking.

Only one seeded `patient_prerequisites` row exists (James Whitfield). These
two scenarios share it and are **not independently repeatable** — run
`prerequisite_not_done` first, then `prerequisite_done` clears it; running
either again after both finds no pending prerequisite (`status: "none"`),
which looks like a skip, not a bug. Re-seed (`seed_mediportal.py`) to reset.

### Resilience

- [x] **`edge_cases`** *(passed 2026-09-19)* — hesitation, odd DOB phrasing, mid-sentence
      self-correction, a request to repeat.
      *Expect:* no premature "I didn't catch that", no hangup, booking
      still completes.

---

## Cross-cutting checks (every call)

- [ ] Nothing internal is ever spoken aloud — no ids, statuses, raw
      timestamps, JSON, or unresolved `{{template}}` variables.
- [ ] The agent never hangs up on an answer it merely failed to parse.
- [ ] `calls.transcript` is populated — pulled from Vogent's API the first
      time a finished call is opened in the dashboard.
- [ ] Booked calls carry `appointment_id`; the dashboard shows the
      appointment rather than "No appointment booked".
- [ ] `patients.home_zip` is saved for new patients.

---

## Known platform-level quirks (not our bugs)

These recur in real transcripts, were investigated, and are not fixable
from our flow config or backend:

- Occasional stray nonsense word after a short acknowledgement (a TTS
  rendering artifact).
- Two consecutive silent node transitions can merge into one run-on spoken
  turn with no pause between sentences.
