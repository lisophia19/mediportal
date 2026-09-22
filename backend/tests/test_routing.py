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


class _ThinkingOnlyMessages:
    """A reply that is nothing but a thinking block -- real Anthropic
    behaviour when the token budget is spent before any text is emitted."""

    def __init__(self, payload):
        self._payload = payload
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            msg = _FakeMessage(json.dumps(self._payload))
            msg.content = [type("Block", (), {"type": "thinking", "text": None})()]
            return msg
        return _FakeMessage(json.dumps(self._payload))


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


def test_match_issue_retries_when_reply_is_thinking_only(client, db, agent_headers, monkeypatch):
    """Regression test for a real dropped call: Claude returned a reply
    containing only a thinking block and no text, which raised and 503'd
    the endpoint -- the caller heard "something went wrong on our end" and
    the call ended. One retry recovers it."""
    term = _make_term(db)
    db.commit()
    payload = {
        "ortho_relevant": True,
        "matches": [{"term_id": term.id, "confidence": 0.92, "spoken_label": "a wrist fracture"}],
    }
    messages = _ThinkingOnlyMessages(payload)
    monkeypatch.setattr(
        routing_module,
        "_anthropic_client",
        lambda: type("C", (), {"messages": messages})(),
    )

    resp = client.post(
        "/api/v1/routing/match-issue",
        json={"complaint_text": "I hurt my wrist", "call_id": "vg_think"},
        headers=agent_headers,
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "matched"
    assert messages.calls == 2


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


def _make_doctor(db, first_name, last_name, specialty, practice, active=True, gender=None):
    doctor = Doctor(
        first_name=first_name, last_name=last_name, specialty=specialty, active=active, gender=gender
    )
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


def test_find_doctors_pins_to_preferred_doctor(client, db, agent_headers):
    """spec §5.1a: a caller who named a specific doctor gets that doctor
    pinned regardless of distance ranking -- find-doctor-by-name validates
    eligibility before this is ever called, so this just needs to resolve
    their real practice the same way the normal ranked path does."""
    term = _make_term(db)
    near = _make_practice(db, "Merrick", "11566", lat=40.6668, lon=-73.5502)
    far = _make_practice(db, "Port Jefferson", "11777", lat=40.9462, lon=-73.0704)
    doc_near = _make_doctor(db, "Alice", "Chen", "Hand & Wrist", near)
    doc_far = _make_doctor(db, "Bennett", "Brown", "Hand & Wrist", far)
    db.add(TermEligibility(term_id=term.id, doctor_id=doc_near.id, min_age=1, max_age=100))
    db.add(TermEligibility(term_id=term.id, doctor_id=doc_far.id, min_age=1, max_age=100))
    db.commit()

    resp = client.post(
        "/api/v1/routing/find-doctors",
        json={
            "term_id": term.id,
            "date_of_birth": "1991-04-02",
            "zip": "11566",
            "call_id": "vg_preferred",
            "preferred_doctor_id": doc_far.id,
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "matched"
    assert body["best_doctor_id"] == doc_far.id
    assert body["best_practice_id"] == far.id


def test_find_doctors_preferred_doctor_inactive_is_not_covered(client, db, agent_headers):
    term = _make_term(db)
    practice = _make_practice(db, "Merrick", "11566")
    doctor = _make_doctor(db, "Alice", "Chen", "Hand & Wrist", practice, active=False)
    db.commit()

    resp = client.post(
        "/api/v1/routing/find-doctors",
        json={
            "term_id": term.id,
            "date_of_birth": "1991-04-02",
            "call_id": "vg_preferred_inactive",
            "preferred_doctor_id": doctor.id,
        },
        headers=agent_headers,
    )
    assert resp.get_json()["status"] == "no_eligible_doctor"


def test_find_doctors_preferred_doctor_not_eligible_for_term(client, db, agent_headers):
    """Regression test: the pinned-doctor path used to only check
    doctor.active, silently booking a caller with a real, active doctor
    who doesn't treat this condition at all. Must run the same eligibility
    check the normal ranked path enforces, not trust the caller."""
    term = _make_term(db)
    practice = _make_practice(db, "Merrick", "11566")
    doctor = _make_doctor(db, "Alice", "Chen", "Hand & Wrist", practice)
    # Deliberately no TermEligibility row for (doctor, term).
    db.commit()

    resp = client.post(
        "/api/v1/routing/find-doctors",
        json={
            "term_id": term.id,
            "date_of_birth": "1991-04-02",
            "call_id": "vg_preferred_not_covered",
            "preferred_doctor_id": doctor.id,
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "no_eligible_doctor"
    assert body["reason"] == "not_covered"


def test_find_doctors_preferred_doctor_wrong_age(client, db, agent_headers):
    """A patient doesn't know a doctor's age restrictions -- pinning to a
    named doctor must not bypass age eligibility. Real age (35) outside
    this doctor's real range (0-17) must be caught, not silently booked."""
    term = _make_term(db)
    practice = _make_practice(db, "Merrick", "11566")
    doctor = _make_doctor(db, "Alice", "Chen", "Pediatrics", practice)
    db.add(TermEligibility(term_id=term.id, doctor_id=doctor.id, min_age=0, max_age=17))
    db.commit()

    resp = client.post(
        "/api/v1/routing/find-doctors",
        json={
            "term_id": term.id,
            "date_of_birth": "1991-04-02",  # ~35 years old
            "call_id": "vg_preferred_age",
            "preferred_doctor_id": doctor.id,
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "no_eligible_doctor"
    assert body["reason"] == "age_restricted"


def test_find_doctors_preferred_gender_filters_ranking(client, db, agent_headers):
    """A caller who prefers a specific gender doctor, with no specific
    person/office in mind, should only ever be ranked against doctors of
    that gender."""
    term = _make_term(db)
    practice = _make_practice(db, "Merrick", "11566")
    male_doctor = _make_doctor(db, "Bennett", "Brown", "Hand & Wrist", practice, gender="male")
    female_doctor = _make_doctor(db, "Alice", "Chen", "Hand & Wrist", practice, gender="female")
    db.add(TermEligibility(term_id=term.id, doctor_id=male_doctor.id, min_age=1, max_age=100))
    db.add(TermEligibility(term_id=term.id, doctor_id=female_doctor.id, min_age=1, max_age=100))
    db.commit()

    resp = client.post(
        "/api/v1/routing/find-doctors",
        json={
            "term_id": term.id,
            "date_of_birth": "1991-04-02",
            "call_id": "vg_gender1",
            "preferred_gender": "female",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "matched"
    assert body["best_doctor_id"] == female_doctor.id
    assert all(d["doctor_id"] == female_doctor.id for d in body["doctors"])


def test_find_doctors_preferred_gender_no_match_is_honest(client, db, agent_headers):
    """No doctor of the requested gender treats this -- an honest
    no_gender_match dead end, not a false 'nobody treats this' claim (we
    DO have an eligible doctor, just not of that gender)."""
    term = _make_term(db)
    practice = _make_practice(db, "Merrick", "11566")
    doctor = _make_doctor(db, "Bennett", "Brown", "Hand & Wrist", practice, gender="male")
    db.add(TermEligibility(term_id=term.id, doctor_id=doctor.id, min_age=1, max_age=100))
    db.commit()

    resp = client.post(
        "/api/v1/routing/find-doctors",
        json={
            "term_id": term.id,
            "date_of_birth": "1991-04-02",
            "call_id": "vg_gender2",
            "preferred_gender": "female",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "no_eligible_doctor"
    assert body["reason"] == "no_gender_match"


def test_find_doctors_unknown_gender_doctor_never_matches_preference(client, db, agent_headers):
    """A doctor with no inferred gender (gender IS NULL) must never match a
    gender preference, rather than being silently treated as a match."""
    term = _make_term(db)
    practice = _make_practice(db, "Merrick", "11566")
    doctor = _make_doctor(db, "Bennett", "Brown", "Hand & Wrist", practice, gender=None)
    db.add(TermEligibility(term_id=term.id, doctor_id=doctor.id, min_age=1, max_age=100))
    db.commit()

    resp = client.post(
        "/api/v1/routing/find-doctors",
        json={
            "term_id": term.id,
            "date_of_birth": "1991-04-02",
            "call_id": "vg_gender3",
            "preferred_gender": "male",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "no_eligible_doctor"
    assert body["reason"] == "no_gender_match"


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


# --- §5.1a find-doctor-by-name --------------------------------------------


def _mock_extraction(monkeypatch, doctor_name=None, practice_name=None, gender_preference=None):
    """Mocks the LLM name-extraction call find_doctor_by_name makes."""
    _mock_llm(
        monkeypatch,
        {"doctor_name": doctor_name, "practice_name": practice_name, "gender_preference": gender_preference},
    )


def test_find_doctor_by_name_matches_doctor_no_office_named(client, db, agent_headers, monkeypatch):
    term = _make_term(db)
    practice = _make_practice(db, "Melville", "11747", lat=40.79, lon=-73.42)
    doctor = _make_doctor(db, "Michael", "Fracchia", "Joint Reconstruction", practice)
    db.add(TermEligibility(term_id=term.id, doctor_id=doctor.id, min_age=1, max_age=100))
    db.commit()
    _mock_extraction(monkeypatch, doctor_name="Fracchia")

    resp = client.post(
        "/api/v1/routing/find-doctor-by-name",
        json={
            "doctor_office_text": "Dr. Fracchia",
            "term_id": term.id,
            "date_of_birth": "1970-01-01",
            "call_id": "vg_name1",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "matched"
    assert body["best_doctor_id"] == doctor.id


def test_find_doctor_by_name_matches_doctor_and_office(client, db, agent_headers, monkeypatch):
    term = _make_term(db)
    practice = _make_practice(db, "Melville", "11747", lat=40.79, lon=-73.42)
    doctor = _make_doctor(db, "Michael", "Fracchia", "Joint Reconstruction", practice)
    db.add(TermEligibility(term_id=term.id, doctor_id=doctor.id, min_age=1, max_age=100))
    db.commit()
    _mock_extraction(monkeypatch, doctor_name="Fracchia", practice_name="Melville")

    resp = client.post(
        "/api/v1/routing/find-doctor-by-name",
        json={
            "doctor_office_text": "Dr. Fracchia at Melville",
            "term_id": term.id,
            "date_of_birth": "1970-01-01",
            "call_id": "vg_name2",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "matched"
    assert body["best_doctor_id"] == doctor.id
    assert body["best_practice_id"] == practice.id


def test_find_doctor_by_name_office_only_no_doctor_named_pins_eligible_doctor(
    client, db, agent_headers, monkeypatch
):
    """A caller who names an office but no doctor must have that preference
    actually applied, not silently ignored in favor of the generic
    by-distance pick."""
    term = _make_term(db)
    melville = _make_practice(db, "Melville", "11747", lat=40.79, lon=-73.42)
    port_jeff = _make_practice(db, "Port Jefferson", "11777", lat=40.94, lon=-73.07)
    far_doctor = _make_doctor(db, "Michael", "Fracchia", "Joint Reconstruction", melville)
    office_doctor = _make_doctor(db, "Brian", "McGinley", "Joint Reconstruction", port_jeff)
    db.add(TermEligibility(term_id=term.id, doctor_id=far_doctor.id, min_age=1, max_age=100))
    db.add(TermEligibility(term_id=term.id, doctor_id=office_doctor.id, min_age=1, max_age=100))
    db.commit()
    _mock_extraction(monkeypatch, doctor_name=None, practice_name="Port Jefferson")

    resp = client.post(
        "/api/v1/routing/find-doctor-by-name",
        json={
            "doctor_office_text": "I'd like to be seen at your Port Jefferson office",
            "term_id": term.id,
            "date_of_birth": "1970-01-01",
            "call_id": "vg_office_only1",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "matched"
    assert body["best_doctor_id"] == office_doctor.id
    assert body["best_practice_id"] == port_jeff.id


def test_find_doctor_by_name_office_only_no_eligible_doctor_there_falls_back_honestly(
    client, db, agent_headers, monkeypatch
):
    """Office named, no doctor -- and nobody eligible practices there.
    Falls back to the normal ranked pick with a spoken reason naming the
    requested office, not a generic doctor-name note."""
    term = _make_term(db)
    melville = _make_practice(db, "Melville", "11747", lat=40.79, lon=-73.42)
    port_jeff = _make_practice(db, "Port Jefferson", "11777", lat=40.94, lon=-73.07)
    only_doctor = _make_doctor(db, "Michael", "Fracchia", "Joint Reconstruction", melville)
    db.add(TermEligibility(term_id=term.id, doctor_id=only_doctor.id, min_age=1, max_age=100))
    db.commit()
    _mock_extraction(monkeypatch, doctor_name=None, practice_name="Port Jefferson")

    resp = client.post(
        "/api/v1/routing/find-doctor-by-name",
        json={
            "doctor_office_text": "I'd like to be seen at your Port Jefferson office",
            "term_id": term.id,
            "date_of_birth": "1970-01-01",
            "call_id": "vg_office_only2",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "matched"
    assert body["best_doctor_id"] == only_doctor.id
    assert "Port Jefferson office" in body["spoken_response"]


def test_find_doctor_by_name_office_and_gender_both_named_but_only_office_satisfiable(
    client, db, agent_headers, monkeypatch
):
    """Office AND gender both stated, but the only eligible doctor at the
    requested office doesn't match the requested gender -- must still book
    that doctor (office wins) but say honestly that gender wasn't
    honored, never silently drop the gender preference."""
    term = _make_term(db)
    port_jeff = _make_practice(db, "Port Jefferson", "11777", lat=40.94, lon=-73.07)
    office_doctor = _make_doctor(
        db, "Brian", "McGinley", "Joint Reconstruction", port_jeff, gender="male"
    )
    db.add(TermEligibility(term_id=term.id, doctor_id=office_doctor.id, min_age=1, max_age=100))
    db.commit()
    _mock_extraction(monkeypatch, practice_name="Port Jefferson", gender_preference="female")

    resp = client.post(
        "/api/v1/routing/find-doctor-by-name",
        json={
            "doctor_office_text": "I'd like your Port Jefferson office, a woman doctor if possible",
            "term_id": term.id,
            "date_of_birth": "1970-01-01",
            "call_id": "vg_office_gender1",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "matched"
    assert body["best_doctor_id"] == office_doctor.id
    assert "that gender" in body["spoken_response"]
    assert "Port Jefferson" in body["spoken_response"]


def test_find_doctor_by_name_named_doctor_ineligible_at_requested_office_still_explains_why(
    client, db, agent_headers, monkeypatch
):
    """Named doctor AND office both stated, but the named doctor doesn't
    treat this at all. A substitute at the requested office is found --
    the substitution reason (named doctor doesn't treat this) must still
    be spoken, not silently dropped just because the office resolved."""
    term = _make_term(db)
    port_jeff = _make_practice(db, "Port Jefferson", "11777", lat=40.94, lon=-73.07)
    wrong_doctor = _make_doctor(db, "Michael", "Fracchia", "Joint Reconstruction", port_jeff)
    office_doctor = _make_doctor(db, "Brian", "McGinley", "Joint Reconstruction", port_jeff)
    db.add(TermEligibility(term_id=term.id, doctor_id=office_doctor.id, min_age=1, max_age=100))
    db.commit()
    _mock_extraction(monkeypatch, doctor_name="Fracchia", practice_name="Port Jefferson")

    resp = client.post(
        "/api/v1/routing/find-doctor-by-name",
        json={
            "doctor_office_text": "Dr. Fracchia at your Port Jefferson office",
            "term_id": term.id,
            "date_of_birth": "1970-01-01",
            "call_id": "vg_ineligible_office1",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "matched"
    assert body["best_doctor_id"] == office_doctor.id
    assert "Fracchia" in body["spoken_response"]
    assert "doesn't treat this" in body["spoken_response"]


def test_find_doctor_by_name_named_eligible_doctor_gender_mismatch_still_books_them(
    client, db, agent_headers, monkeypatch
):
    """Caller names a real, eligible doctor AND a gender preference that
    doctor doesn't match -- still books the named doctor (naming someone
    is a stronger signal than a general gender preference), but must say
    so honestly rather than silently ignoring the stated preference."""
    term = _make_term(db)
    practice = _make_practice(db, "Melville", "11747", lat=40.79, lon=-73.42)
    doctor = _make_doctor(db, "Michael", "Fracchia", "Joint Reconstruction", practice, gender="male")
    db.add(TermEligibility(term_id=term.id, doctor_id=doctor.id, min_age=1, max_age=100))
    db.commit()
    _mock_extraction(monkeypatch, doctor_name="Fracchia", gender_preference="female")

    resp = client.post(
        "/api/v1/routing/find-doctor-by-name",
        json={
            "doctor_office_text": "Dr. Fracchia, though I'd prefer a woman doctor",
            "term_id": term.id,
            "date_of_birth": "1970-01-01",
            "call_id": "vg_named_gender_mismatch1",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "matched"
    assert body["best_doctor_id"] == doctor.id
    assert "that gender" in body["spoken_response"]


def test_find_doctor_by_name_office_unmet_and_gender_unmet_mentions_both(
    client, db, agent_headers, monkeypatch
):
    """Office named, no doctor eligible there at all, AND gender
    preference also unmet -- both failures must be spoken, not just one."""
    term = _make_term(db)
    melville = _make_practice(db, "Melville", "11747", lat=40.79, lon=-73.42)
    port_jeff = _make_practice(db, "Port Jefferson", "11777", lat=40.94, lon=-73.07)
    only_doctor = _make_doctor(db, "Michael", "Fracchia", "Joint Reconstruction", melville, gender="male")
    db.add(TermEligibility(term_id=term.id, doctor_id=only_doctor.id, min_age=1, max_age=100))
    db.commit()
    _mock_extraction(monkeypatch, practice_name="Port Jefferson", gender_preference="female")

    resp = client.post(
        "/api/v1/routing/find-doctor-by-name",
        json={
            "doctor_office_text": "Your Port Jefferson office, a woman doctor please",
            "term_id": term.id,
            "date_of_birth": "1970-01-01",
            "call_id": "vg_both_unmet1",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "matched"
    assert body["best_doctor_id"] == only_doctor.id
    assert "Port Jefferson office" in body["spoken_response"]
    assert "that gender" in body["spoken_response"]


def test_find_doctor_by_name_gender_only_preference_filters_fallback(client, db, agent_headers, monkeypatch):
    """A caller who states only a gender preference (no doctor, no office
    named) must have that preference actually applied to the fallback
    pick, not silently ignored."""
    term = _make_term(db)
    practice = _make_practice(db, "Melville", "11747", lat=40.79, lon=-73.42)
    male_doctor = _make_doctor(db, "Michael", "Fracchia", "Joint Reconstruction", practice, gender="male")
    female_doctor = _make_doctor(db, "Rachel", "Chen", "Joint Reconstruction", practice, gender="female")
    db.add(TermEligibility(term_id=term.id, doctor_id=male_doctor.id, min_age=1, max_age=100))
    db.add(TermEligibility(term_id=term.id, doctor_id=female_doctor.id, min_age=1, max_age=100))
    db.commit()
    _mock_extraction(monkeypatch, gender_preference="female")

    resp = client.post(
        "/api/v1/routing/find-doctor-by-name",
        json={
            "doctor_office_text": "No one specific, but I'd prefer a woman doctor",
            "term_id": term.id,
            "date_of_birth": "1970-01-01",
            "call_id": "vg_gender_only1",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "matched"
    assert body["best_doctor_id"] == female_doctor.id


def test_find_doctor_by_name_gender_preference_unmet_falls_back_honestly(client, db, agent_headers, monkeypatch):
    """No eligible doctor of the requested gender -- an honest note, still
    booking with whoever is actually eligible rather than dead-ending."""
    term = _make_term(db)
    practice = _make_practice(db, "Melville", "11747", lat=40.79, lon=-73.42)
    only_doctor = _make_doctor(db, "Michael", "Fracchia", "Joint Reconstruction", practice, gender="male")
    db.add(TermEligibility(term_id=term.id, doctor_id=only_doctor.id, min_age=1, max_age=100))
    db.commit()
    _mock_extraction(monkeypatch, gender_preference="female")

    resp = client.post(
        "/api/v1/routing/find-doctor-by-name",
        json={
            "doctor_office_text": "No one specific, but I'd prefer a woman doctor",
            "term_id": term.id,
            "date_of_birth": "1970-01-01",
            "call_id": "vg_gender_only2",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "matched"
    assert body["best_doctor_id"] == only_doctor.id
    assert "that gender" in body["spoken_response"]


def test_find_doctor_by_name_doctor_not_at_requested_office_offers_both_options(
    client, db, agent_headers, monkeypatch
):
    """The real "call 3" case: caller wants a specific doctor AND office,
    but that doctor doesn't practice there. Must offer (a) that doctor at
    their real office and (b) a different eligible doctor who is really at
    the requested office -- never silently pick one or dead-end."""
    term = _make_term(db)
    melville = _make_practice(db, "Melville", "11747", lat=40.79, lon=-73.42)
    port_jeff = _make_practice(db, "Port Jefferson", "11777", lat=40.94, lon=-73.07)
    fracchia = _make_doctor(db, "Michael", "Fracchia", "Joint Reconstruction", melville)
    mcginley = _make_doctor(db, "Brian", "McGinley", "Joint Reconstruction", port_jeff)
    db.add(TermEligibility(term_id=term.id, doctor_id=fracchia.id, min_age=1, max_age=100))
    db.add(TermEligibility(term_id=term.id, doctor_id=mcginley.id, min_age=1, max_age=100))
    db.commit()
    _mock_extraction(monkeypatch, doctor_name="Fracchia", practice_name="Port Jefferson")

    resp = client.post(
        "/api/v1/routing/find-doctor-by-name",
        json={
            "doctor_office_text": "Dr. Fracchia at your Port Jefferson office",
            "term_id": term.id,
            "date_of_birth": "1970-01-01",
            "call_id": "vg_name3",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "needs_choice"
    assert body["requested_practice_id"] == port_jeff.id
    assert body["option_a_doctor_id"] == fracchia.id
    assert body["option_b_doctor_id"] == mcginley.id
    assert "Port Jefferson" in body["spoken_response"]


def test_find_doctor_by_name_needs_choice_prefers_gender_matching_option_b(
    client, db, agent_headers, monkeypatch
):
    """Doctor+office mismatch AND a gender preference -- when two eligible
    doctors exist at the requested office, option B must be the one
    matching the stated gender preference, not just whichever is found
    first, and no caveat is needed since it's actually satisfied."""
    term = _make_term(db)
    melville = _make_practice(db, "Melville", "11747", lat=40.79, lon=-73.42)
    port_jeff = _make_practice(db, "Port Jefferson", "11777", lat=40.94, lon=-73.07)
    fracchia = _make_doctor(db, "Michael", "Fracchia", "Joint Reconstruction", melville, gender="male")
    mcginley = _make_doctor(
        db, "Brian", "McGinley", "Joint Reconstruction", port_jeff, gender="male"
    )
    chen = _make_doctor(db, "Rachel", "Chen", "Joint Reconstruction", port_jeff, gender="female")
    db.add(TermEligibility(term_id=term.id, doctor_id=fracchia.id, min_age=1, max_age=100))
    db.add(TermEligibility(term_id=term.id, doctor_id=mcginley.id, min_age=1, max_age=100))
    db.add(TermEligibility(term_id=term.id, doctor_id=chen.id, min_age=1, max_age=100))
    db.commit()
    _mock_extraction(
        monkeypatch, doctor_name="Fracchia", practice_name="Port Jefferson", gender_preference="female"
    )

    resp = client.post(
        "/api/v1/routing/find-doctor-by-name",
        json={
            "doctor_office_text": "Dr. Fracchia at your Port Jefferson office, a woman doctor if not him",
            "term_id": term.id,
            "date_of_birth": "1970-01-01",
            "call_id": "vg_choice_gender1",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "needs_choice"
    assert body["option_b_doctor_id"] == chen.id
    assert "gender you mentioned" not in body["spoken_response"]


def test_find_doctor_by_name_needs_choice_honest_when_gender_unmet_by_both_options(
    client, db, agent_headers, monkeypatch
):
    """Doctor+office mismatch AND a gender preference neither offered
    option satisfies -- must say so, not silently drop it."""
    term = _make_term(db)
    melville = _make_practice(db, "Melville", "11747", lat=40.79, lon=-73.42)
    port_jeff = _make_practice(db, "Port Jefferson", "11777", lat=40.94, lon=-73.07)
    fracchia = _make_doctor(db, "Michael", "Fracchia", "Joint Reconstruction", melville, gender="male")
    mcginley = _make_doctor(
        db, "Brian", "McGinley", "Joint Reconstruction", port_jeff, gender="male"
    )
    db.add(TermEligibility(term_id=term.id, doctor_id=fracchia.id, min_age=1, max_age=100))
    db.add(TermEligibility(term_id=term.id, doctor_id=mcginley.id, min_age=1, max_age=100))
    db.commit()
    _mock_extraction(
        monkeypatch, doctor_name="Fracchia", practice_name="Port Jefferson", gender_preference="female"
    )

    resp = client.post(
        "/api/v1/routing/find-doctor-by-name",
        json={
            "doctor_office_text": "Dr. Fracchia at your Port Jefferson office, a woman doctor if not him",
            "term_id": term.id,
            "date_of_birth": "1970-01-01",
            "call_id": "vg_choice_gender2",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "needs_choice"
    assert body["option_b_doctor_id"] == mcginley.id
    assert "gender you mentioned" in body["spoken_response"]


def test_find_doctor_by_name_mismatch_with_no_alternate_doctor_still_offers_own_office(
    client, db, agent_headers, monkeypatch
):
    """Same mismatch, but nobody else treats this at the requested office --
    still offer the one real fix instead of dead-ending."""
    term = _make_term(db)
    melville = _make_practice(db, "Melville", "11747", lat=40.79, lon=-73.42)
    port_jeff = _make_practice(db, "Port Jefferson", "11777", lat=40.94, lon=-73.07)
    fracchia = _make_doctor(db, "Michael", "Fracchia", "Joint Reconstruction", melville)
    db.add(TermEligibility(term_id=term.id, doctor_id=fracchia.id, min_age=1, max_age=100))
    db.commit()
    _mock_extraction(monkeypatch, doctor_name="Fracchia", practice_name="Port Jefferson")

    resp = client.post(
        "/api/v1/routing/find-doctor-by-name",
        json={
            "doctor_office_text": "Dr. Fracchia at your Port Jefferson office",
            "term_id": term.id,
            "date_of_birth": "1970-01-01",
            "call_id": "vg_name4",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "needs_choice"
    assert body["option_a_doctor_id"] == fracchia.id
    assert body["option_b_doctor_id"] is None


def test_find_doctor_by_name_doctor_ineligible_for_term_falls_back(client, db, agent_headers, monkeypatch):
    """A named doctor who doesn't treat this condition at all shouldn't
    dead-end the call -- fall back to whoever does."""
    term = _make_term(db)
    practice = _make_practice(db, "Melville", "11747", lat=40.79, lon=-73.42)
    wrong_doctor = _make_doctor(db, "Michael", "Fracchia", "Joint Reconstruction", practice)
    right_doctor = _make_doctor(db, "Rasel", "Rana", "Spine", practice)
    db.add(TermEligibility(term_id=term.id, doctor_id=right_doctor.id, min_age=1, max_age=100))
    db.commit()
    _mock_extraction(monkeypatch, doctor_name="Fracchia")

    resp = client.post(
        "/api/v1/routing/find-doctor-by-name",
        json={
            "doctor_office_text": "Dr. Fracchia",
            "term_id": term.id,
            "date_of_birth": "1970-01-01",
            "call_id": "vg_name5",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "matched"
    assert body["best_doctor_id"] == right_doctor.id
    assert "doesn't treat this" in body["spoken_response"]


def test_find_doctor_by_name_doctor_wrong_age_falls_back_with_specific_reason(
    client, db, agent_headers, monkeypatch
):
    """A patient doesn't know a doctor's age restrictions -- when that's
    the real reason a named doctor can't see them, say so explicitly
    rather than the generic (and here inaccurate) "doesn't treat this"."""
    term = _make_term(db)
    practice = _make_practice(db, "Melville", "11747", lat=40.79, lon=-73.42)
    peds_doctor = _make_doctor(db, "Michael", "Fracchia", "Pediatrics", practice)
    adult_doctor = _make_doctor(db, "Rasel", "Rana", "Spine", practice)
    db.add(TermEligibility(term_id=term.id, doctor_id=peds_doctor.id, min_age=0, max_age=17))
    db.add(TermEligibility(term_id=term.id, doctor_id=adult_doctor.id, min_age=1, max_age=100))
    db.commit()
    _mock_extraction(monkeypatch, doctor_name="Fracchia")

    resp = client.post(
        "/api/v1/routing/find-doctor-by-name",
        json={
            "doctor_office_text": "Dr. Fracchia",
            "term_id": term.id,
            "date_of_birth": "1970-01-01",  # adult -- outside peds_doctor's 0-17 range
            "call_id": "vg_name_age",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "matched"
    assert body["best_doctor_id"] == adult_doctor.id
    assert "your age" in body["spoken_response"]
    assert "doesn't treat this" not in body["spoken_response"]


def test_find_doctor_by_name_no_name_extracted_falls_back_to_top_ranked(client, db, agent_headers, monkeypatch):
    term = _make_term(db)
    practice = _make_practice(db, "Melville", "11747", lat=40.79, lon=-73.42)
    doctor = _make_doctor(db, "Rasel", "Rana", "Spine", practice)
    db.add(TermEligibility(term_id=term.id, doctor_id=doctor.id, min_age=1, max_age=100))
    db.commit()
    _mock_extraction(monkeypatch, doctor_name=None, practice_name=None)

    resp = client.post(
        "/api/v1/routing/find-doctor-by-name",
        json={
            "doctor_office_text": "um, not sure, whoever's available",
            "term_id": term.id,
            "date_of_birth": "1970-01-01",
            "call_id": "vg_name6",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "matched"
    assert body["best_doctor_id"] == doctor.id


def test_find_doctor_by_name_requires_agent_key(client, db):
    resp = client.post(
        "/api/v1/routing/find-doctor-by-name",
        json={"doctor_office_text": "Dr. Fracchia", "term_id": 1, "date_of_birth": "1970-01-01", "call_id": "vg_x"},
    )
    assert resp.status_code == 401
