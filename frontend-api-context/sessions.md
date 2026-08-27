# Sessions Service

Generates today's recommended training session. Premium-only and synchronous. The generated
session is not stored for retrieval — there is no GET — but the request and its result are
recorded server-side for moderation review.

---

## POST /sessions/generate
Returns a session: one to five of the five fundamental lifts, each paired with a set plan.

**Auth:** API Key + Access Token · **Premium required**

The backend assembles the model's inputs itself — recent training, strength tiers, local
dates — by reading DynamoDB. The client sends only the two things the backend cannot know.

**Request:**
```json
{
  "set_plan_catalog": [
    {
      "id": "9f2a...",
      "name": "Standard",
      "sequence": ["easy", "easy", "moderate", "moderate", "hard", "progress"],
      "description": "Warm-up sets followed by a progress set"
    }
  ],
  "user_context": {
    "chips": ["Legs are sore"],
    "note": "left knee twinged on Tuesday"
  }
}
```

**Request Fields:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `set_plan_catalog` | array | Yes | The complete catalog as it exists on the client — built-ins **and** user-created plans. Must be non-empty. The catalog lives in client code, so the backend never stores or versions it |
| `set_plan_catalog[].id` | string | Yes | `planId`. The model returns this verbatim; the client resolves it back to a plan |
| `set_plan_catalog[].sequence` | string[] | Yes | Effort keys in order: `easy`, `moderate`, `hard`, `near_max`, `progress`. The app's stored spellings `redline` and `pr` are accepted and normalized server-side, so `effortSequence` can be sent as-is |
| `set_plan_catalog[].description` | string | No | Defaults to empty |
| `user_context.chips` | string[] | No | **Capped at 8.** Extras are dropped, not rejected |
| `user_context.note` | string | No | **Capped at 500 characters.** Longer notes are truncated, not rejected — the first 500 carry the intent. Screened for abuse; see below |

**Response (200):**
```json
{
  "session": {
    "summary": "Squats and Bench are both due a progress set, and you haven't pulled since Monday.",
    "items": [
      {
        "exercise_id": "4c81...",
        "exercise_name": "Deadlifts",
        "set_plan_id": "9f2a...",
        "set_plan_name": "Standard",
        "rationale": "Five days since your last pull, and the last one moved your e1RM."
      }
    ]
  }
}
```

**Response Fields:**
| Field | Type | Description |
|-------|------|-------------|
| `note_used` | boolean | **Top level, beside `session`.** False when the free-text note was withheld from the generator. No reason is given — see below |
| `session.summary` | string | One or two sentences on why today looks like this |
| `session.items` | array | One to five entries, one per lift. Never repeats a lift |
| `items[].exercise_id` | string | `exerciseItemId` from the exercises table. Guaranteed to resolve — the backend rejects any id it did not send |
| `items[].set_plan_id` | string | An `id` from the catalog the client sent. Same guarantee |
| `items[].exercise_name` | string | Convenience copy; `exercise_id` is authoritative |
| `items[].set_plan_name` | string | Convenience copy; `set_plan_id` is authoritative |
| `items[].rationale` | string | One short sentence naming the reason this lift is here |

**Errors:**
| Status | Meaning | Client handling |
|--------|---------|-----------------|
| `400` | `set_plan_catalog` missing, empty, or malformed | A bug — do not offer Retry |
| `401` | Missing or invalid token | Standard auth handling |
| `402` | Not premium | Show the upsell |
| `422` | No fundamental lifts found for this user | Cannot generate; direct the user to log some lifts first |
| `502` | Model returned a session referencing ids that were never sent | Retryable |
| `503` | Generation failed or timed out. Body carries `"retryable": true` | Offer Retry |

---

## Notes for the client

**Retry is the client's job.** The endpoint makes exactly one attempt at generation. It sits
behind API Gateway's fixed 29-second ceiling, so a server-side retry loop would risk a bare
504 with no error body to act on. A `503` means "ask again" — the Retry button re-requests
from scratch, which is the retry strategy.

**Expect it to be slow.** Observed generations run 16–20 seconds. The server bounds itself
just under API Gateway's 29s ceiling and returns a retryable `503` if it cannot finish, so the
client should keep its own request timeout comfortably above 30 seconds — cutting the
connection early turns a clean, retryable server error into an opaque network failure.

**The free-text note is screened.** If it fails moderation — or if the moderation service
itself is unreachable — the note is dropped and the session is generated from the chips
alone. The request is never rejected for this. The response then carries `note_used: false`
and the client should say something like *"Your note couldn't be used."*

**Do not surface a reason.** The response deliberately gives one boolean and no cause.
"Flagged" and "we could not check" are both "couldn't be used" to the user, and a reason code
would only tell someone probing the filter which of the two they hit.

**No GET, no history, no idempotency.** Re-requesting produces a different session, and a
session the user navigates away from is gone as far as the API is concerned. If a generated
session needs to survive app restart, the client owns that. (The backend does keep a record
of each request, but it is for moderation review and is not readable through the API.)

**Timestamps and dates are resolved from the user's own local time**, using
`createdUtcOffsetSeconds` on each record and falling back to `createdTimezone`. Make sure
`timezone` on user-properties stays current — it is what determines which calendar day the
backend treats as "today".
