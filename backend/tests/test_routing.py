# Tests for backend/app/blueprints/routing.py -- spec §5.1 (match-issue),
# §5.2 (find-doctors), §5.9 (directory redirect). The Anthropic call in
# match-issue is mocked throughout -- these tests verify our threshold and
# response-shaping logic, not real LLM behavior. See report for what still
# needs a real ANTHROPIC_API_KEY to exercise end-to-end.
import json
from datetime import date

import pytest

from app.blueprints import routing as routing_module
from app.models import (
    Call,
    Doctor,
    DoctorPractice,
    DirectoryEntry,
    DirectoryRedirectRule,
    Practice,
    Term,
    TermEligibility,
    ZipCentroid,
)


class _FakeMessage:
    def __init__(self, text, with_thinking_block=False):
        blocks = []
        if with_thinking_block:
            # Claude sometimes reasons before answering on more complex
            # prompts -- the thinking block has no .text, or an unrelated
            # one, and must not be mistaken for the real answer.
            blocks.append(type("Block", (), {"type": "thinking", "text": None})())
        blocks.append(type("Block", (), {"type": "text", "text": text})())
        self.content = blocks


class _FakeMessages:
    def __init__(self, payload, with_thinking_block=False):
        self._payload = payload
        self._with_thinking_block = with_thinking_block

    def create(self, **kwargs):
        return _FakeMessage(json.dumps(self._payload), self._with_thinking_block)


class _FakeClient:
    def __init__(self, payload, with_thinking_block=False):
        self.messages = _FakeMessages(payload, with_thinking_block)


def _mock_llm(monkeypatch, payload, with_thinking_block=False):
    monkeypatch.setattr(
        routing_module, "_anthropic_client", lambda: _FakeClient(payload, with_thinking_block)
    )


def _make_term(db, **overrides):
    defaults = dict(
        term="Fracture-Wrist",
        body_part="Hand/Wrist",
        category="Fracture",
        urgency=None,
        patient_phrasing=["broke my wrist", "wrist fracture"],
        default_appointment_type="new_patient_consult",
    )
    defaults.update(overrides)
    term = Term(**defaults)
    db.add(term)
    db.flush()
    return term


def test_candidate_terms_matches_whole_words_not_substrings(client, db):
    """Regression test for a real production miss: a genuine knee-pain
    complaint ("...for a few months...") never surfaced Pain-Knee or
    Osteo/Arthritis-Knee as candidates -- the old substring-based pre-filter
    let "for" spuriously match inside "Deformity" (category text), crowding
    out the real knee terms with unrelated hand/foot/wrist deformity terms."""
    knee_pain = _make_term(
        db, term="Pain-Knee", body_part="Knee/LE", category="pain", patient_phrasing=[]
    )
    hand_deformity = _make_term(
        db, term="Congenital Deformity-Hand", body_part="Hand/Wrist", category="Deformity", patient_phrasing=[]
    )
    db.commit()

    candidates = routing_module._candidate_terms(
        "My right knee has been bothering me for a few months now. It's swollen "
        "and pretty stiff, especially in the mornings after I've been sitting "
        "for a little while."
    )

    candidate_ids = {t.id for t in candidates}
    assert knee_pain.id in candidate_ids
    assert hand_deformity.id not in candidate_ids


def test_match_issue_with_no_reason_given_asks_instead_of_guessing(client, db, agent_headers, monkeypatch):
    """Regression test for real production nondeterminism: "I'd like to
    schedule an appointment, please" carries zero clinical signal, so
    sending it into the LLM classifier produced three different outcomes
    (no_match, needs_clarification, and an outright parse error) across
    three identical real calls. Detect the no-signal case deterministically
    and just ask, without ever calling the LLM."""
    called = {"n": 0}

    def _boom(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("LLM should never be called for a content-free complaint")

    monkeypatch.setattr(routing_module, "_classify_complaint", _boom)

    resp = client.post(
        "/api/v1/routing/match-issue",
        json={"complaint_text": "I'd like to schedule an appointment, please.", "call_id": "vg_noreason"},
        headers=agent_headers,
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["status"] == "needs_clarification"
    assert called["n"] == 0


def test_match_issue_final_attempt_commits_instead_of_dead_ending(
    client, db, agent_headers, monkeypatch
):
    """Regression test for a real dead-end: on the clarification retry the
    flow has nowhere to send another needs_clarification, so an ambiguous
    second answer fell through its catch-all and the caller was told we
    couldn't help -- for "my shoulder has been aching for a couple of
    weeks", a perfectly bookable complaint. With final_attempt the retry
    commits to the best candidate and confirms it instead."""
    shoulder = _make_term(db, term="Pain-Shoulder", body_part="Shoulder/UE")
    arthritis = _make_term(db, term="Arthritis-Shoulder", body_part="Shoulder/UE")
    db.commit()
    payload = {
        "ortho_relevant": True,
        "matches": [
            {"term_id": shoulder.id, "confidence": 0.45, "spoken_label": "shoulder pain"},
            {"term_id": arthritis.id, "confidence": 0.40, "spoken_label": "shoulder arthritis"},
        ],
    }

    _mock_llm(monkeypatch, payload)
    ambiguous = client.post(
        "/api/v1/routing/match-issue",
        json={"complaint_text": "my shoulder aches", "call_id": "vg_fa1"},
        headers=agent_headers,
    ).get_json()
    assert ambiguous["status"] == "needs_clarification"  # first round still asks

    _mock_llm(monkeypatch, payload)
    final = client.post(
        "/api/v1/routing/match-issue",
        json={
            "complaint_text": "my shoulder aches",
            "call_id": "vg_fa2",
            "final_attempt": "true",
        },
        headers=agent_headers,
    ).get_json()
    assert final["status"] == "matched"
    assert final["term"]["id"] == shoulder.id
    assert final["alternate_1_id"] == arthritis.id


def test_match_issue_final_attempt_still_declines_non_ortho(client, db, agent_headers, monkeypatch):
    """final_attempt commits to a best guess, but must not override an
    honest "we don't treat that" -- ortho_relevant stays the gate."""
    _make_term(db)
    db.commit()
    _mock_llm(monkeypatch, {"ortho_relevant": False, "matches": []})

    resp = client.post(
        "/api/v1/routing/match-issue",
        json={"complaint_text": "I have a migraine", "call_id": "vg_fa3", "final_attempt": "true"},
        headers=agent_headers,
    )
    assert resp.get_json()["status"] == "no_match"


def test_candidate_terms_ignores_filler_word_overlap(client, db):
    """Regression test: "and" in the complaint's own filler text used to
    exact-match "and" inside the body_part "Foot and Ankle", giving every
    foot/ankle term a spurious +1 over terms that only scored on "pain" --
    real vague complaint "...a lot of pain...and I'm not sure..." matched
    only Foot and Ankle terms, never Back/Neck or Hip."""
    back_pain = _make_term(
        db, term="Pain-Back", body_part="Back/Neck", category="pain", patient_phrasing=[]
    )
    ankle_pain = _make_term(
        db, term="Pain-Ankle", body_part="Foot and Ankle", category="pain", patient_phrasing=[]
    )
    db.commit()

    candidates = routing_module._candidate_terms(
        "I've been I've got a lot of pain lately, and I'm not really sure what's causing it."
    )

    scores = {t.id: i for i, t in enumerate(candidates)}
    assert back_pain.id in scores
    assert ankle_pain.id in scores
    # Both score equally on "pain" alone -- neither should be pushed out by
    # the complaint's own filler word "and" matching "Foot and Ankle".


def test_candidate_terms_diversifies_tied_scores_across_body_parts(client, db, monkeypatch):
    """Regression test: with many terms tied at the same keyword-overlap
    score, id order used to cluster low-id body parts in the top
    MAX_CANDIDATE_TERMS, silently excluding others (e.g. Hip) needed for
    hip-vs-spine triage to even reach the LLM. Round-robin across body_part
    instead."""
    monkeypatch.setattr(routing_module, "MAX_CANDIDATE_TERMS", 2)
    hip = _make_term(db, term="Pain-Hip", body_part="Hip", category="pain", patient_phrasing=[])
    back = _make_term(db, term="Pain-Back", body_part="Back/Neck", category="pain", patient_phrasing=[])
    # A third same-body-part term with a lower id than `back` would win a
    # pure id-order tiebreak and crowd it out -- diversification must not
    # let that happen once every body_part has at least one slot.
    another_hip = _make_term(db, term="Pain-Groin", body_part="Hip", category="pain", patient_phrasing=[])
    db.commit()

    candidates = routing_module._candidate_terms("I have a lot of pain.")
    candidate_ids = {t.id for t in candidates}
    assert hip.id in candidate_ids or another_hip.id in candidate_ids
    assert back.id in candidate_ids


# --- §5.1 match-issue ---------------------------------------------------


def test_match_issue_matched(client, db, agent_headers, monkeypatch):
    term = _make_term(db)
    db.commit()
    _mock_llm(
        monkeypatch,
        {
            "ortho_relevant": True,
            "matches": [{"term_id": term.id, "confidence": 0.92, "spoken_label": "a possible wrist fracture"}],
        },
    )

    resp = client.post(
        "/api/v1/routing/match-issue",
        json={"complaint_text": "I fell and my wrist is killing me", "call_id": "vg_1"},
        headers=agent_headers,
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["status"] == "matched"
    assert body["term"]["id"] == term.id
    assert "confirm_prompt" in body
    # No runner-up in this mocked payload -- alternates are nullable.
    assert body["alternate_1_id"] is None


def test_match_issue_matched_includes_flat_alternates(client, db, agent_headers, monkeypatch):
    """The Vogent flow needs these as scalar inputs if the caller says the
    top match is wrong -- offer the runner-up(s) instead of just re-asking
    from scratch. Flat fields, not a nested array, since flow templates
    cannot index into an array as a downstream function input."""
    term_top = _make_term(db, term="Fracture-Wrist")
    term_alt1 = _make_term(db, term="Pain-Wrist", category="pain")
    term_alt2 = _make_term(db, term="Osteo/Arthritis-Wrist", category="Arthritis/Osteoarthritis")
    db.commit()
    _mock_llm(
        monkeypatch,
        {
            "ortho_relevant": True,
            "matches": [
                {"term_id": term_top.id, "confidence": 0.9, "spoken_label": "a possible wrist fracture"},
                {"term_id": term_alt1.id, "confidence": 0.4, "spoken_label": "wrist pain"},
                {"term_id": term_alt2.id, "confidence": 0.3, "spoken_label": "wrist arthritis"},
            ],
        },
    )

    resp = client.post(
        "/api/v1/routing/match-issue",
        json={"complaint_text": "my wrist hurts", "call_id": "vg_1"},
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "matched"
    assert body["alternate_1_id"] == term_alt1.id
    assert body["alternate_1_label"] == "wrist pain"
    assert body["alternate_2_id"] == term_alt2.id
    assert body["alternate_2_label"] == "wrist arthritis"


def test_match_issue_handles_leading_thinking_block(client, db, agent_headers, monkeypatch):
    """Regression test: Claude sometimes reasons in a `thinking` block
    before the `text` block on more complex prompts (real production traffic
    hit this) -- content[0] is not reliably the text block."""
    term = _make_term(db)
    db.commit()
    _mock_llm(
        monkeypatch,
        {
            "ortho_relevant": True,
            "matches": [{"term_id": term.id, "confidence": 0.92, "spoken_label": "a possible wrist fracture"}],
        },
        with_thinking_block=True,
    )

    resp = client.post(
        "/api/v1/routing/match-issue",
        json={"complaint_text": "I fell and my wrist is killing me", "call_id": "vg_1"},
        headers=agent_headers,
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["status"] == "matched"
    assert body["term"]["id"] == term.id


def test_match_issue_handles_markdown_fenced_json(client, db, agent_headers, monkeypatch):
    """Regression test: real production traffic showed the model wrapping
    its JSON answer in a ```json fence despite being told not to."""
    term = _make_term(db)
    db.commit()
    payload = {
        "ortho_relevant": True,
        "matches": [{"term_id": term.id, "confidence": 0.92, "spoken_label": "a possible wrist fracture"}],
    }
    fenced_message = _FakeMessage("```json\n" + json.dumps(payload) + "\n```")
    fake_client = _FakeClient(payload)
    fake_client.messages.create = lambda **kwargs: fenced_message
    monkeypatch.setattr(routing_module, "_anthropic_client", lambda: fake_client)

    resp = client.post(
        "/api/v1/routing/match-issue",
        json={"complaint_text": "I fell and my wrist is killing me", "call_id": "vg_1"},
        headers=agent_headers,
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["status"] == "matched"
    assert body["term"]["id"] == term.id


def test_match_issue_needs_clarification(client, db, agent_headers, monkeypatch):
    term_a = _make_term(db, term="Fracture-Wrist")
    term_b = _make_term(db, term="Pain-Wrist", category="pain")
    db.commit()
    _mock_llm(
        monkeypatch,
        {
            "ortho_relevant": True,
            "matches": [
                {"term_id": term_a.id, "confidence": 0.55, "spoken_label": "a possible wrist fracture"},
                {"term_id": term_b.id, "confidence": 0.50, "spoken_label": "ongoing wrist pain"},
            ],
        },
    )

    resp = client.post(
        "/api/v1/routing/match-issue",
        json={"complaint_text": "my wrist hurts", "call_id": "vg_2"},
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "needs_clarification"
    assert len(body["candidates"]) == 2
    assert "clarify_prompt" in body


def test_match_issue_ambiguous_hip_vs_spine_triggers_triage(client, db, agent_headers, monkeypatch):
    """Hip and spine share vague, overlapping presentations ("pain") often
    enough that the generic 'is it more like X or Y' clarify_prompt is a
    weak question -- this specific ambiguity should trigger the dedicated
    screening question instead."""
    hip_term = _make_term(db, term="Pain-Hip", body_part="Hip", category="pain")
    spine_term = _make_term(db, term="Pain-Back", body_part="Back/Neck", category="pain")
    db.commit()
    _mock_llm(
        monkeypatch,
        {
            "ortho_relevant": True,
            "matches": [
                {"term_id": hip_term.id, "confidence": 0.5, "spoken_label": "hip pain"},
                {"term_id": spine_term.id, "confidence": 0.45, "spoken_label": "back pain"},
            ],
        },
    )

    resp = client.post(
        "/api/v1/routing/match-issue",
        json={"complaint_text": "I have some pain", "call_id": "vg_3"},
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "needs_triage"
    assert body["hip_term_id"] == hip_term.id
    assert body["spine_term_id"] == spine_term.id
    assert "triage_question" in body


def test_resolve_triage_hip_answer(client, db, agent_headers, monkeypatch):
    hip_term = _make_term(db, term="Pain-Hip", body_part="Hip", category="pain")
    spine_term = _make_term(db, term="Pain-Back", body_part="Back/Neck", category="pain")
    db.commit()
    monkeypatch.setattr(routing_module, "_classify_hip_or_spine_answer", lambda q, a: "HIP")

    resp = client.post(
        "/api/v1/routing/resolve-triage",
        json={
            "triage_answer": "it mostly just stays in one spot",
            "hip_term_id": hip_term.id,
            "spine_term_id": spine_term.id,
            "call_id": "vg_3",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "matched"
    assert body["term"]["id"] == hip_term.id
    assert body["alternate_1_id"] == spine_term.id


def test_resolve_triage_spine_answer(client, db, agent_headers, monkeypatch):
    hip_term = _make_term(db, term="Pain-Hip", body_part="Hip", category="pain")
    spine_term = _make_term(db, term="Pain-Back", body_part="Back/Neck", category="pain")
    db.commit()
    monkeypatch.setattr(routing_module, "_classify_hip_or_spine_answer", lambda q, a: "SPINE")

    resp = client.post(
        "/api/v1/routing/resolve-triage",
        json={
            "triage_answer": "it shoots down my leg",
            "hip_term_id": hip_term.id,
            "spine_term_id": spine_term.id,
            "call_id": "vg_3",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "matched"
    assert body["term"]["id"] == spine_term.id
    assert body["alternate_1_id"] == hip_term.id


def test_resolve_triage_unclear_answer(client, db, agent_headers, monkeypatch):
    hip_term = _make_term(db, term="Pain-Hip", body_part="Hip", category="pain")
    spine_term = _make_term(db, term="Pain-Back", body_part="Back/Neck", category="pain")
    db.commit()
    monkeypatch.setattr(routing_module, "_classify_hip_or_spine_answer", lambda q, a: "UNCLEAR")

    resp = client.post(
        "/api/v1/routing/resolve-triage",
        json={
            "triage_answer": "I don't really know",
            "hip_term_id": hip_term.id,
            "spine_term_id": spine_term.id,
            "call_id": "vg_3",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "still_unclear"
    assert "spoken_response" in body


def test_resolve_triage_requires_fields(client, agent_headers):
    resp = client.post("/api/v1/routing/resolve-triage", json={}, headers=agent_headers)
    assert resp.status_code == 400


def test_match_issue_low_confidence_ortho_relevant_clarifies_not_no_match(
    client, db, agent_headers, monkeypatch
):
    """Regression test for a real production miss: "I have a lot of pain,
    not sure what's causing it" is genuinely orthopedic but too vague to
    name a specific term -- the LLM honestly returns low-confidence
    matches (ortho_relevant=true), which used to get declined as no_match
    outright by a confidence floor. It should ask a clarifying question
    instead, the way a real front-desk person would."""
    back = _make_term(db, term="Pain-Back", body_part="Back/Neck")
    elbow = _make_term(db, term="Pain-Elbow", body_part="Elbow")
    db.commit()
    _mock_llm(
        monkeypatch,
        {
            "ortho_relevant": True,
            "matches": [
                {"term_id": back.id, "confidence": 0.2, "spoken_label": "back pain"},
                {"term_id": elbow.id, "confidence": 0.15, "spoken_label": "elbow pain"},
            ],
        },
    )

    resp = client.post(
        "/api/v1/routing/match-issue",
        json={"complaint_text": "I have a lot of pain, not sure what's causing it", "call_id": "vg_lowconf"},
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "needs_clarification"


def test_match_issue_no_match(client, db, agent_headers, monkeypatch):
    _make_term(db)
    db.commit()
    _mock_llm(monkeypatch, {"ortho_relevant": False, "matches": []})

    resp = client.post(
        "/api/v1/routing/match-issue",
        json={"complaint_text": "I have a headache", "call_id": "vg_3"},
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "no_match"
    assert "spoken_response" in body


def test_match_issue_llm_unavailable_returns_503(client, db, agent_headers, monkeypatch):
    _make_term(db)
    db.commit()

    def _raise():
        raise RuntimeError("connection failed")

    monkeypatch.setattr(routing_module, "_anthropic_client", _raise)

    resp = client.post(
        "/api/v1/routing/match-issue",
        json={"complaint_text": "my wrist hurts", "call_id": "vg_4"},
        headers=agent_headers,
    )
    assert resp.status_code == 503
    assert resp.get_json()["status"] == "error"


def test_strip_markdown_fence():
    strip = routing_module._strip_markdown_fence
    assert strip('{"a": 1}') == '{"a": 1}'
    assert strip('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip('```\n{"a": 1}\n```') == '{"a": 1}'
    assert strip('  {"a": 1}  ') == '{"a": 1}'


# --- §5.2 find-doctors + §5.9 directory redirect ------------------------


def _make_practice(db, name, zip_code, lat=None, lon=None, main_phone="516-555-9000"):
    # practices.latitude/longitude are denormalized from the ZIP-centroid
    # table at seed time (spec §3.2) -- routing reads the practice columns
    # directly, not a live ZipCentroid join, so both must be set here.
    practice = Practice(
        name=name, address=f"{name} Ave", zip=zip_code, main_phone=main_phone, latitude=lat, longitude=lon
    )
    db.add(practice)
    db.flush()
    if lat is not None:
        db.add(ZipCentroid(zip=zip_code, latitude=lat, longitude=lon, label=name))
    return practice


def _make_doctor(db, first_name, last_name, specialty, practice, active=True):
    doctor = Doctor(first_name=first_name, last_name=last_name, specialty=specialty, active=active)
    db.add(doctor)
    db.flush()
    db.add(DoctorPractice(doctor_id=doctor.id, practice_id=practice.id, is_primary=True))
    return doctor


def test_find_doctors_matched_ranks_by_distance(client, db, agent_headers):
    term = _make_term(db)
    caller_zip = ZipCentroid(zip="11563", latitude=40.6551, longitude=-73.6768, label="Lynbrook")
    db.add(caller_zip)

    near = _make_practice(db, "Merrick", "11566", lat=40.6668, lon=-73.5502)
    far = _make_practice(db, "Port Jefferson", "11777", lat=40.9462, lon=-73.0704)

    doc_far = _make_doctor(db, "Bennett", "Brown", "Hand & Wrist", far)
    doc_near = _make_doctor(db, "Alice", "Chen", "Hand & Wrist", near)
    db.add(TermEligibility(term_id=term.id, doctor_id=doc_far.id, min_age=1, max_age=100))
    db.add(TermEligibility(term_id=term.id, doctor_id=doc_near.id, min_age=1, max_age=100))
    db.commit()

    resp = client.post(
        "/api/v1/routing/find-doctors",
        json={"term_id": term.id, "date_of_birth": "1991-04-02", "zip": "11563", "call_id": "vg_5"},
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "matched"
    assert body["doctors"][0]["doctor_id"] == doc_near.id
    assert body["doctors"][0]["distance_miles"] < body["doctors"][1]["distance_miles"]
    # Flat top-choice fields for the Vogent flow, which cannot index into
    # the doctors array as a downstream function input (see routing.py).
    assert body["best_doctor_id"] == doc_near.id
    assert body["best_practice_id"] == near.id
    assert body["best_doctor_spoken_label"] == body["doctors"][0]["spoken_label"]
    assert body["best_doctor_name"] == body["doctors"][0]["name"]


def test_find_doctors_excludes_already_tried_doctor(client, db, agent_headers):
    """A caller who's told the closest doctor has no slots (or explicitly
    asks for someone else) should get the NEXT-best eligible doctor, not
    the same one again or a dead end -- real feedback: the agent almost
    always routed to the same doctor with no fallback."""
    term = _make_term(db)
    caller_zip = ZipCentroid(zip="11563", latitude=40.6551, longitude=-73.6768, label="Lynbrook")
    db.add(caller_zip)

    near = _make_practice(db, "Merrick", "11566", lat=40.6668, lon=-73.5502)
    far = _make_practice(db, "Port Jefferson", "11777", lat=40.9462, lon=-73.0704)

    doc_far = _make_doctor(db, "Bennett", "Brown", "Hand & Wrist", far)
    doc_near = _make_doctor(db, "Alice", "Chen", "Hand & Wrist", near)
    db.add(TermEligibility(term_id=term.id, doctor_id=doc_far.id, min_age=1, max_age=100))
    db.add(TermEligibility(term_id=term.id, doctor_id=doc_near.id, min_age=1, max_age=100))
    db.commit()

    resp = client.post(
        "/api/v1/routing/find-doctors",
        json={
            "term_id": term.id,
            "date_of_birth": "1991-04-02",
            "zip": "11563",
            "call_id": "vg_exclude",
            "excluded_doctor_id": doc_near.id,
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "matched"
    assert body["best_doctor_id"] == doc_far.id


def test_find_doctors_excluding_the_only_eligible_doctor_gives_honest_reason(client, db, agent_headers):
    term = _make_term(db)
    practice = _make_practice(db, "Merrick", "11566", lat=40.6668, lon=-73.5502)
    doctor = _make_doctor(db, "Alice", "Chen", "Hand & Wrist", practice)
    db.add(TermEligibility(term_id=term.id, doctor_id=doctor.id, min_age=1, max_age=100))
    db.commit()

    resp = client.post(
        "/api/v1/routing/find-doctors",
        json={
            "term_id": term.id,
            "date_of_birth": "1991-04-02",
            "zip": "11566",
            "call_id": "vg_exclude_only",
            "excluded_doctor_id": doctor.id,
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "no_eligible_doctor"
    assert body["reason"] == "no_more_doctors"


def test_find_doctors_falls_back_to_term_id_recorded_on_call(client, db, agent_headers):
    """The flow can reach find-doctors from more than one upstream path (a
    direct match vs. one that needed a clarifying round) -- when it doesn't
    pass term_id explicitly, this falls back to whatever update_call already
    recorded on the call record."""
    term = _make_term(db)
    practice = _make_practice(db, "Merrick", "11566", lat=40.6668, lon=-73.5502)
    doctor = _make_doctor(db, "Alice", "Chen", "Hand & Wrist", practice)
    db.add(TermEligibility(term_id=term.id, doctor_id=doctor.id, min_age=1, max_age=100))
    db.add(Call(vogent_call_id="vg_fallback", matched_term_id=term.id))
    db.commit()

    resp = client.post(
        "/api/v1/routing/find-doctors",
        json={"date_of_birth": "1991-04-02", "zip": "11563", "call_id": "vg_fallback"},
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "matched"
    assert body["doctors"][0]["doctor_id"] == doctor.id


def test_find_doctors_age_restricted(client, db, agent_headers):
    term = _make_term(db, term="Fracture-Elbow", body_part="Elbow")
    practice = _make_practice(db, "Southampton", "11968", lat=40.8676, lon=-72.3882)
    doctor = _make_doctor(db, "Adult", "Only", "Sports Medicine", practice)
    db.add(TermEligibility(term_id=term.id, doctor_id=doctor.id, min_age=18, max_age=100))
    db.commit()

    resp = client.post(
        "/api/v1/routing/find-doctors",
        json={"term_id": term.id, "date_of_birth": "2020-01-01", "zip": "11968", "call_id": "vg_6"},
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "no_eligible_doctor"
    assert body["reason"] == "age_restricted"
    assert "spoken_response" in body


def test_find_doctors_not_covered_with_directory_redirect(client, db, agent_headers):
    term = _make_term(db, term="Arthroscopy-Wrist", category="Surgical Procedure", body_part="Hand/Wrist")
    db.add(DirectoryRedirectRule(category="Surgical Procedure", contact="Spine"))
    db.add(DirectoryEntry(contact="Spine", location="General", group_name="LIBJ", desk_number="844-887-7463"))
    db.commit()

    resp = client.post(
        "/api/v1/routing/find-doctors",
        json={"term_id": term.id, "date_of_birth": "1991-04-02", "zip": "11563", "call_id": "vg_7"},
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "no_eligible_doctor"
    assert body["reason"] == "not_covered"
    assert body["directory_redirect"] == {"contact": "Spine", "desk_number": "844-887-7463"}


def test_find_doctors_not_covered_without_redirect_falls_back_to_main_line(client, db, agent_headers):
    term = _make_term(db, term="Unmapped-Procedure", category="Nonexistent", body_part="Unspecified")
    practice = _make_practice(db, "Riverhead", "11901", lat=40.9176, lon=-72.6620, main_phone="631-555-1000")
    db.commit()

    resp = client.post(
        "/api/v1/routing/find-doctors",
        json={"term_id": term.id, "date_of_birth": "1991-04-02", "zip": "11901", "call_id": "vg_8"},
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "no_eligible_doctor"
    assert body["reason"] == "not_covered"
    assert body["directory_redirect"] is None
    assert "631-555-1000" in body["spoken_response"]


def test_find_doctors_requires_agent_key(client, db):
    resp = client.post(
        "/api/v1/routing/find-doctors",
        json={"term_id": 1, "date_of_birth": "1991-04-02", "zip": "11563", "call_id": "vg_9"},
    )
    assert resp.status_code == 401
