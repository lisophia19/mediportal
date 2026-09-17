# Tests for the account-level Vogent webhook (/webhooks/vogent): HMAC
# signature gating, transcript capture, and the terminal-status gap-fill.
import hashlib
import hmac
import json

from app.models import Call


def _post(client, secret, body):
    raw = json.dumps(body).encode()
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/vogent",
        data=raw,
        headers={"Content-Type": "application/json", "X-Elto-Signature": signature},
    )


def test_requires_valid_signature(client):
    resp = client.post(
        "/webhooks/vogent",
        json={"event": "dial.transcript", "payload": {"dial_id": "vg_1"}},
        headers={"X-Elto-Signature": "not-the-real-signature"},
    )
    assert resp.status_code == 401


def test_missing_signature_header_rejected(client):
    resp = client.post("/webhooks/vogent", json={"event": "dial.transcript", "payload": {}})
    assert resp.status_code == 401


def test_dial_transcript_stores_transcript(client, db, app):
    call = Call(vogent_call_id="vg_wh1", status="in_progress")
    db.add(call)
    db.commit()

    turns = [{"speaker": "AI", "text": "Hi there"}, {"speaker": "HUMAN", "text": "Hi"}]
    resp = _post(
        client,
        app.config["VOGENT_WEBHOOK_SECRET"],
        {"event": "dial.transcript", "payload": {"dial_id": "vg_wh1", "transcript": turns}},
    )
    assert resp.status_code == 200

    updated = Call.query.filter_by(vogent_call_id="vg_wh1").first()
    assert updated.transcript == turns


def test_dial_updated_fills_abandoned_when_call_never_completed(client, db, app):
    call = Call(vogent_call_id="vg_wh2", status="in_progress")
    db.add(call)
    db.commit()

    resp = _post(
        client,
        app.config["VOGENT_WEBHOOK_SECRET"],
        {"event": "dial.updated", "payload": {"dial_id": "vg_wh2", "status": "completed"}},
    )
    assert resp.status_code == 200

    updated = Call.query.filter_by(vogent_call_id="vg_wh2").first()
    assert updated.status == "abandoned"
    assert updated.ended_at is not None


def test_dial_updated_does_not_overwrite_flow_set_terminal_status(client, db, app):
    """The flow's own complete_call already ran (ended_at is set) -- the
    webhook must not clobber a real scheduled/no_match outcome."""
    call = Call(vogent_call_id="vg_wh3", status="scheduled")
    db.add(call)
    db.commit()
    from datetime import datetime, timezone

    call.ended_at = datetime.now(timezone.utc)
    db.commit()

    _post(
        client,
        app.config["VOGENT_WEBHOOK_SECRET"],
        {"event": "dial.updated", "payload": {"dial_id": "vg_wh3", "status": "completed"}},
    )

    updated = Call.query.filter_by(vogent_call_id="vg_wh3").first()
    assert updated.status == "scheduled"


def test_unknown_dial_id_returns_200(client, app):
    resp = _post(
        client,
        app.config["VOGENT_WEBHOOK_SECRET"],
        {"event": "dial.transcript", "payload": {"dial_id": "does_not_exist"}},
    )
    assert resp.status_code == 200
