# Tests for GET /availability (spec §5.5) -- urgency-window widening and
# auth gating.
from datetime import datetime, timedelta, timezone

from .factories import make_doctor, make_practice, make_slot


def test_requires_agent_key(client):
    resp = client.get("/api/v1/availability?doctor_id=1")
    assert resp.status_code == 401


def test_no_slots(client, db, agent_headers):
    doctor = make_doctor(db)
    resp = client.get(f"/api/v1/availability?doctor_id={doctor.id}", headers=agent_headers)
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "no_slots"


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
