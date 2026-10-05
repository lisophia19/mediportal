# Brainstorm — Directory Routing & Insurance Prompting

Status: brainstorm, not implemented or approved.
Scope: two deferred items from `notes-final-product.md` — desk-number routing to
another practice/department, and an insurance-aware prompting harness.

---

## 1. Routing to another doctor's/practice's desk number

### What already exists

This is further along than it looked at first glance:

- **`DirectoryEntry`** (`backend/app/models.py`) — the full `Directory` sheet tab,
  seeded as-is: `contact`, `location`, `group_name`, `desk_number`, `email`. Not
  scoped to LIBJ, because a redirect target is often an unrelated department.
- **`DirectoryRedirectRule`** — hand-curated `category`/`body_part` → contact mapping
  (e.g. `body_part="Back/Neck"` → contact `"Spine"`). Seeded in
  `backend/app/seed/synthetic.py::seed_directory_redirect_rules`.
- **`_directory_redirect_for(term)`** (`backend/app/blueprints/routing.py:568`) —
  resolves a term to its best-matching rule, then to the `DirectoryEntry`'s desk
  number.
- **Wired into the one dead end that needs it today**: `_no_eligible_doctor_response`
  (routing.py:603) — when `find_doctors` can't seat the caller with *any* doctor
  (`not_covered` / `age_restricted`), the agent speaks the desk number: *"We don't
  have a doctor here who treats that. Let me give you the number for our Spine
  line — that's 844-887-7463."*

This already satisfies the user's first stated condition — **route only when we
genuinely can't schedule for the reason** — for the one path that currently reaches
a hard dead end.

### Correction: Vogent DOES support live call-transfer

**Earlier conclusion in this doc was wrong** -- it only checked flow *node*
types and missed that Vogent *functions* have their own `type: "transfer"`
(allowlisted `destination`, live transfer). Built as `transfer_call`; the
§5.9 escape hatch now really transfers when the resolved number is on the
allowlist, else falls back to speaking it. See `notes-final-product.md`.

### What's actually new to build

1. **Test-mode number overlay.** Real desk numbers come from the spreadsheet.
   For testing, swap in a small set of throwaway numbers (McDonald's locations,
   per the user's example — `+18042221111` etc.) mapped one-per-contact, so a
   human tester can dial the spoken-back number and independently confirm the
   agent picked the *right* contact for the reason, without touching real
   Northwell lines. Proposed shape: an env-gated override table/dict keyed by
   `DirectoryEntry.contact`, applied only when `SCHEDULING_PROVIDER`/a new
   `TEST_MODE` flag is set, leaving the real seed data untouched for production.
   Start with a handful of contacts (Spine, Pain Management, Orthopedic Oncology)
   and expand later.
2. **"Patient asks directly to be routed" — does not exist yet.** The flow is a
   deterministic state machine, not a free-roam agent — there's no existing
   "global interrupt" that catches "just transfer me" from *any* point in the
   call. Two ways to add it, different cost/coverage tradeoff:
   - **Narrow (cheaper):** add an explicit escape-hatch transition only at a
     few existing choke points (e.g. right after the complaint is captured, or
     at any dead end) — caller can only ask there.
   - **Broad (more invasive):** a caller-intent check evaluated at every
     question node, closer to how `GLOBAL_CONTEXT` already injects
     always-on guardrails — but this touches most of the ~138 flow nodes.
   - Needs a decision from the user on where in the call this should actually
     be reachable before picking one.

### Open questions for the user

- Should the "ask to be routed" escape hatch require the caller to name what
  they want routed to (e.g. "transfer me to billing"), or should it map through
  the same term/category → `DirectoryRedirectRule` logic as the automatic
  dead-end case?
- Which of the narrow/broad implementations above is worth the flow-node cost?

---

## 2. Insurance-aware prompting harness

### Current state: no data to build on

Checked every tab in `data/Mediportal Information FINAL.xlsx` (`Terms Priority`,
`Terms Updated`, `Provider Info`, `Directory`, `Practice Information`) — **none
contain any insurance column**. There is no existing `Insurance` model, table, or
field anywhere in the codebase. This is a from-scratch design, not something to
derive from the spreadsheet the way doctor/term data was.

### Research: what real practices actually need to ask

Quick research into how this typically works, to ground the design (not
Oscar/QualCare-specific policy — verify against the real plans before shipping):

- **Baseline info collected at scheduling, any plan:** carrier name, member ID,
  group number, and subscriber name/DOB when different from the patient (e.g. a
  child on a parent's plan). This is universal — needed for eligibility
  verification and billing regardless of plan type.
- **Referral requirement — the most common "provocative question" for
  orthopedics specifically.** HMO-style plans (Oscar's "Guided Care HMO" plans,
  QualCare's HMO products) commonly require a PCP referral on file before a
  specialist visit is billable. PPO/EPO plans on the same carrier typically do
  not. This means the trigger isn't "is it Oscar" — it's "is it an HMO product
  *of* Oscar/QualCare/etc." — plan-tier-dependent, not just carrier-dependent.
- **Prior authorization** — separate from a referral, and typically tied to the
  *service* rather than just the visit: imaging (MRI, CT) and certain
  procedures are the common trigger, a plain office visit usually isn't.
  QualCare's provider materials explicitly warn that a specialist proceeding
  without required prior auth risks non-payment — i.e. this one has real
  downside if skipped, not just a caller-experience nicety.
- **In-network confirmation** — the simplest tier: does the practice take this
  plan at all, flagged before the caller invests time in the rest of the call.

### Proposed shape (for discussion, not yet designed in detail)

A `InsurancePlan`-type table isn't really the right unit — the "depth of info
needed" varies by **carrier × plan tier × service type**, not just carrier. A
plausible model:
- `insurance_carriers` (Oscar, QualCare, ... ) — accepted y/n, whether *any*
  product from this carrier ever requires a referral/pre-auth (to decide
  whether to even ask the follow-up questions).
- A small rule table analogous to `DirectoryRedirectRule`, keyed by
  carrier (+ plan tier if the caller can state it) → `requires_referral`,
  `requires_preauth_for` (imaging/procedure/none).
- Flow-side: after capturing insurance carrier (and plan tier, if askable),
  conditionally ask "do you have a referral on file?" only when the rule says
  the practice needs one — never for carriers/plans that don't require it, to
  respect the existing "hard cutoff on unscripted questions" guardrail already
  in `GLOBAL_CONTEXT`.

### Open questions for the user

Per your own answer, the actual scope here still needs defining:
1. Which specific carriers/plans does the real practice need to handle first —
   just Oscar and QualCare, or a broader list? Do you have (or can Northwell
   provide) the practice's actual payer rules, or should this be built as a
   generically-plausible mock (like the McDonald's test-number approach) since
   there's no source data?
2. Is the goal for this demo just to **show the agent asking the right
   follow-up question at the right time** (a believable mock), or to actually
   gate/flag bookings on the answer (e.g. block booking without a referral)?
3. Should "plan tier" (HMO vs. PPO) be something the caller states, or inferred
   from carrier name alone (less accurate, but one less question to ask)?
