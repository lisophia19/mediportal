# Tests for backend/app/blueprints/imaging.py -- MRI location/availability/
# booking, keyed to a practice's machine, never a doctor.
from datetime import date, datetime, timedelta, timezone

from app.models import Call, ImagingAppointment, ImagingSlot

from .factories import make_imaging_slot, make_patient, make_practice


def test_find_location_picks_nearest_mri_capable_practice(client, db, agent_headers):
    from .test_routing import _make_practice  # reuses lat/lon + ZipCentroid seeding

    near = _make_practice(db, "Merrick", "11566", lat=40.6668, lon=-73.5502, main_phone="516-555-9000")
    far = _make_practice(db, "Port Jefferson", "11777", lat=40.9462, lon=-73.0704, main_phone="516-555-9001")
    near.has_mri = True
    far.has_mri = True
    from app.models import ZipCentroid

    db.add(ZipCentroid(zip="11563", latitude=40.6551, longitude=-73.6768, label="Lynbrook"))
    db.commit()

    resp = client.post(
        "/api/v1/imaging/find-location",
        headers=agent_headers,
        json={"modality": "MRI", "zip": "11563"},
    )
    body = resp.get_json()
    assert body["status"] == "matched"
    assert body["best_practice_id"] == near.id


def test_find_location_excludes_non_mri_practices(client, db, agent_headers):
    make_practice(db, has_mri=False)
    resp = client.post("/api/v1/imaging/find-location", headers=agent_headers, json={"modality": "MRI"})
    assert resp.get_json()["status"] == "no_location"


def test_availability_returns_open_slots_only(client, db, agent_headers):
    practice = make_practice(db, has_mri=True)
    make_imaging_slot(practice, db, status="booked")
    open_slot = make_imaging_slot(practice, db, status="open")
    db.commit()

    resp = client.post(
        "/api/v1/imaging/availability", headers=agent_headers, json={"practice_id": practice.id}
    )
    body = resp.get_json()
    assert body["status"] == "slots_available"
    assert [s["slot_id"] for s in body["slots"]] == [open_slot.id]


def test_availability_no_slots(client, db, agent_headers):
    practice = make_practice(db, has_mri=True)
    db.commit()
    resp = client.post(
        "/api/v1/imaging/availability", headers=agent_headers, json={"practice_id": practice.id}
    )
    assert resp.get_json()["status"] == "no_slots"


def test_book_imaging_slot(client, db, agent_headers):
    practice = make_practice(db, has_mri=True)
    slot = make_imaging_slot(practice, db)
    patient = make_patient(db)
    call = Call(vogent_call_id="vg_imaging_1")
    db.add(call)
    db.commit()

    resp = client.post(
        "/api/v1/imaging/book",
        headers=agent_headers,
        json={"slot_id": slot.id, "patient_id": patient.id, "call_id": "vg_imaging_1"},
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["status"] == "scheduled"
    assert body["confirmation"]["practice"] == practice.name

    updated_slot = db.get(ImagingSlot, slot.id)
    assert updated_slot.status == "booked"
    appointment = ImagingAppointment.query.filter_by(slot_id=slot.id).first()
    assert appointment.patient_id == patient.id
    assert appointment.call_id == call.id


def test_book_imaging_slot_already_taken(client, db, agent_headers):
    practice = make_practice(db, has_mri=True)
    slot = make_imaging_slot(practice, db, status="booked")
    patient = make_patient(db)
    db.commit()

    resp = client.post(
        "/api/v1/imaging/book",
        headers=agent_headers,
        json={"slot_id": slot.id, "patient_id": patient.id},
    )
    assert resp.status_code == 409
    assert resp.get_json()["status"] == "slot_taken"


def test_book_imaging_requires_agent_key(client, db):
    resp = client.post("/api/v1/imaging/book", json={"slot_id": 1, "patient_id": 1})
    assert resp.status_code == 401
