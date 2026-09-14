# Spec — Ortho Routing Baseline Demo

Status: design only, not implemented.
Scope: the minimal baseline needed for **one working Vogent demo call**.

---

## 1. Project Goal

Demonstrate a single end-to-end phone call in which a patient who **does not know which
doctor they need** describes their complaint in plain language and is routed to the
correct orthopedic doctor, offered real availability, and booked.

The demo succeeds when all of the following are true in one call:

1. The caller says something unstructured ("my wrist has been killing me since I fell").
2. The agent identifies them as a new or returning patient using name + DOB.
3. The backend resolves the complaint to a clinical term, filters doctors by eligibility
   and patient age, ranks them by proximity to the caller's town/ZIP, and returns a
   ranked list.
4. The agent offers real open slots for the top-ranked doctor, books one, and reads the
   confirmation back.
5. The call transcript, outcome, patient, and appointment are visible in the dashboard.

### Critical design constraint

**No doctor-to-issue mapping lives in the Vogent flow.** The flow never contains a
doctor name, a specialty name, or an `if body_part == "wrist"` branch. It collects
caller input, calls Flask, and speaks back whatever Flask returns. All routing
knowledge lives in database tables and is resolved live, per call, by the backend.

The flow's only branching is on **generic response shapes** the backend returns:
`matched` / `needs_clarification` / `no_match`, and `slots_available` / `no_slots`.
Adding a doctor, changing a doctor's issue coverage, or changing an age restriction is
a database change with zero flow edits. This is the primary thing the demo proves.

### Non-goal: no transfer-instead-of-book shortcut

Any call that reaches a matched doctor with available slots **must complete booking
via `POST /appointments` (§5.6) before the call ends** — it must never substitute a
"someone will call you back" / warm-transfer response for an actual booking on the
happy path. The callback/transfer fallback is reserved **only** for genuine dead
ends: `no_match` (§5.1), `no_eligible_doctor` (§5.2), exhausted `no_slots` after
walking the full ranked doctor list (§5.5), `ambiguous_unresolved` patient lookup
(§5.3), and input-retry exhaustion (§6's Retry behavior). This also constrains the
future real-EHR scheduling provider (§5.5a): an EHR outage during booking should
degrade to the existing slot-race/retry UX (§5.6's `409` pattern), never bypass
booking entirely.

### Baseline scope boundary

- **Orthopedics only.** No PT, OT, Pain Management, or Physiatry routing.
- **One real practice group, fully seeded.** All 10 providers of **Long Island Bone
  and Joint (O&C)** are seeded from the actual spreadsheet — not a curated handful.
  Of those 10, 7 carry real term-eligibility data (routing-relevant); the other 3 (two
  NPs and one General MD with no `Terms Updated` column) are seeded for a realistic
  practice roster but are never returned by routing. See §8.1.
- **No deployment.** Local Docker Compose / local dev only. AWS + EC2 deferred.
- Everything else deferred is listed in `/Users/sophia19/Desktop/mediportal/notes-final-product.md`.

---

## 2. Data Sources

The routing data is grounded in the real provider spreadsheet already in the repo,
`Mediportal Information FINAL.xlsx` (Orlin & Cohen / Northwell data). Relevant sheets:

| Sheet | What it gives us |
|---|---|
| `Terms Updated` | One row per clinical term with `Term`, `Body Part`, `Category`, `Urgency`, then one column per doctor. The cell value is the age range that doctor accepts for that term (`0-100`, `10+`, `18+`, `Y` = no restriction, blank = not eligible). **This matrix is the routing table.** |
| `Provider Info` | First/last name, degree, Specialty 1/2, group, up to 6 practice locations. |
| `Practice Information` | Practice name, address, region, ZIP, phones, onsite X-ray/MRI/PT/OT flags. |

`seed_mediportal.py` in the repo root already parses all of this into `mediportal_*`
tables, including its name-matching and exclusion rules (a provider row whose name
doesn't exactly match a `Terms Updated` column header is silently excluded from
eligibility, not guessed at). **The baseline reuses that parsing logic and that
age-range convention**, but scopes the operational schema below to a single practice
group — **Long Island Bone and Joint (O&C)** — rather than the full multi-practice
directory. See §8.1 for the exact roster.

---

## 3. Data Model & How It Connects

This section is a plain-language walkthrough of the data, written so it can be checked
for correctness **before any code is written**. It explains where every table comes
from in the real spreadsheet and how the tables hang together.

### 3.1 The one-sentence version

A **term** is a thing a patient can have wrong with them; **term_eligibility** says
which **doctors** will treat that term and at what ages; doctors work out of
**practices**; each doctor-at-a-practice generates **appointment_slots**; a **patient**
(matched or created during a **call**) takes one slot, and that becomes an
**appointment**.

### 3.2 Entity-by-entity: where the data actually comes from

**`terms` ← the `Terms Updated` sheet, columns A–D**

Each row of `Terms Updated` is one clinical complaint the practice recognizes. Column A
is the term name (`Fracture-Wrist`), column B the body part (`Hand/Wrist`), column C the
category (`Fracture`), column D an urgency marker (`URGENT` or blank). One spreadsheet
row becomes one `terms` row. These four columns are the entire clinical vocabulary the
agent can route against.

We add one column the spreadsheet doesn't have: `patient_phrasing`, a hand-written list
of lay synonyms ("broke my wrist", "fell on my wrist"). Patients don't say
"Fracture-Wrist", and this is what lets the matcher bridge the gap.

**`doctors` ← the `Provider Info` sheet, columns A–F**

One row per provider: first name, last name, degree, Specialty 1, Specialty 2, group.
For the baseline we seed 3 of these rows.

Important: `specialty` is stored for **speech only** — so the agent can say "one of our
hand and wrist specialists." It is never used to decide routing. Routing goes strictly
through `term_eligibility`. This matters because the spreadsheet's specialty labels are
inconsistent ("Hand & Wrist" vs "Hand and Wrist" vs "Hand, Wrist & Upper Extremity")
and because a doctor's real coverage doesn't map cleanly onto their title anyway.

**`practices` ← the `Practice Information` sheet**

One row per physical office: name (`Merrick`), address, suite, ZIP, region, phones, and
the onsite-service flags (X-ray, MRI, PT, OT). The existing `seed_mediportal.py` already
handles a real quirk here — the same office appears on several rows with different suite
numbers, and those collapse into one practice keyed on (name, address), OR-ing the
onsite flags together. The baseline keeps that behavior.

We add `latitude` / `longitude`, populated from a small seeded ZIP-centroid table, purely
so we can rank practices by distance from the caller.

**`doctor_practices` ← the `Practice 1..6 - Name / Address` column pairs on `Provider Info`**

The provider sheet stores a doctor's offices as up to six repeated column pairs on the
same row. That's a many-to-many relationship flattened into columns. Unflattening it
gives one `doctor_practices` row per (doctor, practice). Practice 1 is flagged
`is_primary`. This is why a doctor can be offered at the Merrick office to one caller
and the Lynbrook office to another.

**`term_eligibility` ← the doctor columns of `Terms Updated` (column E onward)**

This is the most important table in the system and the least obvious one, because in the
spreadsheet it isn't a table at all — it's a **matrix**. `Terms Updated` has one row per
term and one *column per doctor*. The cell where a term row meets a doctor column holds
that doctor's age policy for that term:

| Cell value | Meaning |
|---|---|
| `0-100` | Treats this, any age |
| `10+`, `18+` | Treats this, at or above that age |
| `Y` or `None` | Treats this, no age restriction recorded |
| *(blank)* | **Does not treat this** |

Unpivoting that matrix — one row per non-blank cell — produces `term_eligibility`:
`(term_id, doctor_id, min_age, max_age)`. A 60-term × 90-doctor sheet with mostly blank
cells becomes a few thousand rows of real routing data.

Two consequences worth confirming:

1. **Absence is the signal.** There is no "does not accept" flag. A missing
   `term_eligibility` row *is* "this doctor does not treat this." A blank cell and an
   explicitly-refused case are the same thing in the source data, so they're the same
   thing here.
2. **Age lives on the relationship, not on the doctor.** Dr. Brown sees wrist fractures
   at any age (`0-100`) but elbow fractures only at 18+. The age restriction belongs to
   the *(doctor, term)* pair, which is why it's a column on the join table rather than a
   `min_age` field on `doctors`.

**`patients` ← not from the spreadsheet; created by the system**

Name, DOB, phone, home ZIP. DOB is not optional demographic trivia here — it's a routing
input, because eligibility is age-bounded. Home ZIP is a routing input too, because
practices are ranked by distance.

**`appointment_slots` ← generated, not sourced (see §8.2)**

We have no real schedule data. Slots are synthesized from a per-doctor weekly template
over a rolling 14-day window. Each slot points at both a `doctor_id` and a
`practice_id`, because a doctor is at different offices on different days and "when" and
"where" are answered together.

**`appointments` ← created during a call**

Joins a patient to a slot, and records the `term_id` that caused the booking. Storing
the term on the appointment means every booking carries the routing decision that
produced it — that's what makes the dashboard's routing-quality review possible.

**`calls` ← Vogent webhooks**

One row per phone call: transcript, outcome status, and the two fields that matter most
for review — `raw_complaint` (what the caller actually said) and `matched_term_id` (what
the system decided they meant).

### 3.3 How a single call walks the graph

This is the whole system in one path. A caller says "I fell and my wrist is killing me":

```
"my wrist is killing me"   (raw text)
        │
        ▼  LLM match over term / body_part / category / patient_phrasing
    terms  ──────────────► "Fracture-Wrist"  (term_id = 4)
        │
        │  term_eligibility.term_id = 4
        ▼
 term_eligibility  ─────►  rows for Dr. Brown (0–100), … 
        │
        │  filter: patient.date_of_birth → age 34, within 0–100 ✓
        ▼
     doctors  ──────────►  Dr. Bennett Brown
        │
        │  doctor_practices
        ▼
   practices  ────────►  Merrick / Lynbrook / Crossways
        │
        │  rank by haversine(practice.zip_centroid, patient.home_zip)
        ▼  Merrick, 4.2 mi
appointment_slots  ────►  open slots for (Brown, Merrick), next 14 days
        │
        │  patient picks one
        ▼
 appointments  ────────►  patient_id + slot_id + term_id + call_id
        │
        ▼
     calls  ───────────►  status = scheduled, transcript, raw_complaint, matched_term
```

Note what the Vogent flow sees at each arrow: nothing. It sends text, receives a
doctor's name and some times, and speaks them. Every arrow above is a SQL join executing
inside Flask.

### 3.4 Relationship summary

```
terms ──1:N──► term_eligibility ◄──N:1── doctors
                (min_age, max_age)          │
                                            │ N:M via doctor_practices
                                            ▼
                                        practices
                                            │
              doctors ──┐                   │
                        ├──1:N──► appointment_slots ◄──┘
                                      │ (status: open/held/booked)
                                      │ 1:1
                                      ▼
patients ──1:N──►               appointments ──N:1──► terms
                                      │                (why they came in)
                                      │ N:1
                                      ▼
                                    calls ──N:1──► patients
                                          ──N:1──► terms (matched_term_id)
```

Foreign keys, explicitly:

| From | To | Meaning |
|---|---|---|
| `term_eligibility.term_id` | `terms.id` | which issue |
| `term_eligibility.doctor_id` | `doctors.id` | who treats it, at what ages |
| `doctor_practices.doctor_id` | `doctors.id` | |
| `doctor_practices.practice_id` | `practices.id` | which offices they work from |
| `appointment_slots.doctor_id` | `doctors.id` | whose calendar |
| `appointment_slots.practice_id` | `practices.id` | which office that day |
| `appointments.slot_id` | `appointment_slots.id` | unique — one booking per slot |
| `appointments.patient_id` | `patients.id` | |
| `appointments.term_id` | `terms.id` | the routing decision, persisted |
| `appointments.call_id` | `calls.id` | which call produced it |
| `calls.patient_id` | `patients.id` | nullable — caller may never be identified |
| `calls.matched_term_id` | `terms.id` | nullable — may be a no-match call |
| `calls.appointment_id` | `appointments.id` | nullable |

### 3.5 What we are asserting about the data — please verify

If any of these is wrong, the schema is wrong:

1. A blank cell in the `Terms Updated` matrix means "this doctor does not treat this
   term," and is not merely missing/unknown data.
2. Age restrictions are per (doctor, term), not per doctor.
3. `Y` and `None` in a cell both mean "yes, no age restriction" — not "no."
4. Specialty labels on `Provider Info` are descriptive, and routing should never key off
   them.
5. A term with no eligible doctor is a legitimate outcome the agent must explain, not a
   data error.
6. The same office appearing on multiple `Practice Information` rows with different
   suites is one practice for routing purposes.
7. A doctor's Practice 1 is their primary/home office.

---

## 4. Database Schema

PostgreSQL. Tables below are the complete baseline set — nothing more.

### 4.1 Routing tables (the mapping that must not be hardcoded)

**`doctors`**

| Column | Type | Notes |
|---|---|---|
| `id` | PK | |
| `first_name`, `last_name` | text | |
| `degree` | text | `MD`, `DO` |
| `specialty` | text | `Sports Medicine`, `Hand & Wrist`, `Spine` — display only, never a routing key |
| `npi` | text, nullable | |
| `active` | bool, default true | Soft-disable a doctor without deleting eligibility rows |

`specialty` is deliberately **not** used for matching. Matching goes through
`term_eligibility` only. Specialty exists so the agent can say "Dr. Brown, one of our
hand and wrist specialists."

**`practices`**

| Column | Type | Notes |
|---|---|---|
| `id` | PK | |
| `name` | text | e.g. `Merrick` |
| `address`, `suite`, `zip`, `region` | text | |
| `latitude`, `longitude` | numeric, nullable | Seeded ZIP centroids; see §8.3 |
| `main_phone` | text | |

**`doctor_practices`** — many-to-many.

| Column | Type | Notes |
|---|---|---|
| `doctor_id` | FK → doctors | |
| `practice_id` | FK → practices | |
| `is_primary` | bool | The doctor's Practice 1 from the sheet |

Unique on (`doctor_id`, `practice_id`).

**`terms`** — the clinical vocabulary the agent routes against.

| Column | Type | Notes |
|---|---|---|
| `id` | PK | |
| `term` | text, unique | e.g. `Fracture-Wrist` |
| `body_part` | text | `Hand/Wrist`, `Back/Neck`, `Elbow`, `Shoulder/UE`, `Hip`, `Knee/LE`, `Foot and Ankle`, `Unspecified` |
| `category` | text | `pain`, `Fracture`, `Injury`, `Bursitis`, `Repetitive Stress Injury`, `Surgical Procedure`, … |
| `urgency` | text, nullable | `URGENT` or null |
| `patient_phrasing` | text[] | Lay synonyms used to improve LLM matching, e.g. `{"broke my wrist","wrist fracture","snapped my wrist"}` |
| `default_appointment_type` | text | `new_patient_consult`, `follow_up`, `urgent` — drives slot duration |

`patient_phrasing` is a baseline addition, not in the spreadsheet. It is seeded by hand
for the ~12 demo terms and materially improves match quality.

**`term_eligibility`** — the join that answers "which doctor treats this issue".

| Column | Type | Notes |
|---|---|---|
| `id` | PK | |
| `term_id` | FK → terms | |
| `doctor_id` | FK → doctors | |
| `min_age` | int | From the sheet cell; `Y`/`None` → 1 |
| `max_age` | int | `Y`/`None`/`N+` → 100 |

Unique on (`term_id`, `doctor_id`). Absence of a row means **not eligible** — there is
no "does not accept" flag, absence is the signal. This mirrors the blank cells in the
spreadsheet exactly.

**`directory_entries`** ← the `Directory` sheet (`Contact`, `Location`, `Group`,
`Cell Number`, `Desk Number`, `Email`). This is the practice's internal call-routing
directory — e.g. `"Spine"` → `844-887-7463`, `"Ortho Pediatrics"` → `516-210-8400`. It
exists independently of `terms`/`doctors` and is what a real front desk reaches for
when a caller's issue is a dead end for this practice.

| Column | Type | Notes |
|---|---|---|
| `id` | PK | |
| `contact` | text | The department/call-reason label, e.g. `"Spine"`, `"Ortho Pediatrics"` |
| `location` | text | `"General"` or a named hospital/clinic |
| `group_name` | text | Practice group this entry belongs to |
| `desk_number` | text | The number to give the caller |
| `email` | text, nullable | |

`seed_mediportal.py` already parses this sheet into `MediportalDirectoryEntry`; the
baseline reuses that parsing as-is (see §5.9 for how it's used at a dead end).

### 4.2 Patient and scheduling tables

**`patients`**

| Column | Type | Notes |
|---|---|---|
| `id` | PK | |
| `first_name`, `last_name` | text | |
| `date_of_birth` | date | Required — drives age eligibility |
| `phone` | text, nullable | Only collected when needed to disambiguate, or on create |
| `home_zip` | text, nullable | Captured for proximity ranking |
| `created_at` | timestamptz | |

Index on (`lower(last_name)`, `date_of_birth`) — the primary lookup path.

**`appointment_slots`** — synthetic availability, see §8.2.

| Column | Type | Notes |
|---|---|---|
| `id` | PK | |
| `doctor_id` | FK → doctors | |
| `practice_id` | FK → practices | Doctor may be at different sites on different days |
| `start_time`, `end_time` | timestamptz | |
| `status` | enum | `open`, `held`, `booked` |
| `hold_expires_at` | timestamptz, nullable | Set when `held`; see §7.5 |

Index on (`doctor_id`, `status`, `start_time`). Partial unique index preventing two
`booked` slots for the same doctor at the same `start_time`.

**`appointments`**

| Column | Type | Notes |
|---|---|---|
| `id` | PK | |
| `patient_id` | FK → patients | |
| `slot_id` | FK → appointment_slots, unique | One appointment per slot |
| `term_id` | FK → terms | Why they're coming in — the routing decision, persisted |
| `appointment_type` | text | Copied from the term at booking time |
| `status` | enum | `scheduled`, `cancelled` |
| `call_id` | FK → calls, nullable | Which call produced this booking |
| `created_at` | timestamptz | |

### 4.3 Call capture tables

**`calls`**

| Column | Type | Notes |
|---|---|---|
| `id` | PK | |
| `vogent_call_id` | text, unique | Idempotency key for the webhook |
| `caller_phone` | text | Captured silently from Vogent's `{{toNumber}}` (confirmed ANI — the number the caller is calling from), never asked. Distinct from `patients.phone` (the number on file), which may differ — see §5.3's disambiguation logic. |
| `started_at`, `ended_at` | timestamptz | |
| `status` | enum | `in_progress`, `scheduled`, `no_match`, `no_slots`, `abandoned`, `failed` |
| `patient_id` | FK → patients, nullable | Null if never identified |
| `appointment_id` | FK → appointments, nullable | |
| `transcript` | jsonb | Array of `{speaker, text, timestamp}` |
| `matched_term_id` | FK → terms, nullable | What the routing resolved to |
| `raw_complaint` | text, nullable | What the caller actually said, verbatim |

`raw_complaint` alongside `matched_term_id` is the single most useful pair for
reviewing routing quality — it lets staff see where the LLM match was wrong.

**`users`** — dashboard auth only.

| Column | Type |
|---|---|
| `id` | PK |
| `email` | text, unique |
| `password_hash` | text |
| `created_at` | timestamptz |

---

## 5. Flask API

Base path `/api/v1`. Two audiences: the **Vogent flow** (unauthenticated within the
demo, gated by a shared secret header `X-Agent-Key`) and the **dashboard** (JWT).

### 5.1 `POST /routing/match-issue` — free text → ranked candidate terms

The LLM-assisted matching step. Vogent sends verbatim caller speech; Flask does all
interpretation.

Request:
```json
{ "complaint_text": "my wrist has been killing me since I fell off my bike last week",
  "call_id": "vg_abc123" }
```

Behavior:
1. Cheap pre-filter — retrieve candidate terms by embedding/keyword overlap over
   `term`, `body_part`, `category`, `patient_phrasing`. Keeps the LLM prompt small.
2. LLM classification call — the candidate terms plus the complaint text; the model
   returns ranked `term_id`s with confidence and a one-line rationale. The model is
   given a strict instruction to return `no_match` rather than guess.
3. Confidence thresholds decide the response `status`.

Response — three shapes, and the flow branches only on `status`:

```json
{ "status": "matched",
  "term": { "id": 4, "term": "Fracture-Wrist", "body_part": "Hand/Wrist",
            "category": "Fracture", "urgency": null,
            "appointment_type": "new_patient_consult" },
  "confidence": 0.91,
  "confirm_prompt": "It sounds like this is a possible wrist fracture — is that right?" }
```

```json
{ "status": "needs_clarification",
  "candidates": [ { "id": 4, "term": "Fracture-Wrist", "label": "a possible broken wrist" },
                  { "id": 7, "term": "Pain-Wrist",     "label": "ongoing wrist pain" } ],
  "clarify_prompt": "Is this from a recent injury or fall, or is it pain that's built up over time?" }
```

```json
{ "status": "no_match",
  "spoken_response": "I'm sorry — we're an orthopedic practice, so that's not something our doctors here treat. You'd want to start with your primary care doctor for that." }
```

`confirm_prompt`, `clarify_prompt`, and `spoken_response` are **generated by the
backend and spoken verbatim by the agent**. This keeps the plain-language no-match
fallback (a CLAUDE.md requirement) out of the flow and in the layer that knows why the
match failed.

### 5.2 `POST /routing/find-doctors` — term + patient → ranked doctors

Request:
```json
{ "term_id": 4, "date_of_birth": "1991-04-02", "zip": "11563", "call_id": "vg_abc123" }
```

Backend logic, in order:
1. `term_eligibility` join `doctors` where `term_id = ?` and `doctors.active`.
2. Age filter — compute age from DOB, keep rows where `min_age <= age <= max_age`.
3. Join `doctor_practices` → `practices`; compute distance from the caller's ZIP
   centroid to each practice (haversine).
4. Rank by distance ascending. Each result is a **(doctor, practice)** pair, because
   "which office" is part of the answer.

Response:
```json
{ "status": "matched",
  "term_urgency": "URGENT",
  "doctors": [
    { "doctor_id": 3, "name": "Dr. Bennett Brown", "specialty": "Hand & Wrist",
      "practice": { "id": 2, "name": "Merrick", "address": "1728 Sunrise Highway",
                    "zip": "11566" },
      "distance_miles": 4.2,
      "spoken_label": "Dr. Bennett Brown, one of our hand and wrist specialists, at our Merrick office about four miles from you" } ] }
```

`term_urgency` (the `Urgency` column from `Terms Updated`, `"URGENT"` or `null`) is
echoed here so the flow can pass it straight through to `GET /availability` (§5.5)
without a second lookup. Urgency affects **which slots get offered**, not doctor
ranking — an urgent case still goes to the closest eligible doctor, just with a
tighter scheduling window.

Two failure shapes, again distinguished so the agent can explain plainly. Both attempt
a **directory redirect** (§5.9) before falling back to a bare apology, so a dead end
still ends with a useful number rather than a shrug:

```json
{ "status": "no_eligible_doctor",
  "reason": "age_restricted",
  "directory_redirect": { "contact": "Ortho Pediatrics", "desk_number": "516-210-8400" },
  "spoken_response": "Our elbow fracture specialist only sees patients eighteen and over. Let me give you the number for our Ortho Pediatrics line — that's five one six, two one zero, eight four hundred — they'll get your child seen." }
```

```json
{ "status": "no_eligible_doctor",
  "reason": "not_covered",
  "directory_redirect": { "contact": "Spine", "desk_number": "844-887-7463" },
  "spoken_response": "We don't have a doctor here who treats that. Let me give you the number for our Spine line — that's eight four four, eight eight seven, seven four six three." }
```

`directory_redirect` is `null` when §5.9's lookup finds no matching entry, in which
case `spoken_response` falls back to a generic apology + this practice's own main
line instead of naming a specific department.

Separating `age_restricted` from `not_covered` matters: they are different real-world
conversations, and the demo seed deliberately contains one of each (§8.1).

### 5.3 `POST /patients/lookup` — last name + DOB, layered disambiguation on ties

**Why not first name or phone as hard filters:** first-name spelling coming through
voice transcription is unreliable ("Jon" vs "John," homophones, etc.), and the phone
number on file may not be the number the patient happens to be calling from. Neither
is trustworthy enough to gate a match. DOB is spoken as digits and transcribes far
more reliably, so it stays an exact, hard filter alongside last name.

Request:
```json
{ "last_name": "Rodriguez", "date_of_birth": "1991-04-02",
  "first_name": "Maria", "call_id": "vg_abc123" }
```

`first_name` is collected (the greeting already asks for it) and passed along, but
only ever used as a soft signal, never a filter. `call_id` lets the backend pull the
caller's ANI (`calls.caller_phone`, §4.3/§5.7) server-side rather than having the
flow re-supply a phone value — one source of truth for that number.

**Matching logic:**
1. **Hard filter:** `lower(last_name) = lower(:last_name)` (case-insensitive,
   trimmed) AND `date_of_birth = :date_of_birth` (exact).
2. **0 rows → `not_found`** — sends the flow to patient creation (§5.4).
3. **1 row → `found`** — no first-name check gates this; last name + DOB alone is
   treated as sufficient (collisions are rare enough that the seed data deliberately
   includes exactly one, §8.4, to exercise the path below).
4. **≥2 rows → layered disambiguation**, each layer tried only if the previous one
   doesn't resolve it:
   - **(a) Caller-ID match, silent:** compare `calls.caller_phone` (the number this
     call came in on, captured automatically from Vogent's `{{toNumber}}`) against
     each candidate's `patients.phone` on file. Exactly one match → resolve to that
     patient with no further caller interaction.
   - **(b) First-name soft score:** if (a) didn't resolve it, fuzzy-match (e.g.
     Jaro-Winkler) the caller-supplied `first_name` against each remaining
     candidate's first name. One candidate scoring clearly above the rest → resolve
     to it.
   - **(c) Confirm-by-readback:** if still tied, return `status: "confirm"` with a
     `confirm_prompt` built from the top-scoring candidate's full name, e.g. *"I have
     a record for a Jonathan Smith born on that date — does that sound right?"* The
     flow speaks it; a "yes" resolves to `candidate_patient_id`, a "no" re-calls this
     endpoint with that candidate excluded, advancing to the next one (rare — at most
     one extra round in practice with ≤2–3 ties).
   - **(d) `ambiguous_unresolved`:** if candidates are exhausted with no resolution,
     return this status with a `spoken_response` offering a callback. This is a
     genuine dead end (§1's non-goal, below) — no doctor/slot process has started
     yet, so ending the call here is not a booking shortcut.

**Response shapes:**
```json
{ "status": "found", "patient": { "id": 12, "first_name": "Maria", "last_name": "Rodriguez",
                                  "date_of_birth": "1991-04-02", "home_zip": "11563" } }
```
```json
{ "status": "confirm", "candidate_patient_id": 12,
  "confirm_prompt": "I have a record for a Jonathan Smith born on that date — does that sound right?" }
```
```json
{ "status": "ambiguous_unresolved",
  "spoken_response": "I'm having trouble finding the exact record — let me have someone from our office call you back to get you booked." }
```
```json
{ "status": "not_found" }
```

### 5.4 `POST /patients` — create new patient

Request: `first_name`, `last_name`, `date_of_birth`, `phone`, `home_zip`.
Response: the created patient. Per CLAUDE.md, name + DOB + phone is sufficient;
`home_zip` is baseline-added because proximity ranking needs it.

### 5.5a Scheduling provider — the swap point for a real EHR later

§5.5 and §5.6 are both **thin wrappers** over a `SchedulingProvider` interface, not
direct database access. This is the boundary the user specifically asked for: "mock
connect it to the SQL database for now... soon we'll swap that out for a real API
call into their EHR" — swapping later should touch this one interface, never the
Flask routes or the Vogent flow.

```python
class SchedulingProvider(ABC):
    def get_available_slots(self, doctor_id, practice_id, appointment_type,
                             date_from, date_to, limit) -> list[SlotResult]: ...

    def book_slot(self, slot_id, patient_id, term_id, call_id) -> BookingResult:
        """Raises SlotUnavailableError(alternates=[...]) if the slot is no longer open."""
        ...
```

- **Baseline implementation — `PostgresSchedulingProvider`.** Queries/writes the real
  seeded `appointment_slots`/`appointments` tables directly (the exact SQL described
  in §5.5/§5.6 below). This *is* the "mock" in the sense that it isn't hitting a real
  EHR yet — but it's a fully real, working booking against a real Postgres database,
  not an in-memory fake. Lives at `backend/app/providers/postgres_scheduling.py`.
- **Urgency windowing stays in the route layer, not the provider** — §5.5's
  3-day/14-day widen-and-retry logic calls `get_available_slots` up to twice with
  different date ranges; the provider itself doesn't know what "urgent" means, which
  keeps its interface narrow and reusable by a future EHR-backed implementation.
- **Swap mechanism:** a `SCHEDULING_PROVIDER` env var (`postgres` default) selects
  the implementation in `create_app`; routes obtain it via Flask's app context
  (`current_app.extensions["scheduling_provider"]`), never by importing the concrete
  class directly. A future `RealEHRSchedulingProvider` — translating these same two
  methods into whatever the real EHR's API expects — drops in via that one config
  flag, with zero changes to `app/blueprints/availability.py`,
  `app/blueprints/appointments.py`, or the Vogent flow.
- **EHR outages, when that day comes, should degrade like a slot race** (§5.6's
  `409`/re-offer pattern), never bypass booking with a transfer — see §1's non-goal.

### 5.5 `GET /availability` — open slots

Query params: `doctor_id` (required), `practice_id` (optional), `appointment_type`
(optional, filters by required duration), `urgency` (optional, pass `term_urgency`
from §5.2 straight through), `from` / `to` (defaults below), `limit` (default 3).

**Urgent scheduling window:** when `urgency == "URGENT"`, the backend defaults the
search window to **now → now + 3 days** instead of the normal now → now + 14 days —
this is the "try to get an urgent patient in within 3 days" rule. If that narrower
window returns no slots for this doctor, the backend automatically **widens to the
full 14-day window** in the same call (never a second round trip) and marks the
result so the agent can say so honestly rather than silently offering a slot outside
the target window:

```json
{ "status": "slots_available",
  "urgent_window_met": true,
  "slots": [ { "slot_id": 811, "start_time": "2026-09-17T10:30:00-04:00",
               "duration_minutes": 30, "practice_name": "Port Jefferson",
               "spoken_label": "Wednesday the seventeenth at ten thirty in the morning" } ] }
```

```json
{ "status": "slots_available",
  "urgent_window_met": false,
  "slots": [ { "slot_id": 902, "start_time": "2026-09-24T09:00:00-04:00",
               "duration_minutes": 30, "practice_name": "Riverhead",
               "spoken_label": "the following Wednesday the twenty-fourth at nine in the morning" } ] }
```

`urgent_window_met` is only present when `urgency == "URGENT"`; it's omitted for
routine terms. On `false`, the flow speaks an honest caveat before offering the slot
(e.g. "I couldn't find anything in the next few days, but the soonest opening I have
is…") rather than presenting a late slot as if it met the urgent target — the
caveat text itself is backend-provided (§5.1-style `spoken_response` pattern), not
flow-authored.

`{ "status": "no_slots" }` (no slots at all, urgent or otherwise) moves the flow to the
next ranked doctor from §5.2 without re-running the match. If the ranked list is
exhausted, the call ends with status `no_slots` and a backend-provided apology +
callback offer.

`spoken_label` is pre-formatted server-side so the agent never has to format a date.

### 5.6 `POST /appointments` — book

Request:
```json
{ "slot_id": 811, "patient_id": 12, "term_id": 4, "call_id": "vg_abc123" }
```

Booked inside a transaction with `SELECT ... FOR UPDATE` on the slot row. If the slot
is no longer `open`, returns `409` with `{"status":"slot_taken", "alternate_slots":[…]}`
so the agent can immediately re-offer instead of failing the call.

Success response includes the full confirmation payload the agent reads back:
```json
{ "status": "scheduled", "appointment_id": 55,
  "confirmation": { "doctor": "Dr. Bennett Brown", "practice": "Merrick",
                    "address": "1728 Sunrise Highway, Merrick",
                    "when": "Wednesday, September 17th at 10:30 AM",
                    "appointment_type": "New patient consultation" } }
```

`arrive_minutes_early` was dropped from this baseline (not needed for the demo call) --
revisit if the flow ever needs to tell callers when to arrive.

### 5.7 Call capture

- `POST /calls` — Vogent calls this at call start with `vogent_call_id` and
  `caller_phone` (from `{{toNumber}}`, never asked of the caller); creates a row with
  status `in_progress`. Returns the internal `call_id` threaded through every
  subsequent call above, including §5.3's lookup, which pulls `caller_phone` back
  out via this `call_id` rather than having the flow re-supply it.
- `PATCH /calls/{vogent_call_id}` — incremental updates (matched term, raw complaint,
  patient_id) as the flow progresses, so a dropped call still has partial data.
- `POST /calls/{vogent_call_id}/complete` — Vogent's end-of-call webhook: full
  transcript, `ended_at`, final status. Idempotent on `vogent_call_id`.

A call left `in_progress` with no update for 15 minutes is swept to `abandoned`. Baseline
implementation: lazily, from the dashboard's read routes (`GET /calls`, `GET /calls/{id}`)
rather than a real background scheduler -- cheap at this data volume, and means a caller
hanging up mid-call is reflected correctly the next time anyone opens the dashboard.

### 5.8 Dashboard endpoints

- `POST /auth/login` → JWT. `POST /auth/logout`.
- `GET /calls?status=&q=&page=` → paginated call list with patient name, status,
  duration, matched term.
- `GET /calls/{id}` → full detail: transcript, patient, matched term + raw complaint,
  appointment with doctor/practice/time.

### 5.9 Directory redirect — dead-end fallback

Not a Vogent-facing endpoint on its own; it's internal logic §5.2 calls when it's
about to return `age_restricted` or `not_covered`, so the flow never has to make a
separate call for it.

**Lookup:** match the term's `category` and/or `body_part` (from `terms`, §4.1)
against `directory_entries.contact` — e.g. a `Spine` category term with no eligible
LIBJ doctor matches the `"Spine"` directory row; a term whose `body_part` implies
pediatrics matches `"Ortho Pediatrics"`. Baseline matching is a small **hand-maintained
lookup table** (category/body_part → directory contact string) rather than fuzzy
text matching — the `Directory` sheet's ~40 rows are department names, not clinical
vocabulary, so there's no natural string overlap with `terms` to match on
automatically; unlike §5.1's LLM match, this mapping is short and stable enough to
hand-curate and review directly rather than infer.

**No match found:** `directory_redirect: null`; the practice's own main line
(`practices.main_phone` for the nearest ranked practice) is used as the fallback
instead, so the caller is never told to just "call back."

**This is a real, decided design choice worth flagging:** unlike doctor/term routing,
which must never be hardcoded, this category→contact table *is* a small hand-written
mapping. That's intentional — it's not clinical routing logic (it doesn't decide
who treats the patient), it's a lookup between two vocabularies (spreadsheet term
categories vs. spreadsheet department names) that don't share a key in the source
data. If a future data source adds a shared key (e.g. the `Directory` sheet gains a
`category` column), this becomes data-driven too.

---

## 6. Vogent Flow

Every step below is either **speech** or **an HTTP call**. No step contains routing
knowledge.

| # | Step | Says / Collects | Backend call |
|---|---|---|---|
| 1 | Greet | "Thanks for calling — I can help get you scheduled. Can I get your first and last name?" — `caller_phone` is captured silently from `{{toNumber}}`, never asked | `POST /calls` |
| 2 | Identify | First name, last name, DOB | `POST /patients/lookup` |
| 2a | Disambiguate | Only on `confirm` — speaks `confirm_prompt` (e.g. "I have a record for a Jonathan Smith born on that date — does that sound right?"); "yes" resolves, "no" re-calls excluding that candidate. On `ambiguous_unresolved`, speaks `spoken_response`, ends call with a callback offer — a genuine dead end, not a booking shortcut (see §1 non-goal). | `POST /patients/lookup` (backend handles caller-ID match and first-name scoring internally before ever returning `confirm`) |
| 2b | Create | Only if `not_found` — collects phone, confirms spelling | `POST /patients` |
| 3 | Location | "And what town are you in, or your ZIP code?" | — (held for step 5) |
| 4 | Complaint | "What's going on that brings you in?" — captures **verbatim** | `POST /routing/match-issue` |
| 4a | Clarify | On `needs_clarification`, speaks `clarify_prompt`, captures answer, re-calls step 4 with the combined text. **Max 2 clarification rounds**, then falls through to `no_match`. | `POST /routing/match-issue` |
| 4b | Confirm | On `matched`, speaks `confirm_prompt`. If caller says no, re-collects and re-calls step 4. | — |
| 4c | Dead end | On `no_match`, speaks `spoken_response`, offers to leave a message, ends call with status `no_match` | `POST /calls/.../complete` |
| 5 | Route | Silent — no speech | `POST /routing/find-doctors` |
| 5a | Dead end | On `no_eligible_doctor`, speaks `spoken_response`, ends call | `POST /calls/.../complete` |
| 6 | Offer doctor | Speaks `doctors[i].spoken_label` for the top-ranked pair | — |
| 7 | Offer slots | Passes `term_urgency` from step 6 through as `urgency`; reads up to 3 `spoken_label`s, prefixed with the backend's caveat text if `urgent_window_met: false`; caller picks | `GET /availability` |
| 7a | Next doctor | On `no_slots`, "Dr. X is booked out — let me check our next closest option," then `i += 1`, back to step 6. List exhausted → end with status `no_slots`. | — |
| 8 | Book | | `POST /appointments` |
| 8a | Race | On `409 slot_taken`, "That one just got taken — I also have…", re-offer `alternate_slots` | — |
| 9 | Confirm back | Reads the full `confirmation` object aloud, asks caller to confirm | — |
| 10 | Close | Ends call | `POST /calls/.../complete` with status `scheduled` |

### Ordering note

Name/DOB (step 2) is collected **before** the complaint (step 4) because DOB is
required for age-based eligibility filtering in step 5. ZIP (step 3) is collected next
because proximity ranking also happens in step 5. By the time the complaint is
captured, the backend has everything it needs to route in a single call.

### Retry behavior

Each collection step allows 2 re-asks on unrecognized input, then escalates to
"Let me have someone from our office call you back" and ends with status `failed`. The
re-ask copy lives in the flow (it's conversational, not clinical); all clinical copy
comes from the backend.

---

## 7. Edge Cases

| Case | Handling |
|---|---|
| Complaint matches nothing orthopedic | `match-issue` → `no_match`; backend supplies a plain-language front-desk response, not an error. Call logged as `no_match` with `raw_complaint` for review. |
| Complaint is ambiguous between two terms | `needs_clarification` with a backend-authored disambiguating question; max 2 rounds then `no_match`. |
| Term is covered, but patient's age is outside every eligible doctor's range | `no_eligible_doctor` / `age_restricted` — explicitly distinguished from "not covered" so the spoken explanation is honest. |
| Term has no eligible doctor at all among seeded doctors | `no_eligible_doctor` / `not_covered`. |
| All ranked doctors fully booked | Flow walks the ranked list; exhaustion ends the call as `no_slots` with a callback offer. |
| Slot taken between offer and booking | `SELECT … FOR UPDATE`; `409 slot_taken` returns alternates inline so the call continues. |
| Two patients share last name + DOB | §5.3's layered disambiguation: silent caller-ID match → first-name soft score → confirm-by-readback → `ambiguous_unresolved`. Still unresolved after all four → patient creation is **not** done; the agent offers a callback (safer than merging records or guessing). |
| Returning patient's first name is misheard/misspelled | Not a hard filter (§5.3) — last name + DOB alone resolves a unique match regardless. Only affects the soft-scoring tie-break step when there are multiple last-name+DOB matches. |
| Returning patient calls from a different number than the one on file | Expected and handled: caller-ID match (§5.3 step a) simply fails to disambiguate and falls through to first-name scoring / confirm-by-readback instead — never blocks the lookup. |
| Caller hangs up mid-flow | Partial data already persisted via `PATCH /calls`. Sweeper marks `abandoned` after 15 min. |
| Term marked `URGENT` | Availability search defaults to a **3-day window** instead of 14 (§5.5). Urgency is also logged and surfaced in the dashboard. If no slot exists within 3 days, the backend widens to 14 days and flags `urgent_window_met: false` so the agent states the delay honestly rather than silently offering a late slot. |
| LLM match service unavailable | `match-issue` returns `503`; flow speaks a graceful "let me have someone call you right back" and ends with status `failed`. Never silently guesses. |

---

## 8. Seed Data

### 8.1 Doctors and terms — Long Island Bone and Joint (O&C), all 10 providers

All values below are taken directly from `Mediportal Information FINAL.xlsx`, filtered
to the `Group` column `"Long Island Bone and Joint (O&C)"`. This replaces the earlier
"pick 3 doctors" plan — the practice's real roster is seeded in full.

> **Note on earlier examples:** sections 3 and 6 above use "Dr. Bennett Brown" /
> "Alpert" / "Faust" as illustrative, pedagogical doctor names to explain the schema
> and API shapes before this practice was chosen. They are **not** part of the real
> seed data — the actual roster is below. The mechanism they illustrate (age lives on
> the relationship, generic response shapes, etc.) is unchanged.

**All 10 providers, seeded into `providers` and `provider_practices`:**

| Doctor | Degree | Specialty | Practice location(s) | Routing-eligible? |
|---|---|---|---|---|
| Stephen Densen | DPM | Foot & Ankle | Southampton, Riverhead, Smithtown | Yes — 49 terms |
| Michael Fracchia | MD | Joint Reconstruction / Hip & Knee | Port Jefferson, Riverhead, Melville | Yes — 126 terms |
| June Halsey | MD | General / Pediatrics | Southampton, Riverhead | Yes — 106 terms |
| John Hubbell | MD | Sports Medicine | Southampton | Yes — 78 terms |
| Brian McGinley | MD | Joint Reconstruction / Hip & Knee | Port Jefferson | Yes — 78 terms |
| Rasel Rana | DO | Spine | Port Jefferson, Riverhead | Yes — 203 terms |
| John Yu | MD | Foot & Ankle | Port Jefferson, Riverhead | Yes — 139 terms |
| Eugene Arena | NP | *(none listed)* | Port Jefferson | **No** — no `Terms Updated` column |
| Henry Marano | MD | General | Southampton, Riverhead | **No** — no `Terms Updated` column |
| Kenneth Nissen | NP | *(none listed)* | Port Jefferson | **No** — no `Terms Updated` column |

The "No" row for Arena, Marano, and Nissen isn't a judgment call — it falls straight
out of the data: their names have no matching column in the `Terms Updated` sheet, so
`seed_mediportal.py`'s existing exclusion rule (exact name match against that header
row) naturally leaves them with zero `term_eligibility` rows. They're seeded as real
providers (so the directory/dashboard reflects the actual practice) but the routing
endpoint can never return them. This is the "route only to ortho-relevant ones"
decision, implemented as a data fact rather than a hardcoded filter.

**Terms and eligibility:** the 7 routing-eligible doctors above collectively cover
**247 distinct clinical terms** (out of 296 total terms in the sheet) — the full
intersection of `Terms Updated` rows where at least one of those 7 doctors has a
non-blank cell. That is the real `term_eligibility` seed set; it is not hand-picked
and won't be reproduced in full here (`seed_mediportal.py` generates it directly from
the sheet). A few notable, real properties of this set worth knowing before
implementation:

- **June Halsey (General/Pediatrics)** is eligible for a broad slice of standard ortho
  terms (e.g. `Arthritis-Elbow`, `Bursitis-Knee`, `Congenital Deformity-Hand`) at age
  value `Y` (no restriction) — same as her ortho-specialist colleagues. The sheet does
  not encode a pediatric-only ceiling for her; she is a real, unfiltered routing option
  for any age, not just children. Flagged because it's a real property of the data
  that's easy to assume incorrectly.
- **All 7 doctors' age values in this practice are `Y` or `0–100`** — i.e., **no doctor
  in this specific practice has an actual age floor/ceiling in the real data.** The
  earlier plan's `age_restricted` demo (Fracture-Elbow at 18+) doesn't naturally occur
  within Long Island Bone and Joint's real roster. The `age_restricted` response shape
  and backend logic still exist and still matter for the full 20+-doctor directory —
  this practice's seed data just doesn't happen to exercise that branch. Documented
  here as an honest seed-data limitation rather than papered over; call it out if the
  demo needs that branch shown and we can revisit (e.g. seed one doctor outside age
  norms, clearly labeled as a deliberate seed choice rather than sourced from the row).
- **The `not_covered` demo is still real and available.** 49 terms in the sheet have
  **zero** eligible doctors across all 7 (e.g. `Animal Bite of Hand`,
  `Dupuytren's Contracture`, `Arthroscopy-Wrist`, `Botox`, `EMG- Upper Extremity`) —
  these are real hand/wrist/upper-extremity procedures this specific practice's
  ortho-relevant doctors don't perform. Any of these makes a clean `not_covered` demo
  call.

Each term also gets 3–6 hand-written `patient_phrasing` synonyms for the LLM-match
step, e.g. `Fracture-Wrist` → `{"broke my wrist","think my wrist is broken","snapped
my wrist","fell on my wrist"}`. With 247 terms in scope, synonym authoring is limited
at first to whichever terms are used in the demo's rehearsed call(s) plus a broader
pass generated (not hand-written) for the rest — full manual coverage of 247 terms is
out of scope for the baseline.

### 8.2 Availability — synthetic, by explicit assumption

**We do not have real physician schedule data.** All availability is generated. This is
an explicit baseline assumption, not an oversight.

Generation rules:

- Each of the 7 routing-eligible doctors gets a **recurring weekly template** across
  their real seeded practice locations — e.g. Yu: Mon/Wed/Fri at Port Jefferson,
  Tue/Thu at Riverhead; Rana: Mon/Tue/Thu 8:30–16:30 at Port Jefferson. Templates
  differ per doctor so "this doctor is booked, try the next one" is reachable.
- The template is expanded into concrete slots across a **rolling 14-day window**,
  skipping weekends and a lunch hour.
- Slot duration by appointment type (CLAUDE.md leaves this to us — documented here as
  the assumption): `new_patient_consult` **40 min**, `follow_up` **20 min**,
  `urgent` **30 min**. Slots are generated on a 20-minute grid; a 40-minute
  appointment consumes two adjacent grid slots.
- **Pre-booked baseline load**: ~60% of generated slots are marked `booked` against
  synthetic existing patients, so availability lookups return a realistic sparse set
  rather than a wide-open calendar.
- One doctor is seeded at **~100% booked for the next 7 days** so the "preferred doctor
  has no openings, move to the next option" branch is demonstrable on demand.
- A seed script regenerates the rolling window so the demo never goes stale.

### 8.3 Geography

ZIP-to-coordinate resolution uses a **small seeded lookup table of ZIP centroids** for
the practice's real locations — Southampton, Riverhead, Smithtown, Port Jefferson, and
Melville — plus a spread of Long Island caller ZIPs, no external geocoding API.
Distances are straight-line haversine miles, not driving distance; noted as a
final-product gap.

### 8.4 Patients and calls

- ~20 synthetic existing patients with varied DOBs, including:
  - one pair sharing **the same last name and DOB, with different first names and
    phone numbers** (exercises §5.3's full disambiguation chain: caller-ID match
    resolves it when the seeded test call's `caller_phone` matches one of the two;
    a separate test case using a third, non-matching `caller_phone` exercises the
    first-name-score and confirm-by-readback fallbacks instead).
  - Per §8.1, this practice's real data has no age-restricted doctor, so no patient is
    seeded specifically to trigger `age_restricted` — that branch is exercised in code
    but not reachable via this seed set without a deliberate, clearly-labeled synthetic
    tweak (open question, not decided here).
- 3–5 synthetic completed calls with transcripts across different statuses
  (`scheduled`, `no_match`, `abandoned`) so the dashboard has content before the first
  live demo call.

---

## 9. Dashboard (React)

Minimal, functional over polished. Three screens.

**Login** — email + password, posts to `/auth/login`, stores JWT. No signup; users are
seeded.

**Call list** (`/calls`) — table, newest first:

| Column | Source |
|---|---|
| Time | `calls.started_at` |
| Caller / Patient | `patients.first_name last_name`, or the raw caller phone if unidentified |
| Complaint (raw) | `calls.raw_complaint`, truncated |
| Routed to | `terms.term` |
| Status | colored badge — `scheduled`, `no_match`, `no_slots`, `abandoned`, `failed` |
| Appointment | doctor + date, or `—` |

Filter by status; free-text search over patient name and `raw_complaint`.

**Call detail** (`/calls/:id`) — three stacked sections:

1. **Outcome** — status badge, duration, caller phone.
2. **Routing** — what the caller said (`raw_complaint`) next to what it matched
   (`terms.term`, body part, category), then the doctor + practice chosen and, if
   booked, the appointment date/time/type. This side-by-side is the review artifact
   that makes routing quality auditable.
3. **Transcript** — full turn-by-turn from `calls.transcript`.

No editing, no rebooking from the dashboard in baseline.

---

## 10. Explicitly Out of Scope for This Baseline

Deferred items are tracked in
`/Users/sophia19/Desktop/mediportal/notes-final-product.md`. At a glance:
PT/OT/Pain Management routing, AWS + Docker deployment, reschedule and cancellation,
human escalation/warm transfer, insurance and referral checks, full multi-practice
provider directory load, driving-distance geocoding, SMS/email confirmations,
dashboard polish and analytics, HIPAA/PHI handling. (Urgent-window scheduling and
dead-end desk-number redirect, §5.5 and §5.9, are now in scope — no longer deferred.)

---

## 11. Definition of Done

The baseline is done when a single Vogent call can be placed that:

1. Identifies a returning patient by name + DOB, disambiguating by phone if needed.
2. Takes a free-text complaint and resolves it to a term via the backend LLM match.
3. Returns a doctor the flow has never heard of, filtered by age and ranked by ZIP.
4. Offers real seeded slots, books one, and reads back the confirmation.
5. Appears correctly in the dashboard with transcript, routing decision, and
   appointment.

And, as the real proof of the design constraint: **adding a fourth doctor with new
issue coverage changes the call's behavior with zero edits to the Vogent flow.**
