"""Screen the user's free-text note before it reaches the model.

Only the free text is screened. The context chips are safe by construction — the user
picked them from a fixed list in client code, so there is nothing to check.

FAILS CLOSED. A flagged note, a moderation timeout, a transport error and an unparseable
response all produce the same outcome: the note is dropped. Unchecked text is treated as
unsafe, because the alternative is that the one request where screening broke is also the
request that goes through unscreened.

Dropping the note never fails the request. The session is still generated from the chips,
which is a worse session than the user asked for but a session nonetheless — refusing
outright would punish a user for a false positive on a clumsy phrase.
"""

import logging
import os

from utils.openai_client import GenerationTimeout, _deadline, _get_client

logger = logging.getLogger(__name__)

# Overridable so the model can be PINNED without a code change. `omni-moderation-latest` is
# an alias whose false-positive rate can shift under us with no deploy — and since this fails
# closed, a drift toward over-flagging silently starts dropping legitimate notes. It is the
# default only because a wrong pinned id would fail every call and drop every note, which is
# the worse failure. Pin it via the env var once a snapshot id is confirmed against the API.
MODERATION_MODEL = os.environ.get("OPENAI_MODERATION_MODEL", "omni-moderation-latest")

# Its own budget, nested inside the request's. Moderation typically answers in well under a
# second; left unbounded it would eat the generation budget it is supposed to sit inside.
MODERATION_DEADLINE_SECONDS = 5.0

# Outcomes, recorded verbatim on the stored item.
OK = "ok"
FLAGGED = "flagged"
# Split out from FLAGGED on purpose. The note is dropped either way, but this must NOT count
# as an abuse violation: treating someone who may need help the same as someone being abusive
# would corrupt the count and any decision later made from it.
FLAGGED_SELF_HARM = "flagged_self_harm"
ERROR = "error"
ABSENT = "absent"

# Moderation reports self-harm across several sub-categories.
_SELF_HARM_PREFIX = "self-harm"


def counts_as_violation(status: str) -> bool:
    """Only deliberate abuse increments the per-user counter.

    `error` is excluded because the note was dropped by our own failure, which says nothing
    about the user. `flagged_self_harm` is excluded for the reason above.
    """
    return status == FLAGGED


def screen(note: str) -> tuple[bool, list[str], str]:
    """Return (allowed, categories, status) for a note.

    `allowed` is the only thing the request path needs; `categories` and `status` exist for
    the record. A False from here means "do not send this text", whatever the reason.
    """
    if not note or not note.strip():
        return True, [], ABSENT

    try:
        client = _get_client()
        with _deadline(MODERATION_DEADLINE_SECONDS):
            response = client.moderations.create(model=MODERATION_MODEL, input=note)

        result = response.results[0]
        if not result.flagged:
            return True, [], OK

        # `categories` is a pydantic model of booleans; keep only what tripped.
        raw = result.categories.model_dump() if hasattr(result.categories, "model_dump") else {}
        categories = sorted(k for k, v in raw.items() if v)

        # Normalised because the SDK reports these with either "-" or "_" depending on
        # version, and a missed match here would silently count a self-harm hit as abuse.
        self_harm_only = bool(categories) and all(
            c.replace("_", "-").startswith(_SELF_HARM_PREFIX) for c in categories
        )
        status = FLAGGED_SELF_HARM if self_harm_only else FLAGGED

        logger.warning("Note flagged (%s): %s", status, ", ".join(categories) or "unspecified")
        return False, categories, status

    except GenerationTimeout:
        logger.warning("Moderation timed out after %.1fs; dropping note", MODERATION_DEADLINE_SECONDS)
        return False, [], ERROR
    except Exception:
        # Deliberately broad. Every failure mode here has the same correct response, and a
        # moderation outage must not become a 500 on a request that can still be fulfilled.
        logger.exception("Moderation call failed; dropping note")
        return False, [], ERROR
