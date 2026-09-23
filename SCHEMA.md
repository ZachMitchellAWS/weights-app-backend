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
| User | apns-tokens | userId | apnsToken | - | - | No |
| Notifications | notification-tasks | shard | dueAtTaskId | userId-index | ttl | No |
| Notifications | notification-log | userId | sentAtTaskId | - | ttl | No |

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

---

## Notifications Service

### apns-tokens

Lives in the **user stack**, not the notifications stack — the user Lambda writes it on
registration while the notifications Lambda needs to read `user-properties`, and owning it in
notifications would force a circular stack dependency. See the note in `app.py`.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| userId | String | Yes | Partition key |
| apnsToken | String | Yes | Sort key. Composite so one user can hold several devices |
| apnsEnvironment | String | Yes | `sandbox` / `production` / NULL — a property of the BUILD, not of our backend env |
| invalid | Boolean | Yes | Apple rejected it permanently (410 Unregistered / BadDeviceToken). `false` at creation |
| invalidatedAt | String | Yes | ISO 8601, or NULL |
| invalidReason | String | Yes | Apple's `reason` truncated to 200 chars, or NULL |
| loggedOut | Boolean | Yes | The account signed out on this device. `false` at creation, reset by re-registration |
| lastRegisteredUtc | String | Yes | ISO 8601, or NULL. Written by the user service |
| lastDeliveredUtc | String | Yes | ISO 8601, or NULL. Written by notifications on a successful push |
| backfilledFrom | String | Yes | `user-properties` if the row was created lazily by a send, else NULL |
| createdDatetime | String | Yes | ISO 8601. Set once; later writes preserve it |
| lastModifiedDatetime | String | Yes | ISO 8601 |

**Every attribute is always present.** All three creation paths — registration, delivery
backfill, and invalidation — go through a writer that initialises the full attribute set:
`false` for the flags, DynamoDB NULL for the not-yet-known timestamps. Nothing is left absent,
so "logged out is false" and "nobody has written logged out" are not the same thing on disk.

**A token is sendable when `invalid` and `loggedOut` are both not-true.** The reads still use
`is not True` rather than `is False`, because rows written before this invariant existed are
still out there and a token should not be muted by its own age. Two implementations must agree:
`_write` in `services/notifications/lambda/utils/tokens.py` and `_sync_apns_token` in
`services/user/lambda/handlers/user.py` — the services cannot share code, so change both.

**`apnsEnvironment` is not derivable from our environment.** Xcode rewrites `aps-environment`
to `production` for App Store *and TestFlight* builds, so a TestFlight build of the staging app
produces a production token. Since `APP_BUNDLE_ID_SUFFIX` is empty in both xcconfigs, staging
and production also share one bundle id, so the staging table legitimately holds both kinds.

### notification-tasks

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| shard | Number | Yes | Partition key, 0–99. Random at write time |
| dueAtTaskId | String | Yes | Sort key, `{dueBinUtc}#{taskId}` |
| taskId | String | Yes | UUID |
| userId | String | Yes | Who to notify |
| notificationType | String | Yes | Registry key — the task carries NO content |
| createdAt | String | Yes | ISO 8601 |
| claimedAt | String | No | Present only while a worker holds it; stale after 30 min |
| bypassPrecondition | Boolean | No | **Test affordance.** Delivers even if the type's precondition now fails — e.g. nudging an account that has already unlocked its tier. Checked with `is True`. Never set by bulk scheduling; `schedule_tier_nudges.py` refuses the flag without `--user-id`. A send that used it is recorded with a `-bypass` suffix on the log row's `pathway` |
| ttl | Number | Yes | 30 days. Orphan backstop only |

**Ripeness is a main-table key condition, not a GSI**: the sort key leads with a fixed-width
UTC bin, so `dueAtTaskId < f"{next_bin}#"` finds everything due, *including overdue work* —
which is what makes a missed cron self-healing.

**`userId-index`** is unused by the scheduler. It exists for the moment something enqueues
automatically and must ask "does this user already have one pending?" before writing another.

### notification-log

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| userId | String | Yes | Partition key |
| sentAtTaskId | String | Yes | Sort key, `{sentAtUtc}#{uuid}` |
| notificationType | String | Yes | |
| outcome | String | Yes | See below |
| tokenSuffix | String | No | **Last 8 characters only — never the full token** |
| apnsStatusCode | Number | No | |
| apnsId | String | No | Apple's `apns-id` header |
| apnsReason | String | No | Apple's `reason` on failure |
| apnsEnvironment | String | No | Which host actually answered |
| taskId | String | No | Absent for direct sends |
| pathway | String | No | `scheduled` or `direct` |
| ttl | Number | Yes | 90 days |

`outcome` is one of `delivered`, `skipped_precondition`, `skipped_no_token`,
`skipped_unknown_type`, `failed_invalid_token`, `failed_transient`.

**Attempts that sent nothing are logged, not dropped.** The question this table answers is "why
did this user not get a notification", and a table of successes cannot answer it.

### generated-sessions — retry accounting

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| attempts | Number | Yes | Model calls this request took. 1 for almost everything; 2 means the first attempt's connection dropped and enough budget survived to try again |
| retryReason | String | Yes | `"connection"` when a retry fired, NULL otherwise |

**A second attempt is not automatic.** `openai_client.MIN_RETRY_BUDGET_SECONDS` (16s) gates it:
median generation is 15.7s against a ~25s budget, so retrying with less than that left converts
a fast, actionable failure into a full-deadline wait for the same Retry button. Only
`APIConnectionError` qualifies — a timeout has already spent the budget, and a 4xx is
deterministic.

Both attempts share ONE deadline, measured from the first call. That is what keeps two attempts
inside API Gateway's fixed 29s ceiling; a fresh deadline per attempt could reach ~46s and return
a bare 504 with no body the client can act on.

Rows written before this shipped carry neither attribute — **treat absent as 1**.

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
