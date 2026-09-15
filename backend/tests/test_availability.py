# Tests for GET /availability (spec §5.5) -- urgency-window widening and
# auth gating.
from datetime import datetime, timedelta, timezone

from app.models import Call

from .factories import make_doctor, make_practice, make_slot, make_term


def test_requires_agent_key(client):
    resp = client.get("/api/v1/availability?doctor_id=1")
    assert resp.status_code == 401


def test_no_slots(client, db, agent_headers):
    doctor = make_doctor(db)
    resp = client.get(f"/api/v1/availability?doctor_id={doctor.id}", headers=agent_headers)
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "no_slots"


def test_post_body_works_same_as_get_query_params(client, db, agent_headers):
    """Vogent's function-calling always POSTs a JSON body, never query
    params -- this endpoint must accept both the same way."""
    doctor = make_doctor(db)
    practice = make_practice(db)
    make_slot(doctor, practice, db, start_time=datetime.now(timezone.utc) + timedelta(days=1))

    resp = client.post(
        "/api/v1/availability", headers=agent_headers, json={"doctor_id": doctor.id}
    )
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["status"] == "slots_available"
    assert len(body["slots"]) == 1


def test_normal_window_returns_slots_without_urgent_flag(client, db, agent_headers):
    doctor = make_doctor(db)
    practice = make_practice(db)
    make_slot(doctor, practice, db, start_time=datetime.now(timezone.utc) + timedelta(days=1))

    resp = client.get(f"/api/v1/availability?doctor_id={doctor.id}", headers=agent_headers)
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["status"] == "slots_available"
    assert "urgent_window_met" not in body
    assert len(body["slots"]) == 1
    assert "spoken_label" in body["slots"][0]


def test_urgent_window_met_within_3_days(client, db, agent_headers):
    doctor = make_doctor(db)
    practice = make_practice(db)
    make_slot(doctor, practice, db, start_time=datetime.now(timezone.utc) + timedelta(days=2))

    resp = client.get(
        f"/api/v1/availability?doctor_id={doctor.id}&urgency=URGENT", headers=agent_headers
    )
    body = resp.get_json()

    assert body["status"] == "slots_available"
    assert body["urgent_window_met"] is True


def test_urgent_window_widens_when_unmet(client, db, agent_headers):
    doctor = make_doctor(db)
    practice = make_practice(db)
    # Only a slot outside the 3-day urgent window exists.
    make_slot(doctor, practice, db, start_time=datetime.now(timezone.utc) + timedelta(days=10))

    resp = client.get(
        f"/api/v1/availability?doctor_id={doctor.id}&urgency=URGENT", headers=agent_headers
    )
    body = resp.get_json()

    assert body["status"] == "slots_available"
    assert body["urgent_window_met"] is False
    assert len(body["slots"]) == 1


def test_falls_back_to_call_term_appointment_type_for_slot_sizing(client, db, agent_headers):
    """Regression test for a real production booking failure: this route
    offered a single 20-min slot as sufficient (appointment_type was never
    passed explicitly), but book_slot independently derives the appointment
    type from the term recorded on the call and required 2 contiguous slots
    for "urgent" -- the caller was offered a slot that then failed as
    "taken" at booking time, when the real issue was never enough
    contiguous capacity in the first place. This route must agree with
    book_slot up front by resolving the same term via call_id."""
    doctor = make_doctor(db)
    practice = make_practice(db)
    term = make_term(db, default_appointment_type="urgent")  # needs 2 contiguous 20-min slots
    db.add(Call(vogent_call_id="vg_sizing", matched_term_id=term.id))
    db.commit()

    # Only a single, non-contiguous slot exists -- not enough for "urgent".
    make_slot(doctor, practice, db, start_time=datetime.now(timezone.utc) + timedelta(hours=1))

    resp = client.get(
        f"/api/v1/availability?doctor_id={doctor.id}&call_id=vg_sizing", headers=agent_headers
    )
    body = resp.get_json()

    assert body["status"] == "no_slots"
