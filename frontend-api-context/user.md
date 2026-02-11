# User Service

User service endpoints for managing user-specific properties.

---

## GET /user/properties
Retrieves user properties.

**Auth:** API Key + Access Token

**Response (200):**
```json
{
  "userId": "uuid",
  "availableChangePlates": [],
  "bodyweight": 185.5,
  "createdDatetime": "2026-01-23T10:30:00.000Z",
  "lastModifiedDatetime": "2026-01-23T10:30:00.000Z"
}
```

Note: `bodyweight` may be absent if not yet set.

---

## POST /user/properties
Updates user properties. Partial updates supported - only include fields you want to change.

**Auth:** API Key + Access Token

**Request (all fields optional, at least one required):**
```json
{
  "bodyweight": 185.5,
  "availableChangePlates": [2.5, 5, 10, 25, 35, 45]
}
```

**Field Details:**
| Field | Type | Description |
|-------|------|-------------|
| `bodyweight` | number \| null | User's bodyweight. Send `null` to remove. |
| `availableChangePlates` | number[] | List of available change plate weights |

**Response (200):**
```json
{
  "userId": "uuid",
  "bodyweight": 185.5,
  "availableChangePlates": [2.5, 5, 10, 25, 35, 45],
  "createdDatetime": "2026-01-23T10:30:00.000Z",
  "lastModifiedDatetime": "2026-01-23T14:25:00.000Z"
}
```
