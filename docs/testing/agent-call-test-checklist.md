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

## Scenario checklist

### Core booking

- [ ] **`standard_booking`** — clear complaint (wrist fracture), new patient.
      *Expect:* matched confidently, new patient created, doctor + slot
      offered, booking confirmed. `status = scheduled`, `appointment_id` set.

### Triage (hip vs. spine)

- [ ] **`triage_hip`** — vague "pain", answers *stays in one spot*.
      *Expect:* screening question asked, routed to a **hip** specialist.
- [ ] **`triage_spine`** — vague "pain", answers *shoots down my leg*.
      *Expect:* screening question asked, routed to a **spine** specialist.

### Clarification

- [ ] **`needs_clarification`** — "my knee hurts", then names an injury.
      *Expect:* one clarifying question, then a confident match. Never a
      `no_match` decline.
- [ ] **`no_reason_given`** — "I'd like an appointment" with no reason.
      *Expect:* the agent asks what's bringing them in (deterministic, no
      LLM guess), then proceeds normally. Never a decline or hangup.

### Alternatives

- [ ] **`more_slots`** — caller asks for other times.
      *Expect:* additional times offered, or an honest "those are all the
      openings" — never a silent repeat of the same list, never a hangup.
- [ ] **`other_doctor`** — caller asks for a different doctor.
      *Expect:* the next eligible doctor by distance, or an honest "that's
      the only doctor who treats this" — never the same doctor again.

### Patient identity

- [ ] **`returning_patient`** — Maria Rodriguez, already in the database.
      *Expect:* existing record found, no duplicate patient created.
- [ ] **`ambiguous_patient`** — John Smith vs. Jonathan Smith, same DOB.
      *Expect:* confirm-by-readback; caller rejects the wrong record and
      the right one is used (or an honest callback offer).

### Urgency

- [ ] **`urgent_injury`** — acute ankle fracture, one hour ago.
      *Expect:* urgent term matched, 3-day window honoured. The urgent
      caveat is spoken **only** when no slot exists inside that window.

### Dead ends

- [ ] **`no_match`** — migraines (genuinely not orthopedic).
      *Expect:* plain-language decline plus a redirect. `status = no_match`.

### Resilience

- [ ] **`edge_cases`** — hesitation, odd DOB phrasing, mid-sentence
      self-correction, a request to repeat.
      *Expect:* no premature "I didn't catch that", no hangup, booking
      still completes.

---

## Cross-cutting checks (every call)

- [ ] Nothing internal is ever spoken aloud — no ids, statuses, raw
      timestamps, JSON, or unresolved `{{template}}` variables.
- [ ] The agent never hangs up on an answer it merely failed to parse.
- [ ] `calls.transcript` is populated (via the account-level Vogent webhook
      → `POST /webhooks/vogent`).
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
