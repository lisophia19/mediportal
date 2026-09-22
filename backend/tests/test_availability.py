# Tests for GET /availability (spec §5.5) -- urgency-window widening and
# auth gating.
from datetime import datetime, timedelta, timezone

from app.models import AppointmentSlot, Call, DoctorPractice

from app.seed.availability import generate_availability

from .factories import make_doctor, make_eligibility, make_patient, make_practice, make_slot, make_term


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


def test_falls_back_to_other_practice_when_preferred_has_no_slots(client, db, agent_headers):
    """Regression test for a real dropped call: find_doctors returns the
    doctor's CLOSEST practice, which is not necessarily where they have
    open time. Hard-filtering on it made a doctor with real availability
    look fully booked -- the caller was told nothing was available, then
    that no other doctor could help either."""
    doctor = make_doctor(db)
    nearest = make_practice(db)
    other = make_practice(db)
    # Availability exists only at the farther office.
    make_slot(doctor, other, db, start_time=datetime.now(timezone.utc) + timedelta(days=1))

    resp = client.post(
        "/api/v1/availability",
        headers=agent_headers,
        json={"doctor_id": doctor.id, "practice_id": nearest.id},
    )
    body = resp.get_json()

    assert body["status"] == "slots_available"
    assert body["slots"][0]["practice_name"] == other.name


def test_preferred_practice_still_wins_when_it_has_slots(client, db, agent_headers):
    doctor = make_doctor(db)
    nearest = make_practice(db)
    other = make_practice(db)
    make_slot(doctor, nearest, db, start_time=datetime.now(timezone.utc) + timedelta(days=1))
    make_slot(doctor, other, db, start_time=datetime.now(timezone.utc) + timedelta(days=2))

    body = client.post(
        "/api/v1/availability",
        headers=agent_headers,
        json={"doctor_id": doctor.id, "practice_id": nearest.id},
    ).get_json()

    assert {s["practice_name"] for s in body["slots"]} == {nearest.name}


def test_practice_fallback_reports_the_office_that_changed(client, db, agent_headers):
    """Regression test for a real demo call: the caller was offered times
    "at our Melville office about 11.3 miles from you" and then booked into
    Riverhead -- they would have driven to the wrong address. When the
    fallback moves offices, say which one so the flow can tell the caller."""
    doctor = make_doctor(db)
    nearest = make_practice(db)
    other = make_practice(db)
    make_slot(doctor, other, db, start_time=datetime.now(timezone.utc) + timedelta(days=1))

    body = client.post(
        "/api/v1/availability",
        headers=agent_headers,
        json={"doctor_id": doctor.id, "practice_id": nearest.id},
    ).get_json()

    assert body["different_practice_name"] == other.name


def test_no_office_change_reported_when_preferred_practice_used(client, db, agent_headers):
    doctor = make_doctor(db)
    nearest = make_practice(db)
    make_slot(doctor, nearest, db, start_time=datetime.now(timezone.utc) + timedelta(days=1))

    body = client.post(
        "/api/v1/availability",
        headers=agent_headers,
        json={"doctor_id": doctor.id, "practice_id": nearest.id},
    ).get_json()

    assert "different_practice_name" not in body


# --- generate_availability / _fallback_template (seed-time logic) ----------


def test_generate_availability_skips_non_eligible_doctor(db):
    """A doctor with real seeded practices but zero term_eligibility rows
    must get no schedule at all -- the eligibility check has to run before
    any fallback-template work, not after."""
    doctor = make_doctor(db, last_name="NoEligibility")
    practice = make_practice(db)
    db.add(DoctorPractice(doctor_id=doctor.id, practice_id=practice.id, is_primary=True))
    db.flush()

    generate_availability(
        {"NoEligibility, Test": doctor},
        {(practice.name, practice.address): practice},
        [make_patient(db)],
    )

    assert AppointmentSlot.query.filter_by(doctor_id=doctor.id).count() == 0


def test_generate_availability_fallback_template_uses_real_practices(db):
    """An eligible doctor with no hand-curated WEEKLY_TEMPLATES entry must
    still get bookable slots, generated from their own real seeded
    practices via the round-robin fallback."""
    doctor = make_doctor(db, last_name="NoTemplate")
    term = make_term(db)
    make_eligibility(term, doctor, db)
    practice = make_practice(db)
    db.add(DoctorPractice(doctor_id=doctor.id, practice_id=practice.id, is_primary=True))
    db.flush()

    generate_availability(
        {"NoTemplate, Test": doctor},
        {(practice.name, practice.address): practice},
        [make_patient(db)],
    )

    slots = AppointmentSlot.query.filter_by(doctor_id=doctor.id).all()
    assert slots
    assert all(slot.practice_id == practice.id for slot in slots)
