# Tests for the Vogent function-call webhook envelope unwrapping
# (app/vogent_utils.py) -- {dial_id, dial, params} vs. a flat body, and the
# float-formatted-integer coercion Vogent's flow templates require.
from app.vogent_utils import coerce_int, get_agent_json


def test_flat_body_passes_through_unchanged(app):
    with app.test_request_context(json={"last_name": "Rodriguez"}):
        assert get_agent_json() == {"last_name": "Rodriguez"}


def test_wrapped_params_are_merged_to_top_level(app):
    with app.test_request_context(
        json={"dial_id": "dial_1", "dial": {"source_number": "+1"}, "params": {"last_name": "Rodriguez"}}
    ):
        body = get_agent_json()
        assert body["last_name"] == "Rodriguez"


def test_dial_id_fills_both_call_id_spellings_when_absent(app):
    with app.test_request_context(json={"dial_id": "dial_1", "params": {}}):
        body = get_agent_json()
        assert body["vogent_call_id"] == "dial_1"
        assert body["call_id"] == "dial_1"


def test_explicit_call_id_wins_over_dial_id_fallback(app):
    with app.test_request_context(
        json={"dial_id": "dial_1", "params": {"vogent_call_id": "vg_explicit"}}
    ):
        body = get_agent_json()
        assert body["vogent_call_id"] == "vg_explicit"


def test_coerce_int_handles_vogent_float_formatted_integers():
    """Regression test: a real production call showed Vogent stringifying a
    declared INTEGER node output as "60.000000" (not "60") when interpolated
    into a downstream function's input -- broke every integer-column write
    with a Postgres syntax error until this was added."""
    assert coerce_int("60.000000") == 60
    assert coerce_int("21.000000") == 21
    assert coerce_int(60) == 60
    assert coerce_int(60.0) == 60
    assert coerce_int("60") == 60
    assert coerce_int(None) is None
    assert coerce_int("") is None
    assert coerce_int("not a number") is None
