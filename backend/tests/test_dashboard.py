# Tests for dashboard auth (spec §5.8, auth.py) and call list/detail
# (dashboard.py) -- JWT-gated, distinct audience from the Vogent-facing
# calls_bp.
from datetime import datetime, timedelta, timezone

from app.models import Call

from .factories import (
    make_appointment,
    make_doctor,
    make_patient,
    make_practice,
    make_slot,
    make_term,
    make_user,
)


def _login(client, email="dashboard@test.example", password="test-password"):
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return resp


def test_login_success_returns_token(client, db):
    make_user(db)
    resp = _login(client)
    assert resp.status_code == 200
    assert resp.get_json()["token"]


def test_login_failure_wrong_password(client, db):
    make_user(db)
    resp = _login(client, password="wrong")
    assert resp.status_code == 401


def test_login_failure_unknown_email(client, db):
    resp = client.post(
        "/api/v1/auth/login", json={"email": "nobody@test.example", "password": "x"}
    )
    assert resp.status_code == 401


def test_logout_requires_token(client):
    resp = client.post("/api/v1/auth/logout")
    assert resp.status_code == 401


def test_logout_success(client, db):
    make_user(db)
    token = _login(client).get_json()["token"]
    resp = client.post(
        "/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200


def test_calls_list_requires_jwt(client):
    resp = client.get("/api/v1/calls")
    assert resp.status_code == 401


def test_calls_list_and_detail(client, db):
    make_user(db)
    token = _login(client).get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    doctor = make_doctor(db)
    practice = make_practice(db)
    term = make_term(db)
    patient = make_patient(db)
    slot = make_slot(doctor, practice, db, status="booked")
    appointment = make_appointment(patient, slot, term, db)

    call = Call(
        vogent_call_id="vg_dash_1",
        caller_phone="555-0199",
        status="scheduled",
        patient_id=patient.id,
        appointment_id=appointment.id,
        matched_term_id=term.id,
        raw_complaint="my wrist hurts",
        transcript=[{"speaker": "agent", "text": "hi"}],
        started_at=datetime.now(timezone.utc),
    )
    db.add(call)
    db.flush()

    list_resp = client.get("/api/v1/calls", headers=headers)
    list_body = list_resp.get_json()
    assert list_resp.status_code == 200
    assert list_body["total"] >= 1
    ids = [c["id"] for c in list_body["calls"]]
    assert call.id in ids

    detail_resp = client.get(f"/api/v1/calls/{call.id}", headers=headers)
    detail_body = detail_resp.get_json()
    assert detail_resp.status_code == 200
    assert detail_body["vogent_call_id"] == "vg_dash_1"
    assert detail_body["patient"]["last_name"] == patient.last_name
    assert detail_body["matched_term"]["term"] == term.term
    assert detail_body["appointment"]["doctor"] == f"Dr. {doctor.first_name} {doctor.last_name}"
    assert detail_body["transcript"] == [{"speaker": "agent", "text": "hi"}]


def test_calls_list_filters_by_status(client, db):
    make_user(db)
    token = _login(client).get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    db.add(Call(vogent_call_id="vg_nomatch", status="no_match", started_at=datetime.now(timezone.utc)))
    db.add(Call(vogent_call_id="vg_sched", status="scheduled", started_at=datetime.now(timezone.utc)))
    db.flush()

    resp = client.get("/api/v1/calls?status=no_match", headers=headers)
    body = resp.get_json()
    assert all(c["status"] == "no_match" for c in body["calls"])


def test_call_detail_not_found(client, db):
    make_user(db)
    token = _login(client).get_json()["token"]
    resp = client.get("/api/v1/calls/999999", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404


def test_stale_in_progress_call_swept_to_abandoned_on_read(client, db):
    """A caller who hangs up mid-call (Vogent's end-of-call webhook never
    fires) shouldn't leave the call stuck 'in_progress' forever -- spec
    §5.7's 15-minute sweep, triggered lazily by a dashboard read."""
    make_user(db)
    token = _login(client).get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    stale_started = datetime.now(timezone.utc) - timedelta(minutes=20)
    call = Call(vogent_call_id="vg_hangup", status="in_progress", started_at=stale_started)
    fresh_call = Call(
        vogent_call_id="vg_fresh", status="in_progress", started_at=datetime.now(timezone.utc)
    )
    db.add_all([call, fresh_call])
    db.flush()

    resp = client.get("/api/v1/calls", headers=headers)
    body = resp.get_json()
    statuses_by_id = {c["id"]: c["status"] for c in body["calls"]}

    assert statuses_by_id[call.id] == "abandoned"
    assert statuses_by_id[fresh_call.id] == "in_progress"


def test_finished_call_transcript_backfilled_from_vogent(client, db, monkeypatch):
    """The account-level webhook never delivered dial.transcript to this
    deployment, so transcripts are pulled from Vogent's API the first time
    a finished call is opened (and cached)."""
    from app.blueprints import dashboard as dashboard_module

    make_user(db)
    token = _login(client).get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    call = Call(
        vogent_call_id="vg_backfill",
        status="scheduled",
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        transcript=[],
    )
    db.add(call)
    db.flush()

    monkeypatch.setattr(
        dashboard_module,
        "fetch_transcript",
        lambda _id: [{"speaker": "AI", "text": "hello"}],
    )

    body = client.get(f"/api/v1/calls/{call.id}", headers=headers).get_json()
    assert body["transcript"] == [{"speaker": "AI", "text": "hello"}]


def test_live_call_transcript_not_backfilled(client, db, monkeypatch):
    """A call still in progress must not have a half-written transcript
    cached -- it would freeze mid-conversation and never update."""
    from app.blueprints import dashboard as dashboard_module

    make_user(db)
    token = _login(client).get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    call = Call(
        vogent_call_id="vg_live",
        status="in_progress",
        started_at=datetime.now(timezone.utc),
        ended_at=None,
        transcript=[],
    )
    db.add(call)
    db.flush()

    called = {"n": 0}

    def _spy(_id):
        called["n"] += 1
        return [{"speaker": "AI", "text": "partial"}]

    monkeypatch.setattr(dashboard_module, "fetch_transcript", _spy)

    client.get(f"/api/v1/calls/{call.id}", headers=headers)
    assert called["n"] == 0
