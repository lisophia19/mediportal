# Vogent wraps every function-call webhook as {dial_id, dial, params} --
# the flow's declared input parameters arrive nested under "params", not as
# flat top-level fields (confirmed against Vogent's webhook docs and a real
# example flow's payload shape, both 2026-09). Every Vogent-facing route
# reads the request body through this helper so it works identically
# whether called directly (flat body, e.g. tests/curl) or via a real Vogent
# webhook (wrapped body) -- callers never need to know which.
from flask import request


def get_agent_json():
    body = request.get_json(silent=True) or {}
    params = body.get("params")
    if not isinstance(params, dict):
        return body

    merged = {**body, **params}
    # dial_id is Vogent's own call identifier, auto-injected regardless of
    # declared inputs. Our own endpoints spell this field two different ways
    # (vogent_call_id on /calls*, call_id everywhere else) -- default both
    # from dial_id so no flow node ever needs to explicitly wire a call-id
    # parameter; the real value still wins if a node does pass one.
    if dial_id := body.get("dial_id"):
        merged.setdefault("vogent_call_id", dial_id)
        merged.setdefault("call_id", dial_id)
    return merged
