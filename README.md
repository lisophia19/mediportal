# mediportal

Voice-based scheduling agent demo: Vogent phone agent + Flask API + Postgres +
React dashboard. See `docs/design/spec-ortho-baseline-demo.md` for the full
design and `docs/plans/2026-09-13-scheduling-flow-plan.md` for the
implementation plan. Deferred/future-product items are tracked in
`notes-final-product.md`.

## Backend

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt

createdb mediportal
export FLASK_APP=backend/wsgi.py DATABASE_URL="postgresql://localhost/mediportal" \
       AGENT_KEY="dev-agent-key" SECRET_KEY="dev-secret-change-me"
cd backend && flask db upgrade && cd ..
python3 seed_mediportal.py
```

### Run

```bash
export FLASK_APP=backend/wsgi.py DATABASE_URL="postgresql://localhost/mediportal" \
       AGENT_KEY="dev-agent-key" SECRET_KEY="dev-secret-change-me"
flask run
```

Vogent-facing routes (`/patients`, `/routing`, `/availability`, `/appointments`,
`/calls`) require an `X-Agent-Key` header matching `AGENT_KEY`. Dashboard routes
(`/auth/login`, `/calls` via the dashboard, i.e. GET) require a JWT bearer token
from `/auth/login`. Seeded dashboard login: `frontdesk@libj-demo.example` /
`change-me-before-demo` (see `backend/app/seed/synthetic.py`).

### Tests

Backend tests run against a dedicated `mediportal_test` Postgres database —
never the dev DB — with tables created directly via `db.create_all()` (not
migrations) and each test isolated inside a rolled-back transaction
(`backend/tests/conftest.py`).

```bash
createdb mediportal_test   # once
cd backend
export DATABASE_URL="postgresql://localhost/mediportal_test" \
       TEST_DATABASE_URL="postgresql://localhost/mediportal_test" \
       PYTHONPATH=$(pwd)
python -m pytest tests/ -v
```

Run a single file with e.g. `pytest tests/test_routing.py -v`.

### Issue matching (spec §5.1)

`POST /routing/match-issue` turns a caller's free-text complaint into a
clinical term from the seeded `terms` table, without any doctor/specialty
knowledge in the matching code itself (routing stays entirely data-driven —
see spec §1). Two-stage approach in `backend/app/blueprints/routing.py`:

1. **Keyword pre-filter** (`_candidate_terms`) — cheap in-process scoring of
   every seeded term's `term` / `body_part` / `category` / `patient_phrasing`
   synonyms against the words in the complaint, so the LLM prompt only has to
   reason over a short candidate list (`MAX_CANDIDATE_TERMS`, currently 12),
   not the full term table.
2. **LLM classification** (`_classify_complaint`) — the candidate list plus
   the raw complaint text are sent to the **Anthropic API** (`anthropic`
   Python SDK, model configurable via `ANTHROPIC_MODEL`, default
   `claude-sonnet-5`; needs `ANTHROPIC_API_KEY` set in the environment). The
   model returns a confidence-scored pick (or "none of these"), which is
   bucketed into one of three response shapes: `matched` (confident single
   term), `needs_clarification` (ambiguous between top candidates, returns a
   disambiguating question), or `no_match` (no reasonable candidate — a
   plain-language front-desk response, never a raw error). Thresholds are in
   `routing.py`: `MATCH_CONFIDENCE`, `CLARIFY_CONFIDENCE`, `MATCH_GAP`.

Patient last-name matching (`POST /patients/lookup`, spec §5.3) uses a
different technique — `rapidfuzz` (`fuzz.WRatio`) for the soft first-name
scoring step when a last-name+DOB lookup returns multiple candidates. No LLM
call is involved there; see spec §5.3 for the full 4-layer disambiguation
(caller-ID match → first-name score → confirm-by-readback →
`ambiguous_unresolved`).
