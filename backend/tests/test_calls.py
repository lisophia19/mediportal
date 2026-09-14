# Tests for call capture lifecycle (spec §5.7): create -> patch ->
# complete, idempotent complete, and auth gating.
from .factories import make_term


def test_requires_agent_key(client):
    resp = client.post("/api/v1/calls", json={"vogent_call_id": "vg_1"})
    assert resp.status_code == 401


def test_create_call(client, agent_headers):
    resp = client.post(
        "/api/v1/calls",
        headers=agent_headers,
        json={"vogent_call_id": "vg_test_1", "caller_phone": "555-0111"},
    )
    body = resp.get_json()
    assert resp.status_code == 201
    assert body["call_id"]


def test_create_call_missing_vogent_id_400(client, agent_headers):
    resp = client.post("/api/v1/calls", headers=agent_headers, json={})
    assert resp.status_code == 400


def test_create_call_twice_returns_same_call(client, agent_headers):
    first = client.post(
        "/api/v1/calls", headers=agent_headers, json={"vogent_call_id": "vg_dup"}
    ).get_json()
    second = client.post(
        "/api/v1/calls", headers=agent_headers, json={"vogent_call_id": "vg_dup"}
    ).get_json()
    assert first["call_id"] == second["call_id"]


def test_patch_call_updates_fields(client, db, agent_headers):
    term = make_term(db)
    client.post("/api/v1/calls", headers=agent_headers, json={"vogent_call_id": "vg_patch"})

    resp = client.patch(
        "/api/v1/calls/vg_patch",
        headers=agent_headers,
        json={"raw_complaint": "wrist hurts", "matched_term_id": term.id},
    )
    assert resp.status_code == 200

    from app.models import Call

    call = Call.query.filter_by(vogent_call_id="vg_patch").first()
    assert call.raw_complaint == "wrist hurts"
    assert call.matched_term_id == term.id


def test_patch_unknown_call_404(client, agent_headers):
    resp = client.patch(
        "/api/v1/calls/does_not_exist", headers=agent_headers, json={"raw_complaint": "x"}
    )
    assert resp.status_code == 404


def test_complete_call_is_idempotent(client, agent_headers):
    client.post("/api/v1/calls", headers=agent_headers, json={"vogent_call_id": "vg_complete"})

    first = client.post(
        "/api/v1/calls/vg_complete/complete",
        headers=agent_headers,
        json={"status": "scheduled", "transcript": [{"speaker": "agent", "text": "hi"}]},
    )
    second = client.post(
        "/api/v1/calls/vg_complete/complete",
        headers=agent_headers,
        json={"status": "failed", "transcript": []},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    # Second call must not overwrite the first completion.
    assert second.get_json()["status"] == "scheduled"

    from app.models import Call

    call = Call.query.filter_by(vogent_call_id="vg_complete").first()
    assert call.status == "scheduled"
    assert len(call.transcript) == 1
