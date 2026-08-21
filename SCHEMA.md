# WeightApp Backend - DynamoDB Schema

## Tables Overview

| Service | Table | Partition Key | Sort Key | GSI | TTL | Soft Delete |
|---------|-------|---------------|----------|-----|-----|-------------|
| Auth | users | userId | - | emailAddress-index | - | No |
| Auth | password-reset-codes | userId | - | - | expiryTime | No |
| User | user-properties | userId | - | - | - | No |
| User | ad-attributions | userId | createdDatetime | - | - | No |
| Checkin | exercises | userId | exerciseItemId | - | - | Yes |
| Checkin | lift-sets | userId | liftSetId | userId-createdDatetime-index | - | Yes |
| Checkin | estimated-1rm | userId | liftSetId | userId-createdDatetime-index | - | Yes |
| Checkin | set-plans | userId | planId | - | - | Yes |
| Checkin | recovery-checkins | userId | recoveryCheckinId | userId-checkinDate-index | - | Yes |
| Checkin | groups | userId | groupId | - | - | Yes |
| Entitlements | entitlement-grants | userId | startUtc | userId-endUtc-index | - | No |
| Sessions | generated-sessions | userId | sessionId | - | - | No |

---

## Auth Service

### users

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| userId | String | Yes | Partition key (UUID) |
| emailAddress | String | Yes | GSI partition key, must be unique |
| passwordHash | String | Yes | bcrypt hash |
| createdDatetime | String | Yes | ISO 8601 |
| lastModifiedDatetime | String | Yes | ISO 8601 |

### password-reset-codes

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| userId | String | Yes | Partition key |
| code | String | Yes | 6-digit reset code |
| createdDatetime | String | Yes | ISO 8601 |
| expiryTime | Number | Yes | Unix timestamp, TTL attribute (auto-deletes after 1hr) |
| resetAttempts | Number | Yes | Rate limiting counter (max 3/hr) |

---

## User Service

### user-properties

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| userId | String | Yes | Partition key |
| availableChangePlates | Number[] | Yes | List of plate weights (can be empty []) |
| bodyweight | Number | No | Nullable -- can be removed via null in POST |
| minReps | Number | No | Global minimum reps target |
| maxReps | Number | No | Global maximum reps target |
| activeSetPlanId | String | No | Nullable -- UUID of active set plan |
| stepsGoal | Number | No | Nullable -- daily steps goal (positive int) |
| proteinGoal | Number | No | Nullable -- daily protein goal (positive int) |
| bodyweightTarget | Number | No | Nullable -- target bodyweight |
| biologicalSex | String | No | Nullable -- "male" or "female" |
| weightUnit | String | No | "lbs" or "kg" |
| timezone | String | No | Nullable -- IANA identifier (validated). Push-only client metadata |
| utcOffsetSeconds | Number | No | Nullable -- device's latest UTC offset in seconds east of UTC. A CACHE synced beside `timezone`; it does not move when DST does, so `timezone` stays authoritative for resolving the current offset. Push-only client metadata |
| locale | String | No | Nullable -- device locale identifier, e.g. "en_US" (max 40 chars). Push-only client metadata |
| language | String | No | Nullable -- device language code, e.g. "en" (max 16 chars). Push-only client metadata |
| latestAppVersion | String | No | Nullable -- most recent app version seen, e.g. "1.4.2" (max 32 chars). Push-only client metadata |
| hasCompletedOnboarding | Boolean | No | Set true when a user finishes the onboarding flow. Push-only; not backfilled for pre-feature users |
| apnsDeviceToken | String | No | Nullable -- push token (max 200 chars) |
| hasMetStrengthTierConditions | Boolean | No | Default false -- set true when user completes strength tier journey |
| createdDatetime | String | Yes | ISO 8601 |
| lastModifiedDatetime | String | Yes | ISO 8601 |

Auto-created when a user registers. All non-key fields are optional in the POST body; a field is written
only when its key is present, left untouched when absent, and removed when sent as explicit `null`
(nullable fields only). GET returns the full stored item.

### ad-attributions

Apple Search Ads attribution for the install a user signed up from. Written at most once per
install by the client and never read back — it exists to be joined against Apple Ads
reporting, so cost-per-signup can be attributed to a campaign, ad group or keyword.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| userId | String | Yes | Partition key |
| createdDatetime | String | Yes | Sort key, ISO 8601. A timestamp rather than a fixed key so a reinstall or a second device records its own row instead of overwriting the first |
| attribution | Boolean | Yes | Whether Apple reported this install as ad-driven |
| orgId | String | No | Apple Ads org. Numeric from Apple, stored as a string — these are identifiers, not quantities |
| campaignId | String | No | |
| adGroupId | String | No | |
| keywordId | String | No | |
| adId | String | No | |
| conversionType | String | No | e.g. `Download`, `Redownload` |
| clickDate | String | No | ISO 8601, from Apple |
| countryOrRegion | String | No | |

**Production stores attributed installs only**; staging stores organic ones too, so the write
path can be exercised on a TestFlight build (which always reports organic) without waiting for
a live campaign.

Written with `attribute_not_exists(userId) AND attribute_not_exists(createdDatetime)`, so a
retried request cannot create a duplicate.

No ATT prompt or IDFA is involved — AdServices returns campaign-level attribution for the
install, not a cross-app identifier.

---

## Checkin Service

### exercises

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| userId | String | Yes | Partition key |
| exerciseItemId | String | Yes | Sort key (UUID) |
| name | String | Yes | |
| isCustom | Boolean | Yes | |
| loadType | String | Yes | "Barbell" or "Single Load" |
| createdTimezone | String | Yes | e.g. "America/Los_Angeles" |
| createdUtcOffsetSeconds | Number | No | Seconds EAST of UTC at creation (negative in the Americas). Absent on records from older clients — fall back to resolving `createdTimezone` against `createdDatetime`. `0` is legal (UTC) |
| createdDatetime | String | Yes | ISO 8601 |
| lastModifiedDatetime | String | Yes | ISO 8601 |
| movementType | String | No | e.g. "Push", "Pull", "Legs" |
| notes | String | No | Removed if set to null/empty |
| icon | String | No | |
| deleted | Boolean | No | Only present when true |

### lift-sets

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| userId | String | Yes | Partition key |
| liftSetId | String | Yes | Sort key (UUID) |
| exerciseId | String | Yes | References exercises table |
| reps | Number | Yes | Integer |
| weight | Decimal | Yes | Stored as Decimal, returned as float |
| createdTimezone | String | Yes | |
| createdUtcOffsetSeconds | Number | No | Seconds EAST of UTC at creation (negative in the Americas). Absent on records from older clients — fall back to resolving `createdTimezone` against `createdDatetime`. `0` is legal (UTC) |
| createdDatetime | String | Yes | ISO 8601 |
| lastModifiedDatetime | String | Yes | ISO 8601 |
| isBaselineSet | Boolean | No | Whether this set is a baseline measurement |
| rir | Number | No | Reps in reserve (integer) |
| deleted | Boolean | No | Only present when true |

**GSI:** `userId-createdDatetime-index` -- enables "most recent first" pagination.

### estimated-1rm

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| userId | String | Yes | Partition key |
| liftSetId | String | Yes | Sort key (UUID of associated lift set) |
| estimated1RMId | String | Yes | Unique ID for this record (UUID) |
| exerciseId | String | Yes | References exercises table |
| value | Decimal | Yes | Stored as Decimal, returned as float |
| createdTimezone | String | Yes | |
| createdUtcOffsetSeconds | Number | No | Seconds EAST of UTC at creation (negative in the Americas). Absent on records from older clients — fall back to resolving `createdTimezone` against `createdDatetime`. `0` is legal (UTC) |
| createdDatetime | String | Yes | ISO 8601 |
| lastModifiedDatetime | String | Yes | ISO 8601 |
| deleted | Boolean | No | Only present when true |

**GSI:** `userId-createdDatetime-index` -- enables "most recent first" pagination.

### set-plans

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| userId | String | Yes | Partition key |
| planId | String | Yes | Sort key (UUID) |
| name | String | Yes | Plan name |
| effortSequence | List\<String\> | Yes | Ordered list of effort levels (easy, moderate, hard, redline, pr) |
| isCustom | Boolean | Yes | Whether plan is user-created or built-in |
| planDescription | String | No | Optional description |
| createdTimezone | String | Yes | e.g. "America/Los_Angeles" |
| createdUtcOffsetSeconds | Number | No | Seconds EAST of UTC at creation (negative in the Americas). Absent on records from older clients — fall back to resolving `createdTimezone` against `createdDatetime`. `0` is legal (UTC) |
| createdDatetime | String | Yes | ISO 8601 |
| lastModifiedDatetime | String | Yes | ISO 8601 |
| deleted | Boolean | No | Only present when true |

### recovery-checkins

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| userId | String | Yes | Partition key |
| recoveryCheckinId | String | Yes | Sort key (UUID) |
| checkinDate | String | Yes | "YYYY-MM-DD" — the day the response is for |
| primaryResponse | String | Yes | ready, good, slightly_fatigued, very_fatigued, sick |
| severityLevel | String | No | For sick: mild, moderate, severe |
| planningToTrain | Boolean | No | For very_fatigued/sick |
| createdTimezone | String | Yes | e.g. "America/Los_Angeles" |
| createdUtcOffsetSeconds | Number | No | Seconds EAST of UTC at creation (negative in the Americas). Absent on records from older clients — fall back to resolving `createdTimezone` against `createdDatetime`. `0` is legal (UTC) |
| createdDatetime | String | Yes | ISO 8601 |
| lastModifiedDatetime | String | Yes | ISO 8601 |
| deleted | Boolean | No | Only present when true |

**GSI:** `userId-checkinDate-index` — enables date-range queries for recovery trend data.

### groups

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| userId | String | Yes | Partition key |
| groupId | String | Yes | Sort key (UUID) |
| name | String | Yes | Group name |
| exerciseIds | List\<String\> | Yes | Ordered list of exercise UUIDs |
| isCustom | Boolean | Yes | Whether group is user-created or built-in |
| sortOrder | Number | Yes | Display order (integer) |
| createdTimezone | String | Yes | e.g. "America/Los_Angeles" |
| createdUtcOffsetSeconds | Number | No | Seconds EAST of UTC at creation (negative in the Americas). Absent on records from older clients — fall back to resolving `createdTimezone` against `createdDatetime`. `0` is legal (UTC) |
| createdDatetime | String | Yes | ISO 8601 |
| lastModifiedDatetime | String | Yes | ISO 8601 |
| deleted | Boolean | No | Only present when true |

---

## Sessions Service

### generated-sessions

One record per `POST /sessions/generate` request, plus one aggregate item per user. The two
shapes share the table and are separated by sort key.

**Request record** — `sessionId` is a UUID:

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| userId | String | Yes | Partition key |
| sessionId | String | Yes | Sort key (UUID) |
| createdDatetime | String | Yes | ISO 8601 |
| chips | List\<String\> | Yes | Context chips as sent, capped at 8 |
| note | String | Yes | The user's free text, RAW and always stored — including when moderation rejected it. A violation count without the text behind it cannot distinguish real abuse from an over-eager filter |
| noteUsed | Boolean | Yes | False when the note was withheld from the model |
| moderationStatus | String | Yes | `ok` \| `flagged` \| `flagged_self_harm` \| `error` \| `absent` |
| moderationCategories | List\<String\> | Yes | Populated when flagged |
| outcome | String | Yes | `ok` \| `nothing_to_recommend` \| `invalid` \| `timeout` \| `failed` |
| durationMs | Number | Yes | Generation wall time |
| model | String | Yes | Generation model used |
| modelResponse | Map | No | `{summary, lifts[]}` — absent when generation never returned |

**Violation aggregate** — `sessionId` is the literal `#violations`:

| Field | Type | Notes |
|-------|------|-------|
| violationCount | Number | Incremented via `ADD` on abuse verdicts only. Not `error` (our failure), not `flagged_self_harm` (not abuse) |
| lastViolationAt | String | ISO 8601 |

Attribute names avoid DynamoDB reserved words on purpose — `outcome` not `status`,
`modelResponse` not `items`, `durationMs` not `duration`. `PutItem` uses no expression so
reserved names would work today and fail the first time anyone writes a query.

**No TTL — retained indefinitely.** This is the only table holding user-written free text
permanently, so it is covered by `scripts/delete_user.py`. Nothing outside the sessions
service reads it; the violation count in particular is never returned by any endpoint.

---

## Entitlements Service

### entitlement-grants

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| userId | String | Yes | Partition key |
| startUtc | String | Yes | Sort key (ISO 8601) |
| endUtc | String | Yes | Subscription end date |
| entitlementName | String | Yes | e.g. "premium" |
| paymentPlatformSource | String | Yes | "apple" (future: "google", "stripe") |
| originalTransactionId | String | Yes | Apple transaction ID |
| productId | String | Yes | Apple product ID |
| createdDatetime | String | Yes | ISO 8601 |
| lastModifiedDatetime | String | Yes | ISO 8601 |

**GSI:** `userId-endUtc-index` -- query active subscriptions (endUtc > now).
Conditional write prevents duplicate `userId + startUtc` entries.

---

## Cross-Table Relationships

```
users ──── user-properties     (userId)
  │
  ├────── exercises            (userId)
  │         │
  │         ├── lift-sets      (exerciseId → exerciseItemId)
  │         │     │
  │         │     └── estimated-1rm  (liftSetId → liftSetId)
  │         │
  │         └── estimated-1rm  (exerciseId → exerciseItemId)
  │
  ├────── set-plans            (userId, activeSetPlanId in user-properties)
  │
  ├────── groups               (userId)
  │
  ├────── recovery-checkins    (userId)
  │
  └────── entitlement-grants   (userId)
```

## Design Patterns

- **User isolation:** All tables partition on `userId` from JWT -- enforced server-side
- **Soft deletes:** Checkin entities use `deleted: true` flag, filtered on read
- **Decimal handling:** Numeric values stored as DynamoDB Decimal, converted to float in responses
- **Timestamps:** All ISO 8601 strings, `lastModifiedDatetime` updated on every write
- **Pagination:** GSIs on `createdDatetime` with `ScanIndexForward=False` for reverse-chronological
