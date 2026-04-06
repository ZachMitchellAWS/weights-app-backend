# Auth Service

Authentication endpoints for user management and JWT token operations.

---

## POST /auth/create-user
Creates a new user account.

**Auth:** API Key only

**Request:**
```json
{
  "emailAddress": "user@example.com",
  "password": "securePassword123"
}
```

**Response (201):**
```json
{
  "userId": "uuid",
  "emailAddress": "user@example.com",
  "accessToken": "jwt...",
  "refreshToken": "jwt...",
  "accessTokenExpiresIn": 900,
  "refreshTokenExpiresIn": 2592000
}
```

**Side Effects:**
- Creates user account and user properties record
- Sends welcome email asynchronously

---

## POST /auth/login
Authenticates existing user.

**Auth:** API Key only

**Request:**
```json
{
  "emailAddress": "user@example.com",
  "password": "securePassword123"
}
```

**Response (200):**
```json
{
  "userId": "uuid",
  "emailAddress": "user@example.com",
  "accessToken": "jwt...",
  "refreshToken": "jwt...",
  "accessTokenExpiresIn": 900,
  "refreshTokenExpiresIn": 2592000
}
```

---

## POST /auth/refresh
Obtains new access token using refresh token.

**Auth:** API Key only

**Request:**
```json
{
  "refreshToken": "jwt..."
}
```

**Response (200):**
```json
{
  "userId": "uuid",
  "accessToken": "jwt...",
  "accessTokenExpiresIn": 900
}
```

---

## POST /auth/logout
Invalidates refresh token.

**Auth:** API Key + Access Token

**Request:** No body required

**Response (200):**
```json
{
  "message": "Logged out successfully"
}
```

**Behavior:**
- Stateless: signals client to discard tokens locally
- Refresh token remains technically valid until 30-day expiry
- Client should clear stored tokens on logout

---

## POST /auth/initiate-password-reset
Sends 6-digit reset code via email.

**Auth:** API Key only

**Request:**
```json
{
  "emailAddress": "user@example.com"
}
```

**Response (200 - always):**
```json
{
  "message": "If an account exists for this email, a reset code has been sent"
}
```

**Security:**
- Always returns success (doesn't reveal if email exists)
- Code expires in 1 hour
- Rate limited: 3 attempts per hour per user

---

## POST /auth/confirm-password-reset
Validates reset code and updates password.

**Auth:** API Key only

**Request:**
```json
{
  "emailAddress": "user@example.com",
  "code": "123456",
  "newPassword": "newSecurePassword123"
}
```

**Response (200):**
```json
{
  "message": "Password reset successfully"
}
```
