"""Device token selection and lifecycle.

PHASED READ. The app has written `apnsDeviceToken` onto user-properties for a long time, and
still does. The dedicated `apns-tokens` table is the destination, but it starts empty, so this
reads the table first and falls back to user-properties when a user has no row yet. A send
against a fallback token BACKFILLS a row, so the table populates itself as notifications go out
rather than needing a migration to be useful.

The fallback disappears in a later phase, together with the user-properties write — see the
comment at the write site in `services/user/lambda/handlers/user.py`.

WHY A COMPOSITE KEY. `userId` + `apnsToken` means multiple devices per user cost nothing and
"same device, new account" is two visible rows rather than one silent overwrite. It also means
a token can be marked dead for one user without disturbing another.

WHAT MAKES A TOKEN SENDABLE. Not invalid (Apple has not rejected it) and not logged out (the
account that registered it is no longer signed in on that device).

ROWS ARE COMPLETE BY CONSTRUCTION. Every write path goes through `_write`, which initialises
every attribute the table ever carries — `False` for the flags, DynamoDB NULL for the
not-yet-known timestamps. So a reader never has to decide what an absent key means, and
"logged out is false" and "nobody has written logged out" stop being the same thing on disk.

The reads still use `is not True` rather than `is False`. Rows written before this invariant
existed are still out there, and a token whose flags predate it should stay sendable rather
than be silently muted by its own age.
"""

import os
from datetime import datetime, timezone
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

_dynamodb = None


def _resource():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def _tokens_table():
    name = os.environ.get("APNS_TOKENS_TABLE_NAME")
    if not name:
        raise ValueError("APNS_TOKENS_TABLE_NAME environment variable not set")
    return _resource().Table(name)


def _user_properties_table():
    name = os.environ.get("USER_PROPERTIES_TABLE_NAME")
    if not name:
        raise ValueError("USER_PROPERTIES_TABLE_NAME environment variable not set")
    return _resource().Table(name)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def token_suffix(device_token: str) -> str:
    """The last 8 characters, which is all that ever gets logged.

    Enough to correlate a log row with a token row during triage; useless to anyone who obtains
    the log. A full device token is a credential for pushing to that device — it does not belong
    in a table we read casually.
    """
    return device_token[-8:] if device_token else ""


def user_properties(user_id: str) -> dict[str, Any]:
    """The user's properties row, or `{}`. Absence is normal — a user can exist without one."""
    resp = _user_properties_table().get_item(Key={"userId": user_id})
    return resp.get("Item") or {}


def usable_tokens(user_id: str, properties: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every token this user should currently receive pushes on.

    `properties` is accepted so the caller can pass the row it already fetched for the
    precondition check, instead of reading the same item twice per notification.
    """
    resp = _tokens_table().query(KeyConditionExpression=Key("userId").eq(user_id))
    rows = [
        r for r in resp.get("Items", [])
        if r.get("invalid") is not True and r.get("loggedOut") is not True
    ]
    if rows:
        return rows

    # Fallback: no row yet for this user. Synthesise one from user-properties so they are
    # reachable during the transition. `_fallback` marks it for backfill after a send.
    props = user_properties(user_id) if properties is None else properties
    legacy = props.get("apnsDeviceToken")
    if not legacy:
        return []
    return [{
        "userId": user_id,
        "apnsToken": legacy,
        # Unknown for a legacy token — nothing recorded it. Production is the safer guess: it is
        # what TestFlight and App Store builds produce, which is nearly everyone. A wrong guess
        # costs one extra round trip, because `apns.send` retries the other host on
        # BadDeviceToken and reports which one worked.
        "apnsEnvironment": "production",
        "_fallback": True,
    }]


# Every attribute a token row ever carries, with the value it holds before anything is known.
# Written by EVERY path that can create a row, so an item is never partially shaped.
#
# WHY BOTHER: absence and falsity are different things, and code that cannot tell them apart
# ends up guessing. A row with no `loggedOut` key might mean "not logged out" or "written by a
# path that did not know" — with `False` on disk it means exactly one thing. `None` becomes a
# DynamoDB NULL: an explicit "not yet known", which is also distinguishable.
_CREATION_DEFAULTS = {
    "apnsEnvironment": None,
    "invalid": False,
    "invalidatedAt": None,
    "invalidReason": None,
    "loggedOut": False,
    "lastRegisteredUtc": None,
    "lastDeliveredUtc": None,
    "backfilledFrom": None,
}


def _write(user_id: str, device_token: str, explicit: dict) -> None:
    """Apply `explicit`, and initialise every other attribute exactly once.

    Anything the caller does not set is written with `if_not_exists`, so whichever path creates
    the row first produces a COMPLETE item and every later path leaves those values untouched.

    Every attribute name is aliased through ExpressionAttributeNames, without checking which
    ones need it. `shard` taught us that a reserved keyword in a raw expression fails the whole
    operation at runtime, and aliasing unconditionally costs nothing.
    """
    now = _now()
    defaults = dict(_CREATION_DEFAULTS)
    defaults["createdDatetime"] = now
    explicit = {**explicit, "lastModifiedDatetime": now}

    parts, names, values = [], {}, {}
    for i, (key, value) in enumerate(explicit.items()):
        names[f"#e{i}"], values[f":e{i}"] = key, value
        parts.append(f"#e{i} = :e{i}")
    for i, (key, value) in enumerate(defaults.items()):
        if key in explicit:
            continue
        names[f"#d{i}"], values[f":d{i}"] = key, value
        parts.append(f"#d{i} = if_not_exists(#d{i}, :d{i})")

    _tokens_table().update_item(
        Key={"userId": user_id, "apnsToken": device_token},
        UpdateExpression="SET " + ", ".join(parts),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def record_registration(user_id: str, device_token: str, environment: str) -> None:
    """Upsert on token registration.

    Resets `invalid`/`loggedOut` to False and clears the invalidation detail: a device
    re-registering is telling us this token is live again, which genuinely happens after a
    reinstall, and a token retired earlier should come back rather than stay muted forever.
    """
    _write(user_id, device_token, {
        "lastRegisteredUtc": _now(),
        "apnsEnvironment": environment,
        "invalid": False,
        "invalidatedAt": None,
        "invalidReason": None,
        "loggedOut": False,
    })


def record_delivery(user_id: str, device_token: str, environment: str,
                    was_fallback: bool = False) -> None:
    """Stamp a successful send, creating a complete row if this was a user-properties fallback.

    This is the self-migration: the first successful push to a legacy token gives it a real
    row, so the fallback path narrows on its own without a migration job.
    """
    explicit = {"lastDeliveredUtc": _now(), "apnsEnvironment": environment}
    if was_fallback:
        explicit["backfilledFrom"] = "user-properties"
    _write(user_id, device_token, explicit)


def mark_invalid(user_id: str, device_token: str, reason: str) -> None:
    """Retire a token Apple has rejected permanently. Written even for a fallback token, so the
    row exists purely to stop us trying it again."""
    _write(user_id, device_token, {
        "invalid": True,
        "invalidatedAt": _now(),
        "invalidReason": reason[:200],
    })


def mark_logged_out(user_id: str, device_token: str) -> None:
    """The account signed out on this device. The token is still valid for whoever signs in
    next, so this is deliberately not `invalid` — it mutes this pairing only."""
    _write(user_id, device_token, {"loggedOut": True})


def correct_environment(user_id: str, device_token: str, environment: str) -> None:
    """Fix a token filed under the wrong APNs environment, once a send has proven which host
    answers for it. Cheap to write and it stops every future send paying the retry."""
    _write(user_id, device_token, {"apnsEnvironment": environment})
