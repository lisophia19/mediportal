# Read-only client for Vogent's own API, used to pull a finished call's
# transcript.
#
# Why we pull instead of waiting to be pushed: the account-level webhook
# reliably delivers dial.inbound but never delivered dial.transcript to this
# deployment, so transcripts never landed. Pulling also backfills calls that
# happened before the webhook existed, which a push never can.
import requests
from flask import current_app

VOGENT_API = "https://api.vogent.ai/api"
TIMEOUT_SECONDS = 5


def fetch_transcript(vogent_call_id):
    """Returns Vogent's transcript turns for a dial, or None if unavailable.
    Never raises -- callers treat this as best-effort enrichment."""
    api_key = current_app.config.get("VOGENT_API_KEY")
    if not api_key or not vogent_call_id:
        return None
    try:
        resp = requests.get(
            f"{VOGENT_API}/dials/{vogent_call_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        turns = resp.json().get("transcript")
    except Exception:
        current_app.logger.exception("Vogent transcript fetch failed for %s", vogent_call_id)
        return None
    if not isinstance(turns, list):
        return None

    # Keep only real spoken turns -- Vogent interleaves node-transition and
    # function-call entries that carry no text and would render as blanks.
    return [
        {"speaker": turn.get("speaker"), "text": (turn.get("text") or "").strip()}
        for turn in turns
        if (turn.get("text") or "").strip()
    ]
