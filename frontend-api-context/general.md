# General API Context

## Environment Configuration

**Environment:** Staging
**Base URL:** `https://h49ho1pn62.execute-api.us-west-1.amazonaws.com/staging`
**API Key:** `VedgMnwCCw6gSxybUQxLi1aTpHVEUz5t2u1NC9K3`

---

## Required Headers

### All Requests
| Header | Value |
|--------|-------|
| `x-api-key` | `VedgMnwCCw6gSxybUQxLi1aTpHVEUz5t2u1NC9K3` |
| `Content-Type` | `application/json` |

### Protected Endpoints (additional)
| Header | Value |
|--------|-------|
| `Authorization` | `Bearer <accessToken>` |

---

## Authentication

### Token Types
| Token | Expiry | Purpose |
|-------|--------|---------|
| Access Token | 15 minutes | Used in `Authorization` header for protected endpoints |
| Refresh Token | 30 days | Used to obtain new access tokens without re-login |

### Authentication Flow
1. **Login/Create Account** → Receive both tokens
2. **Make API calls** → Include access token in `Authorization: Bearer <token>` header
3. **Token expires** → Use refresh token to get new access token
4. **Logout** → Invalidates refresh token; access token remains valid until expiry

### Token Refresh Strategy
- Access tokens expire after 15 minutes
- Refresh proactively before expiration, or reactively on 401 response
- If refresh fails (401), user must re-authenticate

---

## Error Handling

### HTTP Status Codes
| Code | Meaning |
|------|---------|
| 200 | Success |
| 201 | Created |
| 400 | Bad request (invalid format, missing fields) |
| 401 | Unauthorized (invalid/expired token, bad credentials) |
| 403 | Forbidden (missing/invalid API key) |
| 404 | Not found |
| 409 | Conflict (e.g., duplicate email) |
| 500 | Server error |

### Error Response Format
```json
{
  "message": "Error description"
}
```

Some endpoints include additional detail:
```json
{
  "error": "Validation failed",
  "message": "One or more exercises have validation errors",
  "errors": ["Exercise at index 0: missing fields: name"]
}
```

### 401 vs 403
- **401 Unauthorized**: Token issue (expired, invalid, missing)
- **403 Forbidden**: API key issue (missing or invalid)

---

## Data Ownership & Security

### User ID Source
The `userId` is extracted from the JWT access token by the Lambda Authorizer. It is never sent in request bodies for protected endpoints.

### Resource Ownership
- `userId` is the DynamoDB partition key for user data
- Users can only access/modify their own resources
- Attempting to access another user's resources results in `notFoundIds` or 404

---

## CORS

Currently configured for all origins:
- `Access-Control-Allow-Origin: *`
- `Access-Control-Allow-Methods: *`
- `Access-Control-Allow-Headers: Content-Type, X-Amz-Date, Authorization, X-Api-Key, X-Amz-Security-Token`

---

## Rate Limits

| Endpoint | Limit |
|----------|-------|
| Password reset | 3 attempts per hour per user |
| Other endpoints | No current limits |
