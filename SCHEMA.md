# WeightApp Backend - DynamoDB Schema

## Tables Overview

| Service | Table | Partition Key | Sort Key | GSI | TTL | Soft Delete |
|---------|-------|---------------|----------|-----|-----|-------------|
| Auth | users | userId | - | emailAddress-index | - | No |
| Auth | password-reset-codes | userId | - | - | expiryTime | No |
| User | user-properties | userId | - | - | - | No |
| Checkin | exercises | userId | exerciseItemId | - | - | Yes |
| Checkin | lift-sets | userId | liftSetId | userId-createdDatetime-index | - | Yes |
| Checkin | estimated-1rm | userId | liftSetId | userId-createdDatetime-index | - | Yes |
| Entitlements | entitlement-grants | userId | startUtc | userId-endUtc-index | - | No |

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
| createdDatetime | String | Yes | ISO 8601 |
| lastModifiedDatetime | String | Yes | ISO 8601 |

Auto-created when a user registers.

---

## Checkin Service

### exercises

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| userId | String | Yes | Partition key |
| exerciseItemId | String | Yes | Sort key (UUID) |
| name | String | Yes | |
| isCustom | Boolean | Yes | |
| loadType | String | Yes | "Barbell", "Bodyweight + Single Load", or "Single Load" |
| createdTimezone | String | Yes | e.g. "America/Los_Angeles" |
| createdDatetime | String | Yes | ISO 8601 |
| lastModifiedDatetime | String | Yes | ISO 8601 |
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
| createdDatetime | String | Yes | ISO 8601 |
| lastModifiedDatetime | String | Yes | ISO 8601 |
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
| createdDatetime | String | Yes | ISO 8601 |
| lastModifiedDatetime | String | Yes | ISO 8601 |
| deleted | Boolean | No | Only present when true |

**GSI:** `userId-createdDatetime-index` -- enables "most recent first" pagination.

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
  └────── entitlement-grants   (userId)
```

## Design Patterns

- **User isolation:** All tables partition on `userId` from JWT -- enforced server-side
- **Soft deletes:** Checkin entities use `deleted: true` flag, filtered on read
- **Decimal handling:** Numeric values stored as DynamoDB Decimal, converted to float in responses
- **Timestamps:** All ISO 8601 strings, `lastModifiedDatetime` updated on every write
- **Pagination:** GSIs on `createdDatetime` with `ScanIndexForward=False` for reverse-chronological
