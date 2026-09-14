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
    def __init__(self, text):
        self.content = [type("Block", (), {"text": text})()]


class _FakeMessages:
    def __init__(self, payload):
        self._payload = payload

    def create(self, **kwargs):
        return _FakeMessage(json.dumps(self._payload))


class _FakeClient:
    def __init__(self, payload):
        self.messages = _FakeMessages(payload)


def _mock_llm(monkeypatch, payload):
    monkeypatch.setattr(routing_module, "_anthropic_client", lambda: _FakeClient(payload))


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
