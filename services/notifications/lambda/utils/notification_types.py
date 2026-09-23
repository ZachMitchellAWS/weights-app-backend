"""The notification type registry.

A task carries a TYPE and nothing else — no title, no body, no variables. Everything a
notification says is resolved here at send time, which has two consequences worth stating
plainly because they are the reason for the design:

  * Editing copy changes what ALREADY-QUEUED tasks will say. A nudge queued three days ago
    renders against today's strings, not the ones that were current when it was written.
  * A queued notification can decide not to send. `precondition` runs immediately before
    delivery and can cancel — the world moves between queueing and sending, and a nudge to do
    something the user has since done is worse than no nudge at all.

A cancelled send is LOGGED (`skipped_precondition`), never silently dropped. "Why did this user
not get a nudge" is the question the notification log exists to answer.

ADDING A TYPE. Add an entry here and nothing else — the handler, the queue and the sender are
all type-agnostic. If a future type needs to read training data to build its copy, widen
`RenderContext` and take the matching read grants in `notifications_stack.py`; today the
precondition only needs user-properties, so that is the only read the service has.
"""

from dataclasses import dataclass
from typing import Callable


@dataclass
class RenderContext:
    """Everything a type is allowed to look at. Deliberately small.

    `user_properties` is the user's row, or `{}` if they have none — a user can exist without
    one, so every predicate here must treat absence as "not yet done" rather than crashing.
    """
    user_id: str
    user_properties: dict


@dataclass
class NotificationType:
    key: str
    title: str
    body: str
    precondition: Callable[[RenderContext], bool]
    # APNs `aps.category`, for actionable notifications later. None omits the field entirely
    # rather than sending an empty one, which Apple treats as a malformed payload.
    category: str | None = None


def _tier_not_yet_unlocked(ctx: RenderContext) -> bool:
    """True while the user has NOT unlocked their starting strength tier.

    `hasMetStrengthTierConditions` is the same flag the app writes when all five fundamentals
    have an e1RM, and the same one `scripts/count_apns_without_tier.py` counts. Checked with
    `is not True` rather than `not ...` so a missing key and an explicit False behave
    identically — both mean "still to do".
    """
    return ctx.user_properties.get("hasMetStrengthTierConditions") is not True


REGISTRY: dict[str, NotificationType] = {
    "unlock-strength-tier-nudge": NotificationType(
        key="unlock-strength-tier-nudge",
        title="How strong are you?",
        body="Log one set per lift to find out.",
        precondition=_tier_not_yet_unlocked,
    ),
}


def get(notification_type: str) -> NotificationType | None:
    """Look up a type, or None if it is unknown.

    Unknown types are a real possibility, not a theoretical one: a task row can be hand-written,
    and a type can be removed from this file while tasks referencing it are still queued. The
    caller logs and drops rather than raising, so one bad row cannot stall a shard.
    """
    return REGISTRY.get(notification_type)
