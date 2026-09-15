# Tests for POST /appointments (spec §5.6) -- successful booking, the
# 409 slot-race path, and auth gating.
from datetime import timedelta

from .factories import make_doctor, make_patient, make_practice, make_slot, make_term


def test_requires_agent_key(client):
    resp = client.post("/api/v1/appointments", json={})
    assert resp.status_code == 401


def test_book_appointment_success(client, db, agent_headers):
    doctor = make_doctor(db)
    practice = make_practice(db)
    term = make_term(db)
    patient = make_patient(db)
    # new_patient_consult is 40 min -- two contiguous 20-min grid slots
    # (spec §8.2), so book_slot needs the second one open too.
    slot = make_slot(doctor, practice, db)
    make_slot(doctor, practice, db, start_time=slot.end_time)

    resp = client.post(
        "/api/v1/appointments",
        headers=agent_headers,
        json={"slot_id": slot.id, "patient_id": patient.id, "term_id": term.id},
    )
    body = resp.get_json()

    assert resp.status_code == 201
    assert body["status"] == "scheduled"
    assert body["appointment_id"]
    confirmation = body["confirmation"]
    assert confirmation["doctor"] == f"Dr. {doctor.first_name} {doctor.last_name}"
    assert confirmation["practice"] == practice.name
    assert confirmation["appointment_type"] == "New patient consultation"
    assert "arrive_minutes_early" not in confirmation


def test_book_appointment_slot_taken_returns_409_with_alternates(client, db, agent_headers):
    doctor = make_doctor(db)
    practice = make_practice(db)
    # follow_up is a single 20-min grid slot -- duration isn't what this
    # test is about, so keep the fixture to one slot per offer.
    term = make_term(db, default_appointment_type="follow_up")
    patient = make_patient(db)
    slot = make_slot(doctor, practice, db)
    other_slot = make_slot(doctor, practice, db, start_time=slot.start_time + timedelta(days=1))
    # Race: slot gets taken between the offer and this booking attempt.
    slot.status = "booked"
    db.flush()

    resp = client.post(
        "/api/v1/appointments",
        headers=agent_headers,
        json={"slot_id": slot.id, "patient_id": patient.id, "term_id": term.id},
    )
    body = resp.get_json()

    assert resp.status_code == 409
    assert body["status"] == "slot_taken"
    assert any(alt["slot_id"] == other_slot.id for alt in body["alternate_slots"])


def test_book_appointment_missing_fields_400(client, agent_headers):
    resp = client.post("/api/v1/appointments", headers=agent_headers, json={})
    assert resp.status_code == 400


def test_book_appointment_falls_back_to_patient_and_term_recorded_on_call(
    client, db, agent_headers
):
    """The flow may reach booking without explicitly passing patient_id/
    term_id (e.g. after a confirm-by-readback path) -- falls back to
    whatever update_call already recorded on the call record."""
    from app.models import Call

    doctor = make_doctor(db)
    practice = make_practice(db)
    term = make_term(db, default_appointment_type="follow_up")
    patient = make_patient(db)
    slot = make_slot(doctor, practice, db)
    db.add(Call(vogent_call_id="vg_fallback_book", patient_id=patient.id, matched_term_id=term.id))
    db.commit()

    resp = client.post(
        "/api/v1/appointments",
        headers=agent_headers,
        json={"slot_id": slot.id, "call_id": "vg_fallback_book"},
    )
    body = resp.get_json()
    assert resp.status_code == 201
    assert body["status"] == "scheduled"
