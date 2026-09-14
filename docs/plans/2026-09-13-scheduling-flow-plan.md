# Implementation Plan — Ortho Routing Baseline Demo

Source spec: `docs/design/spec-ortho-baseline-demo.md` (read that first — this plan
assumes its data model, API contracts, and flow are already agreed).
Deferred items: `notes-final-product.md`.

Goal: one working Vogent demo call that routes a Long Island Bone and Joint patient
to the right doctor from live data, books a real slot, and shows up in the dashboard.
No deployment (local only).

Phases are executed one at a time, each through a full implement → test → simplify →
test again → review loop, with human approval before moving to the next phase (see
`.claude/agents/team-lead.md`).

---

## Open decisions

- **Age-restricted demo (still open).** Spec §8.1/§8.4 note that this practice's real
  data has no age-restricted doctor, so the `age_restricted` branch has no
  naturally-occurring demo trigger. Decide before Phase 0's seed step: leave it
  untriggered in the demo, or deliberately seed one doctor with a synthetic age
  floor (clearly labeled as a seed choice, not sourced data). Everything else in
  this plan proceeds either way.
- **DB target (resolved).** Local Postgres via `docker-compose`, connection driven
  entirely by `DATABASE_URL`. No AWS RDS instance exists anywhere in this repo today
  (confirmed by exploration); pointing at real RDS later is a pure env-var change
  with zero code change, since the `PostgresSchedulingProvider` (Phase 1) talks to
  Postgres via SQLAlchemy regardless of where it's hosted.

---

## Phase 0 — Foundation (scaffolding + schema + seed data)

Backend/frontend skeletons, the full database schema, and real seed data — nothing
here depends on API logic, so it's one phase.

**Scaffolding**
- [ ] `backend/` — Flask app factory (`create_app`), config for local Postgres,
      `app/extensions.py` (db), `app/models.py`, blueprint structure matching §5's
      endpoint groups (`routing`, `patients`, `availability`, `appointments`, `calls`,
      `auth`, `dashboard`), plus a new `app/providers/` module for the
      `SchedulingProvider` interface (§5.5a — built out in Phase 1, but the module
      and interface stub belong here alongside the rest of the app skeleton).
- [ ] `frontend/` — React app (Vite or CRA — pick one, minimal tooling), routing for
      login / call list / call detail (§9).
- [ ] `docker-compose.yml` — Postgres service + backend + frontend for local dev.
      (Deployment to AWS is explicitly out of scope — this compose file is dev-only.)
- [ ] `.env.example` — `DATABASE_URL`, `JWT_SECRET`, an LLM API key (§5.1 matching),
      and `SCHEDULING_PROVIDER` (default `postgres`).

**Schema (§4)**
- [ ] Models/migrations for all tables in dependency order: `doctors`, `practices`,
      `doctor_practices`, `terms`, `term_eligibility`, `directory_entries` (§4.1) →
      `patients`, `appointment_slots`, `appointments` (§4.2) → `calls`, `users` (§4.3).
- [ ] Enforce constraints called out in the spec: unique (`term_id`, `doctor_id`) on
      `term_eligibility`; unique `slot_id` on `appointments`; absence-means-ineligible
      (no boolean flag) on `term_eligibility`; case-insensitive index on
      (`last_name`, `date_of_birth`) on `patients` for §5.3's lookup.
- [ ] `flask db upgrade` runs clean against a fresh Postgres instance.

**Seed data (§8)**
- [ ] Adapt `seed_mediportal.py`'s existing parsing (`Provider Info`, `Practice
      Information`, `Terms Updated`, `Directory`) so it writes into the operational
      schema above, filtered to `Group == "Long Island Bone and Joint (O&C)"` (§8.1)
      — not the full multi-practice directory.
- [ ] Verify the resulting counts match §8.1: 10 providers seeded, 7 with
      `term_eligibility` rows, 247 distinct terms covered, 49 terms with zero eligible
      doctors (spot-check a few against the sheet directly).
- [ ] Seed `directory_entries` from the `Directory` sheet in full (not LIBJ-filtered —
      the redirect target is often a different department/line, e.g. "Spine" or
      "Ortho Pediatrics"), plus the hand-maintained category/body_part →
      `directory_entries.contact` lookup table for §5.9.
- [ ] Synthetic availability generator (§8.2): per-doctor weekly templates, expanded
      over a rolling 14-day window, ~60% pre-booked, one doctor near-100%-booked.
      Runnable repeatedly (regenerates the rolling window) without duplicating rows.
- [ ] Seed ZIP centroid lookup table (§8.3) covering the 5 practice-location ZIPs plus
      a spread of Long Island caller ZIPs.
- [ ] Seed ~20 synthetic patients (§8.4), including one last-name+DOB collision pair
      with different first names/phone numbers (to exercise §5.3's full
      disambiguation chain), and 3–5 synthetic completed calls across different
      statuses, plus one seeded dashboard login user.
- [ ] `patient_phrasing` synonyms (§8.1): author by hand for whichever terms the
      rehearsed demo call(s) will use; generate (not hand-write) the rest.

## Phase 1 — Backend API (spec §5)

Build and test in call order, since each depends on the previous:

- [ ] §5.3/§5.4 `POST /patients/lookup`, `POST /patients` — last_name+DOB hard
      filter; on ≥2 matches, the layered disambiguation chain (silent caller-ID
      match → first-name soft score → confirm-by-readback → `ambiguous_unresolved`);
      create-on-`not_found`.
- [ ] §5.1 `POST /routing/match-issue` — LLM-assisted free text → ranked terms,
      `matched` / `needs_clarification` / `no_match`.
- [ ] §5.2 `POST /routing/find-doctors` — eligibility join + age filter + proximity
      ranking; `term_urgency` echoed in the response; `age_restricted` /
      `not_covered` failure shapes, each attempting §5.9's directory redirect before
      falling back to the practice's main line.
- [ ] §5.9 directory redirect lookup (internal, called from §5.2 — not a standalone
      route).
- [ ] §5.5a `SchedulingProvider` interface + `PostgresSchedulingProvider` — the
      real implementation behind §5.5/§5.6, selected via `SCHEDULING_PROVIDER`.
      Both routes below are thin wrappers over this: they own request parsing,
      response-shape formatting, and (for §5.5) the urgency-window widen/retry
      logic; the provider owns the actual slot query and the booking transaction.
- [ ] §5.5 `GET /availability` — urgency-aware window (3 days for `URGENT`, widen to
      14 with `urgent_window_met: false` if empty); `no_slots` triggers next-doctor
      fallback client-side (flow), not server-side.
- [ ] §5.6 `POST /appointments` — book via the provider; `409 slot_taken` +
      `alternate_slots` on race.
- [ ] §5.7 call capture — `POST /calls` (start, captures `caller_phone` from
      `{{toNumber}}`), a webhook/update endpoint for transcript + status,
      `POST /calls/.../complete`.
- [ ] §5.8 dashboard endpoints — auth, call list, call detail.
- [ ] Integration test per edge case in §7: age-restricted, not-covered w/ redirect,
      urgent window met/unmet, slot race, last-name+DOB collision (all four
      disambiguation layers), LLM-match service down, clarification-round
      exhaustion, fully-booked doctor list. Explicitly verify the §1 non-goal: no
      code path substitutes a transfer/callback for a real booking on the happy path.

## Phase 2 — Vogent Flow + Dashboard

Both are thin consumers of the Phase 1 API and don't depend on each other — build
together (in parallel across two implementers, if the team-lead workflow's cap of
two is used for this phase).

**Vogent flow (§6)**
- [ ] Build the flow's steps 1–9 exactly per the §6 table — each step is either
      speech or one HTTP call, never both, and never contains a doctor/specialty
      name or age number.
- [ ] Wire the dead-end branches (4c `no_match`, 5a `no_eligible_doctor`, 2a
      `ambiguous_unresolved`, 7a exhausted `no_slots`) to end the call with the
      correct status and speak the backend-provided text verbatim — confirm none of
      these are reachable from the happy path once a doctor+slot is matched (§1
      non-goal).
- [ ] Confirm step 7's urgency pass-through and caveat-prefix behavior against §5.5,
      and step 1's silent `caller_phone` capture from `{{toNumber}}`.
- [ ] Rehearse the full happy path scripted end-to-end against seeded data.

**Dashboard (§9)**
- [ ] Login (JWT) — seeded user only, no signup.
- [ ] Call list — table per §9, newest first.
- [ ] Call detail — transcript, patient, matched term + raw complaint, appointment
      details when scheduled.
- [ ] Confirm the 3–5 seeded synthetic calls render correctly before the first live
      demo call, so the dashboard isn't empty during a demo.

## Phase 3 — Demo Verification

- [ ] Run one real phone call end-to-end: unidentified caller → routed via live data
      → real slot offered → booked → confirmed → visible in dashboard.
- [ ] Run at least one dead-end call (a term from the 49 uncovered) to confirm the
      directory redirect is spoken correctly.
- [ ] Run one urgent-term call to confirm the 3-day-window behavior and, if no slot
      exists within 3 days, the honest caveat.
- [ ] Run one returning-patient call where the caller's number doesn't match the
      number on file, to confirm disambiguation still resolves via first-name score
      or confirm-by-readback rather than getting stuck.
- [ ] Confirm no doctor name, specialty, or age number appears anywhere in the flow
      config itself — only in database rows. This is the thing the whole exercise
      proves; check it explicitly, don't just assume it from the design.

---

Everything not listed above (PT/OT, deployment, reschedule/cancel, human escalation,
etc.) is intentionally out of scope here — see `notes-final-product.md`.
