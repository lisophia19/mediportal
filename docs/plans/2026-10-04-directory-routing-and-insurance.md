# Plan — Caller-requested routing & mocked insurance prompting

Status: plan, not yet implemented. Decisions below were confirmed with the user
on 2026-10-04; supersedes the open questions in
`docs/design/brainstorm-directory-routing-and-insurance.md`.

## Decisions locked in

1. **Escape-hatch scope: narrow.** Reachable from a few specific choke points,
   not every question node in the flow.
2. **Escape-hatch targeting: auto-map from complaint.** Caller doesn't have to
   name a department — it reuses the same term/category → `DirectoryRedirectRule`
   lookup `_no_eligible_doctor_response` already uses for the automatic case.
3. **Insurance: ask only, no gating.** The agent asks the right follow-up
   question at the right time; booking is never blocked on the answer.
4. **Insurance data: mocked for now.** Plausible invented carrier/plan rules,
   not real Northwell payer policy — swap in real data later if the practice
   provides it.

---

## Part 1 — Caller-requested directory routing

### Where the escape hatch lives

Two choke points, both already-existing nodes a caller is naturally at when
they'd plausibly ask this:

- **Right after `match_issue_fn` resolves** (the complaint is captured, a
  `matched_term_id` exists) — a caller who's just described their issue and
  says "can you just transfer me to the spine line" has a real term to map
  from.
- **At any existing dead end** (`dead_end_no_eligible_doctor` and friends) —
  the caller may ask after hearing "we don't treat that," on top of the
  automatic offer already spoken there.

Not added at every question node (ask_first_name, ask_dob, etc.) — a caller
asking to be transferred before stating any complaint has nothing to map
from, and per the scope decision this is intentionally not the broad/every-node
version.

### Backend changes

- **No new table needed.** Reuses `_directory_redirect_for(term)` exactly as
  the automatic case does — same `DirectoryRedirectRule` → `DirectoryEntry`
  lookup, same `term.category`/`term.body_part` matching.
- **New endpoint**: `POST /routing/request-transfer` (or similar) — takes
  `call_id`, looks up the call's `matched_term_id`, resolves the redirect the
  same way, returns `{"status": "redirect", "spoken_response": "..."}` or
  `{"status": "no_redirect", "spoken_response": "..."}` (honest fallback to
  main office number) when no term is on the call yet or no rule matches.

### Flow changes (`vogent/vogent_flow.py`)

- A caller-intent check added to the 1-2 choke-point nodes' `answer_guidelines`
  (e.g. on the node right after `match_issue_fn`, or as an additional branch
  on existing dead-end nodes): classify whether the caller is asking to be
  transferred, same pattern as `offer_alternates`'s CONCERN/ALT1/ALT2
  sentinel-string classification already used elsewhere in this flow.
- New function node calling the new endpoint, new terminal node that speaks
  the result and hangs up (mirrors `dead_end_no_eligible_doctor`'s shape).

### Test-mode number overlay

- Add an env-gated override: when `DIRECTORY_TEST_MODE=true` (or reuse an
  existing flag), `DirectoryEntry.desk_number` values get swapped for a small
  hardcoded mapping of throwaway numbers (the McDonald's-location approach)
  at seed time or at read time in `_directory_redirect_for` — seed-time is
  simpler and doesn't touch request-path code at all, so prefer that unless
  it turns out swapping needs to be toggled without reseeding.
- Start with the 2-3 contacts most likely to be exercised in test calls
  (Spine, Pain Management) — expand only if testing surfaces a need for more.

### Tests

- Backend: new endpoint — matched term with a redirect rule returns the
  right contact/number; no term on call falls back honestly; no matching
  rule falls back to main office number.
- Flow: structural validation (no dupes/dangling/unreachable) after the new
  nodes are added, same as every prior flow change this session.
- Live: one manual test-call scenario — caller states a complaint, then asks
  to be transferred instead of continuing booking; confirm the agent routes
  using the already-captured term, not a generic/wrong line.

---

## Part 2 — Mocked insurance-aware prompting

### Data model (new tables)

- **`insurance_carriers`**: `name`, `accepted` (bool), `ever_requires_referral`
  (bool — lets the flow skip the question entirely for carriers that never
  need one, respecting the existing "no unscripted questions" guardrail).
- **`insurance_referral_rules`**: `carrier_id`, `plan_tier` (nullable — null
  means "applies to all tiers of this carrier"), `requires_referral` (bool).
  Small, hand-maintained table, same spirit as `DirectoryRedirectRule`.
- Mocked seed data for 2-3 carriers (Oscar, QualCare, plus one "doesn't
  require anything" carrier to prove the skip-path works) — plausible, not
  sourced from the real practice, flagged as such in the seed file's own
  comment per this project's established "mock vs. real" documentation habit
  (see e.g. the McDonald's test-number precedent).

### Backend changes

- New endpoint: `POST /patients/check-insurance` (or under `/routing/`) —
  takes `carrier_name` (free text from the caller) + optional `plan_tier`,
  fuzzy-matches to `insurance_carriers` (same `_best_fuzzy_match` helper
  already used for doctor/practice name matching), returns whether a
  referral question is needed and, if so, the question text.
- **No gating logic** — this endpoint only ever informs what the agent says
  next; nothing it returns can block `POST /appointments`.

### Flow changes

- New question node after patient identification (or wherever makes sense
  relative to the preference/availability steps — needs a decision on exact
  placement once this part is actually being built) asking for insurance
  carrier.
- A function node calling the new endpoint, with a conditional follow-up
  question node (referral) that's only reached when the backend says one is
  needed — same `equal()`-on-backend-field pattern used throughout this
  session's fixes, not a freeform "ask only if relevant" instruction (that
  exact pattern already caused two live bugs this session).

### Tests

- Backend: carrier requiring a referral triggers the question; a carrier
  that never does skips it; unrecognized carrier name degrades honestly
  (same fallback shape as doctor-name extraction failures).
- Flow: structural validation.
- Live: one manual test-call scenario per carrier type (requires referral /
  doesn't).

---

## Sequencing

1. Part 1 first — smaller, reuses existing backend logic almost entirely,
   lower risk.
2. Part 2 second — new tables, more new surface area, benefits from Part 1's
   review cycle shaking out any process issues first.
3. Same discipline as every other change this session: implement → test →
   Simplify (4 parallel review agents) → Skeptic review → re-test → present
   diff → commit/push/deploy only on explicit go-ahead.
