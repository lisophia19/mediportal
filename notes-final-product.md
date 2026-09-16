# Notes — Final Product (Deferred from the Baseline Demo)

**This is not the spec.** The baseline spec is
`docs/design/spec-ortho-baseline-demo.md`, which covers only the minimum needed for one
working Vogent demo call (ortho routing, all 10 Long Island Bone and Joint providers,
247 terms, no deployment).

This file is a running parking lot for everything the *eventual* product needs that was
deliberately cut from the baseline. It was stubbed from the obvious deferrals — add to
it freely as more requirements surface. Nothing here is committed to or scheduled.

---

## Clinical scope

- **PT / OT routing.** Physical and occupational therapy are in the source data
  (`PT Onsite` / `OT Onsite` flags on `Practice Information`) and in the real practice's
  workflow, but are out of the ortho baseline entirely. Needs its own routing rules —
  PT is usually referral-driven and script-bound, not "pick a therapist by issue."
- **Pain Management, Physiatry, Joint Reconstruction** and the other non-ortho
  specialties in `Provider Info`.
- **Full provider directory.** Baseline seeds all 10 Long Island Bone and Joint
  providers; the spreadsheet has ~90 across other practice groups. Loading all of them
  means confronting the data-quality issues `seed_mediportal.py` already logs as
  warnings (name mismatches between sheets, unrecognized age values, practices that
  don't resolve).
- **Urgent scheduling beyond a 3-day window.** Baseline enforces "try to get an urgent
  patient in within 3 days" (widening to 14 with an honest caveat if nothing's sooner —
  spec §5.5). Real behavior for the case that caveat represents — an urgent complaint
  with nothing available even in the wider window — needs an actual resolution, not
  just an apology: either rearrange/bump the doctor's schedule to fit the patient in, or
  redirect the caller to the practice's actual scheduling desk (phone number) so a human
  can do what the automated system can't. Baseline doesn't do either — it just offers
  the best available late slot with a spoken caveat. Also still deferred: routing
  URGENT terms to an urgent-care site with different hours (the `urgent_hours` /
  `evening_hours` / `weekend_hours` fields on `Practice Information` exist for this but
  are unused).
- **Appointment type doesn't distinguish "existing patient, new issue" from "brand-new
  patient" or "follow-up."** `appointment_type` (and its duration) currently comes
  purely from the matched term (`urgent` or `new_patient_consult` — `follow_up` is
  defined but never actually assigned by the seed data). A returning patient calling
  about a non-urgent issue that isn't a tracked follow-up gets the same
  `new_patient_consult` duration/label as a brand-new patient, which isn't right.
  Deliberately left as-is for the baseline (decided during Phase 1 review); real product
  needs a duration/label decision that accounts for patient-is-new vs. returning, most
  naturally driven by whether `/patients/lookup` returned `found` vs. created a new
  record.
- **Onsite-service matching.** A patient needing an X-ray or MRI at the visit should be
  routed to a practice with `has_xray` / `has_mri`. Data is already modeled; matching
  logic is not.
- **Clinical restrictions / "For Consideration" fields.** `Provider Info` carries
  free-text restriction notes per provider that the baseline ignores entirely.
- **Triage beyond hip vs. spine.** Baseline's `needs_triage` branch (§5.1) only fires
  for the Hip/Back-Neck ambiguity. Other body-part pairs have the same real referred-
  pain overlap and deserve the same treatment: **hip vs. knee** (hip arthritis
  classically refers pain into the knee) and **neck vs. shoulder** (cervical
  radiculopathy presents as shoulder/arm pain) are the clinically common ones;
  **elbow vs. hand/wrist** (nerve entrapment referral, e.g. cubital tunnel) is real but
  less common as a phone-intake ambiguity. Today these fall through to the generic
  `needs_clarification`/alternates flow instead of a dedicated screening question.

## Call handling

- **Reschedule and cancellation** over the phone. Needs appointment lookup by patient,
  plus a cancellation policy and slot release.
- **Human escalation / warm transfer.** Baseline gives callers a real desk number to
  call back for the routing dead ends that have one (`not_covered`/`age_restricted` via
  the §5.9 directory redirect), but never actually transfers the live call — the caller
  has to hang up and redial. Genuine warm transfer (keeping the caller on the line) is
  deferred, as is any redirect/desk-number fallback for the urgent-no-slots case noted
  above under Clinical scope.
- **Callback queue.** Every baseline dead end (`no_match`, `no_slots`, `failed`) should
  create a real work item for staff, not just a logged call status.
- **Multi-appointment calls.** Caller booking for a spouse or child, or booking two
  issues in one call.
- **Interruption / barge-in handling**, hold music, and call-quality edge cases.
- **Spanish and other languages.**
- **Voicemail / after-hours behavior.**

## Patient identity and records

- **Better returning-patient matching.** Baseline is exact DOB + case-insensitive name.
  Real product needs fuzzy/phonetic name matching, nickname handling ("Bob" → "Robert"),
  and a duplicate-record merge path.
- **Identity verification** beyond name/DOB/phone, if PHI policy requires it.
- **Insurance capture and eligibility checks.** Which plans each doctor and location
  accept is a hard routing constraint in reality and is entirely absent from baseline.
- **Referral requirements** — some plans and some visit types require one.
- **EHR / practice-management integration.** Baseline owns its own patient and
  appointment tables. Production would need to read and write a real system of record
  rather than being one.

## Scheduling

- **Real availability.** Baseline generates synthetic slots from per-doctor weekly
  templates because no schedule data exists yet. Replacing this with a real feed (or
  live EHR calendar reads) changes the availability endpoint's implementation but not
  its contract.
- **Appointment duration rules.** Baseline assumes 40 / 20 / 30 min for new patient /
  follow-up / urgent. The practice should confirm real durations, likely per specialty.
- **Block scheduling, templates, and doctor time-off.**
- **Overbooking and double-book policies** that real front desks actually use.
- **Waitlist** for "call me if something opens up."
- **Confirmations and reminders** — SMS/email confirmation after the call, reminder
  before the visit, and no-show handling.

## Geography

- **Driving distance, not straight-line.** Baseline uses haversine over seeded ZIP
  centroids. Real ranking should use a routing/geocoding service and account for traffic
  and transit realities on Long Island.
- **Caller-stated preferences** — "I can only do the Garden City office", "somewhere
  near my job in Queens".
- **Full ZIP coverage** rather than the small seeded Long Island / Queens centroid table.

## Dashboard

- Baseline is three screens (login, call list, call detail), deliberately unpolished.
  Deferred:
- **Routing-quality review workflow** — let staff flag a bad match and correct the term,
  building a labeled dataset for improving the matcher.
- **Analytics** — match rate, no-match rate, booking conversion, most common complaints,
  which doctors get routed to and how often.
- **Search and filtering** beyond status and free text.
- **Rebooking / editing appointments** from the dashboard.
- **Roles and permissions.** Baseline has a single flat user table; real product needs
  at least admin vs. reviewer.
- **Real auth** — password reset, session management, SSO, MFA.
- **Provider / term admin UI.** The whole point of the baseline design is that routing
  lives in data, not in the flow — the payoff is a screen where staff edit doctor issue
  coverage and age ranges without a developer. That screen is deferred but is the
  natural next thing to build.

## Platform and operations

- **Deployment.** Docker + AWS EC2 per CLAUDE.md — one container for backend, one for
  frontend, reachable, not on a laptop. Explicitly out of scope for the baseline pass.
- **CI/CD**, migrations strategy, environment separation.
- **Monitoring and alerting**, especially on the LLM matching step (latency directly
  becomes dead air on a phone call).
- **LLM cost, latency, and caching** for `match-issue`. Baseline calls it once or twice
  per call with no budget.
- **Matcher evaluation harness.** A fixed set of complaint phrasings with expected
  terms, run on every change, so matching quality is measurable rather than vibes.
- **Fallback when the LLM is down** — baseline fails the call gracefully; production
  wants a degraded deterministic matcher.
- **Load and concurrency** — the baseline slot-booking lock is correct but untested
  under real contention.

## Compliance

- **HIPAA / PHI handling.** Transcripts contain names, DOBs, and clinical complaints.
  Needs encryption at rest and in transit, access logging, retention policy, and a BAA
  with every vendor in the path — including Vogent and whatever LLM provider backs the
  matching step.
- **Call recording consent** and state-specific disclosure requirements.
- **Audit trail** on who viewed which call and when.
- **Data retention and deletion** policy.

---

## Open questions to put to the practice

- Real appointment durations per visit type and specialty?
- Which insurance plans gate which doctors/locations?
- What actually happens today when a caller's issue isn't treated — where do they get
  sent?
- Should URGENT complaints ever be scheduled by an agent at all, or always transferred?
- Who owns the schedule of record, and can we read/write it?
