# Tests for backend/app/blueprints/patients.py -- spec §5.3 (lookup) and
# §5.4 (create). Fixtures are built directly against the `db` session rather
# than the full seed script, per the Phase 1 task instructions.
from datetime import date

from app.models import Call, Patient


def _make_patient(db, **overrides):
    defaults = dict(
        first_name="Maria",
        last_name="Rodriguez",
        date_of_birth=date(1991, 4, 2),
        phone="516-555-0100",
        home_zip="11563",
    )
    defaults.update(overrides)
    patient = Patient(**defaults)
    db.add(patient)
    db.flush()
    return patient


def _make_call(db, vogent_call_id="vg_test1", caller_phone=None):
    call = Call(vogent_call_id=vogent_call_id, caller_phone=caller_phone)
    db.add(call)
    db.flush()
    return call


def test_lookup_not_found(client, db, agent_headers):
    resp = client.post(
        "/api/v1/patients/lookup",
        json={"last_name": "Nobody", "date_of_birth": "1990-01-01", "call_id": "vg_x"},
        headers=agent_headers,
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "not_found"}


def test_lookup_found_single_match(client, db, agent_headers):
    _make_patient(db)
    db.commit()

    resp = client.post(
        "/api/v1/patients/lookup",
        json={"last_name": "rodriguez", "date_of_birth": "1991-04-02", "call_id": "vg_x"},
        headers=agent_headers,
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["status"] == "found"
    assert body["patient"]["last_name"] == "Rodriguez"


def test_lookup_ambiguous_resolved_by_caller_id(client, db, agent_headers):
    # Seeded Smith/Smith collision pair -- same last name and DOB, different
    # first names and phone numbers (spec §8.4).
    _make_patient(db, first_name="Jonathan", last_name="Smith", phone="516-555-0001")
    _make_patient(db, first_name="John", last_name="Smith", phone="516-555-0002")
    _make_call(db, vogent_call_id="vg_phone_match", caller_phone="516-555-0002")
    db.commit()

    resp = client.post(
        "/api/v1/patients/lookup",
        json={
            "last_name": "Smith",
            "date_of_birth": "1991-04-02",
            "call_id": "vg_phone_match",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "found"
    assert body["patient"]["first_name"] == "John"


def test_lookup_ambiguous_resolved_by_first_name(client, db, agent_headers):
    _make_patient(db, first_name="Jonathan", last_name="Smith", phone="516-555-0001")
    _make_patient(db, first_name="Katherine", last_name="Smith", phone="516-555-0002")
    # Caller ID doesn't match either patient on file.
    _make_call(db, vogent_call_id="vg_name_score", caller_phone="516-555-9999")
    db.commit()

    resp = client.post(
        "/api/v1/patients/lookup",
        json={
            "last_name": "Smith",
            "date_of_birth": "1991-04-02",
            "first_name": "Jon",
            "call_id": "vg_name_score",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "found"
    assert body["patient"]["first_name"] == "Jonathan"


def test_lookup_confirm_then_ambiguous_unresolved(client, db, agent_headers):
    p1 = _make_patient(db, first_name="Pat", last_name="Nguyen", phone="516-555-0011")
    p2 = _make_patient(db, first_name="Pat", last_name="Nguyen", phone="516-555-0012")
    _make_call(db, vogent_call_id="vg_confirm", caller_phone="516-555-9999")
    db.commit()

    resp = client.post(
        "/api/v1/patients/lookup",
        json={
            "last_name": "Nguyen",
            "date_of_birth": "1991-04-02",
            "first_name": "Pat",
            "call_id": "vg_confirm",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert body["status"] == "confirm"
    assert body["candidate_patient_id"] in (p1.id, p2.id)

    # Caller says "no" to the offered candidate -- flow re-calls excluding it.
    resp2 = client.post(
        "/api/v1/patients/lookup",
        json={
            "last_name": "Nguyen",
            "date_of_birth": "1991-04-02",
            "first_name": "Pat",
            "call_id": "vg_confirm",
            "excluded_patient_ids": [body["candidate_patient_id"]],
        },
        headers=agent_headers,
    )
    body2 = resp2.get_json()
    assert body2["status"] == "found"

    # Reject both candidates -- exhausted, genuine dead end.
    resp3 = client.post(
        "/api/v1/patients/lookup",
        json={
            "last_name": "Nguyen",
            "date_of_birth": "1991-04-02",
            "first_name": "Pat",
            "call_id": "vg_confirm",
            "excluded_patient_ids": [p1.id, p2.id],
        },
        headers=agent_headers,
    )
    assert resp3.get_json()["status"] == "ambiguous_unresolved"


def test_create_patient(client, db, agent_headers):
    resp = client.post(
        "/api/v1/patients",
        json={
            "first_name": "New",
            "last_name": "Patient",
            "date_of_birth": "1985-06-15",
            "phone": "516-555-0200",
            "home_zip": "11563",
        },
        headers=agent_headers,
    )
    body = resp.get_json()
    assert resp.status_code == 201
    assert body["status"] == "created"
    assert body["patient"]["last_name"] == "Patient"
    assert body["patient"]["id"] is not None


def test_create_patient_requires_minimum_fields(client, db, agent_headers):
    resp = client.post(
        "/api/v1/patients",
        json={"first_name": "Missing", "last_name": "Phone", "date_of_birth": "1985-06-15"},
        headers=agent_headers,
    )
    assert resp.status_code == 400


def test_confirm_patient_resolves_candidate_id_to_found_shape(client, db, agent_headers):
    patient = _make_patient(db)
    resp = client.post(
        "/api/v1/patients/confirm", headers=agent_headers, json={"patient_id": patient.id}
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["status"] == "found"
    assert body["patient"]["id"] == patient.id


def test_confirm_patient_unknown_id_returns_not_found(client, agent_headers):
    resp = client.post(
        "/api/v1/patients/confirm", headers=agent_headers, json={"patient_id": 999999}
    )
    assert resp.get_json()["status"] == "not_found"


def test_lookup_requires_agent_key(client, db):
    resp = client.post(
        "/api/v1/patients/lookup",
        json={"last_name": "Rodriguez", "date_of_birth": "1991-04-02", "call_id": "vg_x"},
    )
    assert resp.status_code == 401


def test_update_zip_via_explicit_patient_id(client, db, agent_headers):
    patient = _make_patient(db, home_zip=None)
    resp = client.post(
        "/api/v1/patients/update-zip",
        headers=agent_headers,
        json={"patient_id": patient.id, "zip": "10001"},
    )
    assert resp.status_code == 200
    assert db.get(Patient, patient.id).home_zip == "10001"


def test_update_zip_falls_back_to_call_patient_id(client, db, agent_headers):
    """Regression test: the flow asks ZIP after patient lookup/creation, so
    this route (called from that later point in the call) must resolve
    which patient to update from the call record, same as every other
    late-in-call endpoint that can't rely on the flow re-passing an id."""
    patient = _make_patient(db, home_zip=None)
    call = _make_call(db, vogent_call_id="vg_zip")
    call.patient_id = patient.id
    db.commit()

    resp = client.post(
        "/api/v1/patients/update-zip",
        headers=agent_headers,
        json={"call_id": "vg_zip", "zip": "07030"},
    )
    assert resp.status_code == 200
    assert db.get(Patient, patient.id).home_zip == "07030"


def test_update_zip_requires_patient_and_zip(client, agent_headers):
    resp = client.post(
        "/api/v1/patients/update-zip", headers=agent_headers, json={"zip": "10001"}
    )
    assert resp.status_code == 400


def test_create_patient_is_idempotent_on_phone_and_dob(client, db, agent_headers):
    """Regression test: a caller correcting one detail sent the flow back
    through intake, which called create twice and left three near-identical
    rows for one person. Name transcription varies call to call, so the
    number they just gave plus DOB is the reliable key."""
    first = client.post(
        "/api/v1/patients",
        headers=agent_headers,
        json={
            "first_name": "Jordan",
            "last_name": "Testcaller",
            "date_of_birth": "1970-05-03",
            "phone": "703-555-0168",
        },
    ).get_json()

    # Same person, same number typed differently, name misheard.
    second = client.post(
        "/api/v1/patients",
        headers=agent_headers,
        json={
            "first_name": "Jordan",
            "last_name": "Tess Koller",
            "date_of_birth": "1970-05-03",
            "phone": "7035550168",
        },
    ).get_json()

    assert first["patient"]["id"] == second["patient"]["id"]
    assert Patient.query.filter_by(date_of_birth=date(1970, 5, 3)).count() == 1


def test_create_patient_still_separates_different_people(client, db, agent_headers):
    """Same DOB but a different phone is a different person -- the seeded
    John/Jonathan Smith pair share a birthday."""
    client.post(
        "/api/v1/patients",
        headers=agent_headers,
        json={"first_name": "John", "last_name": "Smith", "date_of_birth": "1985-05-12", "phone": "516-555-0101"},
    )
    client.post(
        "/api/v1/patients",
        headers=agent_headers,
        json={"first_name": "Jonathan", "last_name": "Smith", "date_of_birth": "1985-05-12", "phone": "631-555-0177"},
    )
    assert Patient.query.filter_by(date_of_birth=date(1985, 5, 12)).count() == 2


def test_update_patient_name_applies_correction_via_call_id(client, db, agent_headers):
    patient = _make_patient(db, first_name="Jordan", last_name="Tess Koller")
    call = _make_call(db, vogent_call_id="vg_name")
    call.patient_id = patient.id
    db.commit()

    resp = client.post(
        "/api/v1/patients/update-name",
        headers=agent_headers,
        json={"call_id": "vg_name", "full_name": "Jordan Testcaller"},
    )
    assert resp.status_code == 200
    updated = db.get(Patient, patient.id)
    assert (updated.first_name, updated.last_name) == ("Jordan", "Testcaller")
