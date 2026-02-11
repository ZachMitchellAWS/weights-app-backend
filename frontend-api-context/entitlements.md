# Entitlements Service

Manages subscription entitlements via Apple App Store Server API integration.

---

## GET /entitlements/status
Returns current account subscription status.

**Auth:** API Key + Access Token

**Response (200):**
```json
{
  "accountStatus": "free",
  "expirationUtc": null
}
```

Or for premium users:
```json
{
  "accountStatus": "premium",
  "expirationUtc": "2025-12-01T09:32:55.000"
}
```

**Response Fields:**
| Field | Type | Description |
|-------|------|-------------|
| `accountStatus` | string | `"free"` or `"premium"` |
| `expirationUtc` | string \| null | ISO 8601 expiration date, or null if free |

---

## POST /entitlements
Process Apple transactions and create entitlement grants.

**Auth:** API Key + Access Token

**Request:**
```json
{
  "apple": {
    "originalTransactionIds": ["1000000123456789"]
  }
}
```

**Request Fields:**
| Field | Type | Description |
|-------|------|-------------|
| `apple.originalTransactionIds` | string[] | Array of Apple original transaction IDs |

**Response (200):**
```json
{
  "activeEntitlements": [
    {
      "userId": "uuid",
      "startUtc": "2025-11-01T09:32:55.000",
      "endUtc": "2025-12-01T09:32:55.000",
      "entitlementName": "premium",
      "paymentPlatformSource": "apple",
      "originalTransactionId": "1000000123456789",
      "productId": "com.app.premium.monthly",
      "createdDatetime": "2025-11-01T09:32:55.000Z",
      "lastModifiedDatetime": "2025-11-01T09:32:55.000Z"
    }
  ],
  "created": 2,
  "skipped": 1
}
```

**Response Fields:**
| Field | Type | Description |
|-------|------|-------------|
| `activeEntitlements` | array | List of currently active entitlement grants |
| `created` | number | Number of new entitlement grants created |
| `skipped` | number | Number of grants skipped (already existed) |

**Usage:**
- Call after a successful in-app purchase to sync entitlements
- Uses `originalTransactionId` (not regular transactionId) from StoreKit
- Creates grants for all subscription periods in transaction history

---

## POST /entitlements/apple-notification
Apple Server Notification V2 webhook endpoint.

**Auth:** API Key only (no JWT - Apple cannot authenticate)

**Request:** Apple's signed notification payload (handled automatically by Apple)

**Response (200 - always):**
```json
{
  "message": "ok"
}
```

**Behavior:**
- Always returns 200 to Apple (even on errors)
- Extracts `userId` from `appAccountToken` in notification
- Verifies user exists before processing
- Fetches transaction history and creates entitlement grants

---

## iOS Integration

### Setting appAccountToken
When initiating purchases, set `appAccountToken` to the user's `userId`:

```swift
let purchase = try await product.purchase(options: [
    .appAccountToken(UUID(uuidString: userId)!)
])
```

This links Apple transactions to your user accounts.

### After Purchase
Call `POST /entitlements` with the `originalTransactionId`:

```swift
// Get originalTransactionId from transaction
let originalId = transaction.originalID

// Sync with backend
let response = await api.post("/entitlements", body: [
    "apple": [
        "originalTransactionIds": [originalId]
    ]
])
```

### Checking Status
Call `GET /entitlements/status` to check subscription state:

```swift
let status = await api.get("/entitlements/status")
if status.accountStatus == "premium" {
    // Enable premium features
}
```

---

## App Store Connect Setup

### Webhook Configuration
1. Go to App Store Connect > Your App > App Information
2. Scroll to "App Store Server Notifications"
3. Set Production URL: `{baseUrl}/production/entitlements/apple-notification`
4. Set Sandbox URL: `{baseUrl}/staging/entitlements/apple-notification`

### Required SSM Parameters
Before deploying, configure these in AWS SSM Parameter Store:

| Parameter | Description |
|-----------|-------------|
| `/{project}/{env}/entitlements/apple-private-key` | .p8 file contents from App Store Connect |
| `/{project}/{env}/entitlements/apple-key-id` | Key ID from App Store Connect |
| `/{project}/{env}/entitlements/apple-issuer-id` | Issuer ID from App Store Connect |
| `/{project}/{env}/entitlements/apple-bundle-id` | Your app's bundle ID |

---

## Entitlement Grant Schema

| Field | Type | Description |
|-------|------|-------------|
| `userId` | string | User's unique identifier (partition key) |
| `startUtc` | string | Subscription start date - ISO 8601 (sort key) |
| `endUtc` | string | Subscription end date - ISO 8601 |
| `entitlementName` | string | Type of entitlement (e.g., "premium") |
| `paymentPlatformSource` | string | Payment platform ("apple") |
| `originalTransactionId` | string | Apple's original transaction ID |
| `productId` | string | Apple's product ID |
| `createdDatetime` | string | When grant was created |
| `lastModifiedDatetime` | string | When grant was last modified |
