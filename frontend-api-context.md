# Project CDK API - Frontend Integration Guide

## Base Configuration

**Environment:** Staging
**Base URL:** `https://1hlfq3bzb9.execute-api.us-west-1.amazonaws.com/staging`
**API Key:** `UWLqSbcHeo1ibSEqQvPbU5lUoa6cKf8f835qWblO`

**Required Headers for ALL Requests:**
```javascript
{
  "x-api-key": "UWLqSbcHeo1ibSEqQvPbU5lUoa6cKf8f835qWblO",
  "Content-Type": "application/json"
}
```

**Required Headers for Protected Endpoints:**
```javascript
{
  "x-api-key": "UWLqSbcHeo1ibSEqQvPbU5lUoa6cKf8f835qWblO",
  "Content-Type": "application/json",
  "Authorization": "Bearer <accessToken>"
}
```

---

## Authentication Flow

### Overview
The API uses JWT-based authentication with access tokens (15-minute expiry) and refresh tokens (30-day expiry).

**Authentication Flow:**
1. User creates account or logs in → Receives `accessToken` and `refreshToken`
2. Store both tokens securely (localStorage, sessionStorage, or memory)
3. Include `accessToken` in `Authorization: Bearer <token>` header for protected endpoints
4. When access token expires (after 15 minutes), use refresh token to get new access token
5. On logout, call logout endpoint to invalidate refresh token

---

## API Endpoints

### 1. Create User
**Endpoint:** `POST /auth/create-user`
**Authentication:** API Key only
**Description:** Creates a new user account and returns JWT tokens. Sends welcome email.

**Request:**
```json
{
  "emailAddress": "user@example.com",
  "password": "securePassword123"
}
```

**Response (201 Created):**
```json
{
  "userId": "123e4567-e89b-12d3-a456-426614174000",
  "emailAddress": "user@example.com",
  "accessToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refreshToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "expiresIn": 900
}
```

**Error (400 Bad Request):**
```json
{
  "message": "Email and password are required"
}
```

**Error (409 Conflict):**
```json
{
  "message": "User with this email already exists"
}
```

**What Happens:**
- User account created in database
- Password hashed with SHA256 + pepper
- User properties record created
- Welcome email sent asynchronously
- JWT tokens generated and returned

---

### 2. Login
**Endpoint:** `POST /auth/login`
**Authentication:** API Key only
**Description:** Authenticates existing user and returns JWT tokens.

**Request:**
```json
{
  "emailAddress": "user@example.com",
  "password": "securePassword123"
}
```

**Response (200 OK):**
```json
{
  "userId": "123e4567-e89b-12d3-a456-426614174000",
  "emailAddress": "user@example.com",
  "accessToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refreshToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "expiresIn": 900
}
```

**Error (401 Unauthorized):**
```json
{
  "message": "Invalid email or password"
}
```

---

### 3. Refresh Token
**Endpoint:** `POST /auth/refresh`
**Authentication:** API Key only
**Description:** Refreshes expired access token using valid refresh token.

**Request:**
```json
{
  "refreshToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
}
```

**Response (200 OK):**
```json
{
  "userId": "123e4567-e89b-12d3-a456-426614174000",
  "accessToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "expiresIn": 900
}
```

**Error (401 Unauthorized):**
```json
{
  "message": "Invalid or expired refresh token"
}
```

**When to Use:**
- Access token expired (after 15 minutes)
- Before making API call if token might be expired
- To maintain user session without re-login

---

### 4. Logout
**Endpoint:** `POST /auth/logout`
**Authentication:** API Key + JWT Access Token (Both Required)
**Description:** Logs out user by invalidating their refresh token.

**Request:**
- No request body
- Requires `x-api-key` header with API key
- Requires `Authorization: Bearer <accessToken>` header with JWT token

**Response (200 OK):**
```json
{
  "message": "Logged out successfully"
}
```

**Error (401 Unauthorized):**
```json
{
  "message": "Invalid or expired token"
}
```

**Error (403 Forbidden):**
```json
{
  "message": "Forbidden"
}
```
*Note: 403 error occurs when API key is missing or invalid*

**What Happens:**
- Refresh token removed from database
- User cannot refresh access token anymore
- Current access token remains valid until expiration (15 min)
- User must login again to get new tokens

---

### 5. Initiate Password Reset
**Endpoint:** `POST /auth/initiate-password-reset`
**Authentication:** API Key only
**Description:** Sends 6-digit reset code to user's email. Always returns success for security.

**Request:**
```json
{
  "emailAddress": "user@example.com"
}
```

**Response (200 OK - Always):**
```json
{
  "message": "If an account exists for this email, a reset code has been sent"
}
```

**Security Features:**
- Always returns success (doesn't reveal if email exists)
- 6-digit code expires in 1 hour
- Rate limited to 3 attempts per hour per user
- Code sent via email using AWS SES

---

### 6. Confirm Password Reset
**Endpoint:** `POST /auth/confirm-password-reset`
**Authentication:** API Key only
**Description:** Validates reset code and updates password.

**Request:**
```json
{
  "emailAddress": "user@example.com",
  "code": "123456",
  "newPassword": "newSecurePassword123"
}
```

**Response (200 OK):**
```json
{
  "message": "Password reset successfully"
}
```

**Error (400 Bad Request):**
```json
{
  "message": "Invalid request format"
}
```

**Error (401 Unauthorized):**
```json
{
  "message": "Invalid or expired reset code"
}
```

**What Happens:**
- Validates 6-digit code
- Updates password (hashed with SHA256 + pepper)
- Deletes reset code (single-use)
- User can immediately login with new password

---

### 7. Get User Properties
**Endpoint:** `GET /user/properties`
**Authentication:** API Key + JWT Access Token (Both Required)
**Description:** Retrieves user properties for authenticated user.

**Request:**
- No request body
- Requires `x-api-key` header with API key
- Requires `Authorization: Bearer <accessToken>` header with JWT token
- User ID extracted from JWT token

**Response (200 OK):**
```json
{
  "userId": "123e4567-e89b-12d3-a456-426614174000",
  "placeholderBool": true,
  "createdDatetime": "2026-01-23T10:30:00.000Z",
  "lastModifiedDatetime": "2026-01-23T10:30:00.000Z"
}
```

**Error (401 Unauthorized):**
```json
{
  "message": "Invalid or expired token"
}
```

**Error (403 Forbidden):**
```json
{
  "message": "Forbidden"
}
```
*Note: 403 error occurs when API key is missing or invalid*

**Error (404 Not Found):**
```json
{
  "message": "User properties not found"
}
```

---

### 8. Update User Properties
**Endpoint:** `POST /user/properties`
**Authentication:** API Key + JWT Access Token (Both Required)
**Description:** Updates user properties for authenticated user.

**Request:**
- Requires `x-api-key` header with API key
- Requires `Authorization: Bearer <accessToken>` header with JWT token
- User ID extracted from JWT token

```json
{
  "placeholderBool": false
}
```

**Response (200 OK):**
```json
{
  "userId": "123e4567-e89b-12d3-a456-426614174000",
  "placeholderBool": false,
  "createdDatetime": "2026-01-23T10:30:00.000Z",
  "lastModifiedDatetime": "2026-01-23T14:25:00.000Z"
}
```

**Error (400 Bad Request):**
```json
{
  "message": "Invalid request format"
}
```

**Error (401 Unauthorized):**
```json
{
  "message": "Invalid or expired token"
}
```

**Error (403 Forbidden):**
```json
{
  "message": "Forbidden"
}
```
*Note: 403 error occurs when API key is missing or invalid*

---

## Frontend Implementation Examples

### JavaScript/TypeScript Example

```typescript
// API Configuration
const API_CONFIG = {
  baseUrl: 'https://1hlfq3bzb9.execute-api.us-west-1.amazonaws.com/staging',
  apiKey: 'UWLqSbcHeo1ibSEqQvPbU5lUoa6cKf8f835qWblO'
};

// Helper function for API calls
async function apiCall(endpoint: string, options: RequestInit = {}) {
  const headers = {
    'x-api-key': API_CONFIG.apiKey,
    'Content-Type': 'application/json',
    ...options.headers
  };

  const response = await fetch(`${API_CONFIG.baseUrl}${endpoint}`, {
    ...options,
    headers
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.message || 'API request failed');
  }

  return response.json();
}

// Helper function for authenticated API calls
async function authenticatedApiCall(endpoint: string, options: RequestInit = {}) {
  const accessToken = localStorage.getItem('accessToken');

  if (!accessToken) {
    throw new Error('No access token found');
  }

  return apiCall(endpoint, {
    ...options,
    headers: {
      ...options.headers,
      'Authorization': `Bearer ${accessToken}`
    }
  });
}

// Example: Create User
async function createUser(email: string, password: string) {
  const response = await apiCall('/auth/create-user', {
    method: 'POST',
    body: JSON.stringify({ emailAddress: email, password })
  });

  // Store tokens
  localStorage.setItem('accessToken', response.accessToken);
  localStorage.setItem('refreshToken', response.refreshToken);
  localStorage.setItem('userId', response.userId);

  return response;
}

// Example: Login
async function login(email: string, password: string) {
  const response = await apiCall('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ emailAddress: email, password })
  });

  // Store tokens
  localStorage.setItem('accessToken', response.accessToken);
  localStorage.setItem('refreshToken', response.refreshToken);
  localStorage.setItem('userId', response.userId);

  return response;
}

// Example: Refresh Token
async function refreshAccessToken() {
  const refreshToken = localStorage.getItem('refreshToken');

  if (!refreshToken) {
    throw new Error('No refresh token found');
  }

  const response = await apiCall('/auth/refresh', {
    method: 'POST',
    body: JSON.stringify({ refreshToken })
  });

  // Update access token
  localStorage.setItem('accessToken', response.accessToken);

  return response;
}

// Example: Get User Properties
async function getUserProperties() {
  return authenticatedApiCall('/user/properties', {
    method: 'GET'
  });
}

// Example: Update User Properties
async function updateUserProperties(placeholderBool: boolean) {
  return authenticatedApiCall('/user/properties', {
    method: 'POST',
    body: JSON.stringify({ placeholderBool })
  });
}

// Example: Logout
async function logout() {
  await authenticatedApiCall('/auth/logout', {
    method: 'POST'
  });

  // Clear tokens
  localStorage.removeItem('accessToken');
  localStorage.removeItem('refreshToken');
  localStorage.removeItem('userId');
}

// Example: Initiate Password Reset
async function initiatePasswordReset(email: string) {
  return apiCall('/auth/initiate-password-reset', {
    method: 'POST',
    body: JSON.stringify({ emailAddress: email })
  });
}

// Example: Confirm Password Reset
async function confirmPasswordReset(email: string, code: string, newPassword: string) {
  return apiCall('/auth/confirm-password-reset', {
    method: 'POST',
    body: JSON.stringify({ emailAddress: email, code, newPassword })
  });
}
```

---

### React Example with Axios

```typescript
import axios from 'axios';

// Create axios instance
const api = axios.create({
  baseURL: 'https://1hlfq3bzb9.execute-api.us-west-1.amazonaws.com/staging',
  headers: {
    'x-api-key': 'UWLqSbcHeo1ibSEqQvPbU5lUoa6cKf8f835qWblO',
    'Content-Type': 'application/json'
  }
});

// Add request interceptor for auth token
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('accessToken');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// Add response interceptor for token refresh
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const originalRequest = error.config;

    // If 401 and we haven't retried yet, try to refresh token
    if (error.response?.status === 401 && !originalRequest._retry) {
      originalRequest._retry = true;

      try {
        const refreshToken = localStorage.getItem('refreshToken');
        const response = await axios.post(
          'https://1hlfq3bzb9.execute-api.us-west-1.amazonaws.com/staging/auth/refresh',
          { refreshToken },
          {
            headers: {
              'x-api-key': 'UWLqSbcHeo1ibSEqQvPbU5lUoa6cKf8f835qWblO'
            }
          }
        );

        localStorage.setItem('accessToken', response.data.accessToken);
        originalRequest.headers.Authorization = `Bearer ${response.data.accessToken}`;

        return api(originalRequest);
      } catch (refreshError) {
        // Refresh failed, redirect to login
        localStorage.clear();
        window.location.href = '/login';
        return Promise.reject(refreshError);
      }
    }

    return Promise.reject(error);
  }
);

// Export configured axios instance
export default api;

// Usage in components:
// import api from './api';
// const response = await api.post('/auth/login', { emailAddress, password });
```

---

## Token Management Best Practices

### 1. Store Tokens Securely
- Use `httpOnly` cookies for production (prevents XSS attacks)
- Use `localStorage` or `sessionStorage` for development
- Never store tokens in regular cookies without `httpOnly` flag

### 2. Handle Token Expiration
- Access token expires in 15 minutes
- Implement automatic refresh before expiration
- Handle 401 responses by attempting token refresh
- Redirect to login if refresh fails

### 3. Token Refresh Strategy
```typescript
// Option 1: Refresh proactively before expiration
setInterval(async () => {
  try {
    await refreshAccessToken();
  } catch (error) {
    // Redirect to login
  }
}, 13 * 60 * 1000); // Refresh every 13 minutes (before 15-min expiry)

// Option 2: Refresh on 401 response (see axios interceptor example above)
```

### 4. Logout Properly
- Always call `/auth/logout` endpoint
- Clear all stored tokens
- Redirect to login page

---

## Error Handling

All error responses follow this format:
```json
{
  "message": "Error description"
}
```

**Common HTTP Status Codes:**
- `200 OK` - Request successful
- `201 Created` - Resource created successfully (user registration)
- `400 Bad Request` - Invalid request format or missing required fields
- `401 Unauthorized` - Invalid credentials, expired token, or missing authentication
- `403 Forbidden` - API key missing or invalid
- `404 Not Found` - Resource not found
- `409 Conflict` - Resource already exists (e.g., duplicate email)
- `500 Internal Server Error` - Server-side error

**Example Error Handling:**
```typescript
try {
  const response = await login(email, password);
  // Handle success
} catch (error) {
  if (error.response) {
    // Server responded with error
    switch (error.response.status) {
      case 400:
        console.error('Invalid request:', error.response.data.message);
        break;
      case 401:
        console.error('Authentication failed:', error.response.data.message);
        break;
      case 403:
        console.error('API key invalid or missing');
        break;
      default:
        console.error('Error:', error.response.data.message);
    }
  } else {
    // Network error or other issue
    console.error('Network error:', error.message);
  }
}
```

---

## CORS Configuration

The API has CORS enabled for all origins with the following headers:
- `Access-Control-Allow-Origin: *`
- `Access-Control-Allow-Methods: *`
- `Access-Control-Allow-Headers: Content-Type, X-Amz-Date, Authorization, X-Api-Key, X-Amz-Security-Token`

**Note:** In production, CORS should be restricted to specific frontend domains.

---

## Testing with cURL

### Create User
```bash
curl -X POST https://1hlfq3bzb9.execute-api.us-west-1.amazonaws.com/staging/auth/create-user \
  -H "x-api-key: UWLqSbcHeo1ibSEqQvPbU5lUoa6cKf8f835qWblO" \
  -H "Content-Type: application/json" \
  -d '{"emailAddress":"test@example.com","password":"password123"}'
```

### Login
```bash
curl -X POST https://1hlfq3bzb9.execute-api.us-west-1.amazonaws.com/staging/auth/login \
  -H "x-api-key: UWLqSbcHeo1ibSEqQvPbU5lUoa6cKf8f835qWblO" \
  -H "Content-Type: application/json" \
  -d '{"emailAddress":"test@example.com","password":"password123"}'
```

### Get User Properties (Protected)
```bash
curl -X GET https://1hlfq3bzb9.execute-api.us-west-1.amazonaws.com/staging/user/properties \
  -H "x-api-key: UWLqSbcHeo1ibSEqQvPbU5lUoa6cKf8f835qWblO" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN_HERE"
```

---

## Rate Limits and Quotas

**Current Configuration:**
- No throttling limits
- No quota limits
- Password reset: 3 attempts per hour per user

**Future Considerations:**
- May add rate limiting in production
- May add request quotas per API key

---

## Support and Questions

For API issues or questions:
1. Check CloudWatch Logs in AWS Console
2. Verify API key is correct
3. Ensure all required headers are included
4. Check token expiration
5. Review error response messages
