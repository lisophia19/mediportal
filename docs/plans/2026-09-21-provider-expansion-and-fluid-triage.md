# Plan — Orlin & Cohen data expansion + fluid LLM-driven triage

## Context

The baseline demo (and Call 3/Call 4 built after it) is deliberately scoped to
Long Island Bone and Joint's 10 doctors and 5 practices. The real spreadsheet
(`data/Mediportal Information FINAL.xlsx`) has a second, much larger provider
group — **Orlin & Cohen** (58 more doctors) — that was scoped out of the
baseline but whose data is otherwise ready to use as-is: the `Terms Updated`
eligibility sheet already has columns for all 61 doctors combined (not just
the LIBJ 10), and doctors reference their own practices the same generic way
LIBJ doctors do. This is the "Full provider directory" item from
`notes-final-product.md`'s deferred list, now being picked up.

Separately, ambiguous-complaint handling today is split across two
mechanisms: a generic one-retry-then-force-a-guess clarify path
(`needs_clarification`), and a hip/spine-only hardcoded triage special case
(`needs_triage`) that only handles that one body-part pair, caps at one
retry, and (found while designing this plan) has a **live bug** — its
retry-exhausted dead end reads an upstream field (`match_issue_fn.spoken_response`)
that's never populated for the triage path, the same bug class as the
`dead_end_no_doctor` sharing bug fixed earlier this session.

Per direction: when the complaint is obvious (a body part is stated
clearly — today's existing confidence+gap check already handles this, no
change needed), match directly. When it's ambiguous, ask up to **3**
LLM-generated triage questions, each one dynamically generated per the actual
ambiguous candidates (no hardcoded pairs or question text, the same way
`match_issue` already resolves free text via the Anthropic API) — narrowing
or resolving each round — and if still unresolved after 3, give an honest
"someone from the office will call you back" instead of ever forcing a guess.
This replaces both existing mechanisms with one.

Both parts are backend/seed/flow-only — no schema changes, no new migration.

---

## Part 1 — Orlin & Cohen orthopedic data integration

### Verified against the real spreadsheet (via direct read, not assumed)

- Provider Info has exactly two `Group` values: `"Long Island Bone and Joint
  (O&C)"` (10, already seeded) and `"Orlin and Cohen"` (58).
- Specialty filter (exclude Pain Management/Physiatrist/Neurologist, include
  everything else including the 2 with no specialty listed): **49 of the 58**
  pass, giving **59 total doctors**. No name collisions between the two
  groups.
- `Terms Updated`'s doctor columns already cover 61 names; 53 match our
  59-doctor filtered set by exact `"Last, First"` string. The other 8 columns
  are pre-existing name mismatches (e.g. sheet says `Parry, Steven`, Provider
  Info says `Steve Parry`; `Yadegar, Daniel` doesn't exist in Provider Info at
  all) — same class of issue `seed_terms_and_eligibility` already logs as a
  warning and skips, no new handling needed.
- Resulting real counts (computed by replaying the actual seed logic against
  the spreadsheet, not estimated):
  - Doctors seeded: **59** (was 10)
  - Doctors with ≥1 `term_eligibility` row: **52** (was 7)
  - Total distinct terms: **296** (unchanged)
  - Covered terms: **288** (was 247); uncovered: **8** (was 49)
- Practices: the 59 filtered doctors' `Practice 1–6` columns resolve to **14
  distinct practices** (5 existing LIBJ + 9 new: Bohemia, Garden City,
  Huntington Station, Kew Gardens, Lynbrook, Massapequa, Merrick, Rockville
  Centre, Woodbury). Three O&C locations labeled PT/OT-only in `Practice
  Information` (Bellmore, Port Jefferson Station, Rocky Point) aren't
  referenced by any filtered doctor's practice columns, so they're correctly
  never created — no special-casing needed.
- ZIP centroids: `geography.py`'s `ZIP_CENTROIDS` already happens to contain
  6 of the 9 new practice ZIPs (added earlier as generic caller ZIPs). Missing:
  Huntington Station (11746), Kew Gardens (11706 — see data-quality note
  below), Rockville Centre (11570), Woodbury (11797).
  - **Data-quality note to log, not fix**: the spreadsheet's Kew Gardens ZIP
    (11706) is actually Bay Shore's ZIP, already in the table under that
    label — a real spreadsheet error, consistent with this project's existing
    pattern of seeding the data as given and flagging it (e.g. the MRI
    duplicate-row issue). Document it in `notes-final-product.md`, seed it as
    the sheet states.

### Changes

- **`backend/app/seed/spreadsheet.py`**
  - Replace the single `LIBJ_GROUP` equality filter in `seed_doctors` with an
    allowed-groups check (`{"Long Island Bone and Joint (O&C)", "Orlin and
    Cohen"}`) plus an excluded-specialty set (`{"Pain Management",
    "Physiatrist", "Neurologist"}`, checked against `Specialty 1` only,
    matching the verified breakdown above) applied only to the O&C group —
    LIBJ doctors are never specialty-filtered.
  - `seed_practices_and_doctor_practices` and `seed_terms_and_eligibility`
    need **no changes** — both already key off `doctors_by_name`, which will
    simply contain more doctors once `seed_doctors` returns more. Confirmed
    by reading both functions in full.
  - Update the module/function docstrings that currently say "scoped to
    Long Island Bone and Joint" / "LIBJ roster" / "the 5 real LIBJ locations"
    — keep them short per the coding-standards rule, just no longer
    LIBJ-only.
- **`backend/app/seed/geography.py`** — add the 4 missing ZIP centroids
  (Huntington Station, Rockville Centre, Woodbury, and confirm Kew Gardens'
  11706 reuses the existing Bay Shore entry with a comment noting why).
- **`seed_mediportal.py`** — update `EXPECTED_DOCTOR_COUNT` (59),
  `EXPECTED_ELIGIBLE_DOCTOR_COUNT` (52), `EXPECTED_COVERED_TERM_COUNT` (288),
  `EXPECTED_UNCOVERED_TERM_COUNT` (8). `EXPECTED_UNCOVERED_TERM_COUNT` drops
  from 49 to 8 as a natural consequence of more doctors covering more terms,
  not a bug.
- **`notes-final-product.md`** — mark "Full provider directory" as done in
  the deferred list (move it out, or annotate), add the Kew Gardens
  data-quality note next to the existing MRI-duplicate one.
- **`docs/design/spec-ortho-baseline-demo.md`** §8.1 — update the seed-count
  targets to match (spec is the source of truth other docs reference).
- **Tests**: grep `backend/tests/` for doctor/eligible-doctor count literals
  tied to the old LIBJ-only baseline. Most routing tests use factories
  (synthetic doctors, unaffected); this is likely contained to the seed
  summary assertions above, but must be checked directly.

### Verification

- `python3 seed_mediportal.py` (local Postgres) — printed summary must match
  the new expected counts with **no mismatches** line.
- Spot-check a few new O&C doctors resolve through `find_doctors` for a term
  they're eligible for.
- Backend `pytest` suite passes (frontend tests out of scope per this
  instruction).

---

## Part 2 — Fluid, LLM-driven ambiguity triage (replaces both existing mechanisms)

### Current state (confirmed by reading `routing.py` and `vogent_flow.py`)

- **Generic clarify** (`needs_clarification`): one retry only. `ask_clarify`
  → `match_issue_retry_fn` (`final_attempt=true`) → *always* returns a forced
  best-guess match, even on a genuinely unclear second answer. Never
  escalates.
- **Hip/spine triage** (`needs_triage`): one hardcoded pair, one fixed
  question (`HIP_SPINE_TRIAGE_QUESTION`), one fixed classifier prompt
  (`_TRIAGE_CLASSIFIER_PROMPT` / `_classify_hip_or_spine_answer`). One retry
  (`ask_triage_preference` → `resolve_triage_retry_fn`), and **on exhaustion
  routes to `dead_end_no_match_direct`, which speaks
  `{{node.match_issue_fn.spoken_response}}` — a field never populated for
  this path.** Live bug, not yet triggered in the passing 12-scenario suite
  because no real call has exhausted hip/spine triage twice.
- The Vogent flow side is otherwise already dynamic — `ask_triage_question`'s
  prompt is a template (`{{node.match_issue_fn.triage_question}}`), not baked
  text, and `resolve_triage_fn` already takes flat scalar term-id inputs the
  same way every other two-choice node in this flow works.

### New unified design

One mechanism replaces both. `match_issue`'s "obvious" path (today's
existing `MATCH_CONFIDENCE`/`MATCH_GAP` check) is unchanged — that's the
direct-match case and already works. Everything that doesn't clear it enters
a triage loop, capped at 3 rounds, that always either resolves to a real term
or ends in an honest callback offer — never a forced guess.

- **`_generate_triage_question(candidates, prior_qa=None)`** (`routing.py`)
  — given the current ambiguous candidate terms (`term`/`body_part`/
  `category`) and, on rounds 2–3, the prior question(s) and answer(s) already
  asked (so it doesn't repeat itself), asks the LLM for the single best
  one-shot discriminating question a real front-desk person could ask. The
  prompt explicitly allows a direct "is it more like X, or Y?" as a valid
  fallback when no better symptom-based discriminator exists — so this
  always returns a real question, replacing the old separate generic-clarify
  path entirely (no `None` case, no second mechanism).
- **`_classify_triage_answer(candidates, question, answer_text)`** —
  classifies the caller's answer against the current candidates, returning
  the matching term's id, or `UNCLEAR`. Generalizes
  `_classify_hip_or_spine_answer`'s structure (same retry-aware
  `_claude_text` call) without hardcoding hip/spine clinical patterns.
- **`routing.py` endpoint changes**:
  - `match_issue`'s ambiguous branch (today's `hip_match`/`spine_match` scan
    plus the separate generic-clarify fallback) collapses into one block:
    take the top-2 candidates, call `_generate_triage_question`, return
    `needs_triage` with the question + `term_a_id`/`term_a_label`/
    `term_b_id`/`term_b_label` (renamed from `hip_term_id`/`spine_term_id`).
    `needs_clarification` status is removed.
  - `resolve_triage` is renamed field-for-field (`term_a_id`/`term_b_id`) and
    restructured: on a resolved answer, unchanged (`matched` + term +
    `confirm_prompt`). On `UNCLEAR`, it now **also** calls
    `_generate_triage_question` again for a follow-up question and returns
    `status: "still_unclear"` + the new `triage_question` — the same
    response shape every round, so **one backend endpoint serves all 3
    rounds**; only the Vogent flow nodes are duplicated per round (the
    established, accepted pattern in this codebase for Vogent's static
    per-node templates — see the file's own "Known simplifications" header).
- **`vogent/vogent_flow.py` changes**:
  - Remove `ask_clarify`, `match_issue_retry_fn`, `confirm_complaint_2`,
    `save_term_clarified`, `dead_end_no_match_clarified` (the old
    force-a-guess mechanism, fully superseded).
  - Rename `match_issue_fn`'s triage-related outputs
    (`hip_term_id`/`hip_label`/`spine_term_id`/`spine_label` →
    `term_a_id`/`term_a_label`/`term_b_id`/`term_b_label`).
  - Build 3 round node-pairs (question node + `resolve_triage` function
    node), each identical in shape to today's existing
    `ask_triage_question`/`resolve_triage_fn` pair, chained so round *N*'s
    `still_unclear` transition goes to round *N+1*'s question node, and round
    3's `still_unclear` transition goes to a **new, dedicated**
    `dead_end_triage_exhausted` node — static hardcoded text ("let me have
    someone from the office call you back"), not a template reference to any
    upstream `spoken_response`, which is exactly what fixes the live bug
    above (no shared/mis-wired dead end to get wrong a second time).
  - No new Vogent functions to register (`resolve_triage` already exists) —
    ships as a flow republish only, no `vogent_setup.py` changes.

### Tests (`backend/tests/test_routing.py`, backend only)

- Replace the hip/spine-specific tests with generic ones: mock
  `_generate_triage_question`/`_classify_triage_answer` directly rather than
  relying on real hip/spine body-part detection.
- New: 3-round exhaustion — `_classify_triage_answer` returns `UNCLEAR` all 3
  times → final response is the honest callback status, not a forced match.
- New: round 2/3 pass prior Q&A into `_generate_triage_question` (assert the
  mock receives it) so a real follow-up question can't just repeat round 1.
- Remove the now-dead `needs_clarification`/`ask_clarify` tests.

### Docs

- **`README.md`**'s "Issue matching (spec §5.1)" section — replace the
  `needs_clarification` description with the unified triage design: obvious
  complaints match directly (unchanged), ambiguous ones get up to 3
  LLM-generated discriminating questions (not a fixed list — generated fresh
  per ambiguous pair the same way `_classify_complaint` already works), then
  an honest callback offer.
- **`docs/design/spec-ortho-baseline-demo.md`** — update §5.1's
  `needs_clarification`/`needs_triage` description to match.

### Verification

- Backend `pytest` suite passes.
- Manual sanity check of `_generate_triage_question` against a few real
  ambiguous pairs from the expanded term set (hip/knee, neck/shoulder, and a
  case with no natural symptom discriminator) via a local script call —
  confirms it's actually discriminating, not just always firing the same way.
- New `vogent/run_test_calls.py` scenarios (see below), run live against the
  test-caller agent after deploy.

---

## New live test-call scenarios (`vogent/run_test_calls.py`)

Write more scripts and run them with `mediportal-test-caller`
(already-built agent-to-agent infra, confirmed working — 12/12 existing
scenarios passed 2026-09-19, see `docs/testing/agent-call-test-checklist.md`,
whose stale "structurally blocked" claim was already corrected this
session). Add:

- **`triage_hip_vs_knee`** — vague pain description that plausibly fits
  either a hip or knee term; scripted answers to whatever discriminating
  question the agent generates.
- **`triage_neck_vs_shoulder`** — same shape, neck/shoulder ambiguity.
- **`triage_exhausted_callback`** — a caller who gives a genuinely
  unclassifiable answer to all 3 rounds; *expect* the honest callback
  message, never a forced booking.
- (Call 3/Call 4 scenarios — `named_doctor_right_office`,
  `named_doctor_wrong_office`, `named_doctor_age_restricted`,
  `prerequisite_not_done`, `prerequisite_done` — were already added earlier
  this session and are still unrun; run those alongside the new ones.)

Update `docs/testing/agent-call-test-checklist.md` with checklist rows for
all of these, same format as the existing entries.

---

## Implementation workflow

Each part goes through: **Implement → Test → Simplify → Skeptic Review →
Test Again**, backend tests only (no frontend test requirement for this
work), with human review/approval before commit at the end of each part —
same discipline as every prior feature this session.

## Sequencing

1. Write this approved plan to `docs/plans/2026-09-21-provider-expansion-and-fluid-triage.md` before any code changes. *(this file)*
2. **Part 1** (data/config only, lower risk, no flow changes) — full
   Implement → Test → Simplify → Skeptic Review → Test Again loop, then
   present for review before committing.
3. **Part 2** (the flow behavior change) — same loop. Given it removes live
   flow nodes and fixes a real bug, the skeptic review step should
   specifically check: every dead-end node's `spoken_response`/template
   reference resolves from a node that actually ran on that path (the exact
   bug class already found twice this session).
4. New test-call scenarios — written alongside Part 2, run live only after
   Part 2 is committed, pushed, and redeployed (needs the real endpoints
   live to call against).
5. Same commit → review → push → deploy discipline as every prior feature
   this session — nothing pushed, redeployed, or run against the live
   test-caller without explicit go-ahead at each step.
