# Postman Collection - Project CDK API

This directory contains Postman collection and environment files for testing all Project CDK microservices.

## Files

- **`project-cdk-api.postman_collection.json`** - Complete API collection (all services)
- **`staging.postman_environment.json`** - Staging environment configuration
- **`production.postman_environment.json`** - Production environment configuration

## Quick Start

### 1. Import Collection & Environments

1. Open Postman
2. Click **Import** button (top left)
3. Select all 3 JSON files:
   - `project-cdk-api.postman_collection.json`
   - `staging.postman_environment.json`
   - `production.postman_environment.json`
4. Click **Import**

### 2. Select Environment

Click the environment dropdown (top-right) and select:
- **Project CDK - Staging** (for development/testing)
- **Project CDK - Production** (for production use)

### 3. Authenticate

1. Open the **Auth Service** folder
2. Send **Create User** or **Login** request
3. Tokens are automatically saved to environment variables
4. All subsequent requests will use these tokens

### 4. Make Requests

All protected endpoints automatically use the saved access token. Just send requests!

## Collection Structure

```
Project CDK API/
├── Auth Service/
│   ├── Create User          (saves tokens)
│   ├── Login                (saves tokens)
│   ├── Refresh Token        (updates access token)
│   └── Logout               (requires auth, clears tokens)
├── User Service/
│   ├── Get User Properties  (requires auth)
│   └── Update User Properties (requires auth)
└── [Your New Services Here]
```

## Environment Variables

### Base Configuration

| Variable | Description | Example |
|----------|-------------|---------|
| `baseUrl` | API Gateway base URL (without environment) | `https://...amazonaws.com` |
| `environment` | Environment stage name (used in URL path) | `staging` or `production` |

### Authentication (Auto-populated)

| Variable | Description | Set By | Used By |
|----------|-------------|--------|---------|
| `accessToken` | JWT access token (15 min) | Auth endpoints | All protected endpoints |
| `refreshToken` | JWT refresh token (30 days) | Auth endpoints | Refresh endpoint |
| `userId` | Current user's ID | Auth endpoints | Your endpoints |
| `userEmail` | Current user's email | Auth endpoints | Reference |

### Test Credentials

| Variable | Description | Modify As Needed |
|----------|-------------|------------------|
| `userEmail` | Test email address | Yes - change for tests |
| `userPassword` | Test password | Yes - change for tests |

## Authentication Flow

### Initial Authentication

```
1. Create User or Login
   ↓
2. Tokens automatically saved to environment
   ↓
3. Make requests to any protected endpoint
   ↓
4. Token automatically included in Authorization header
```

### Token Refresh (After 15 minutes)

```
1. Access token expires
   ↓
2. Send Refresh Token request
   ↓
3. New access token saved
   ↓
4. Continue making requests
```

## Adding New Services/Endpoints

### Step 1: Create Service Folder

1. Right-click on collection root
2. Select **Add Folder**
3. Name it after your service (e.g., "User Service", "Order Service")

### Step 2: Add Requests

1. Right-click on your new folder
2. Select **Add Request**
3. Configure the request:
   - Set method (GET, POST, etc.)
   - Set URL: `{{baseUrl}}/{{environment}}/your-service/your-endpoint`
   - Add request body if needed

### Step 3: Add Authentication

If your endpoint requires authentication:

1. Go to **Authorization** tab
2. Select **Type**: Bearer Token
3. Set **Token**: `{{accessToken}}`
4. Done! Token is automatically included

### Example: Adding a User Profile Endpoint

```
User Service/
└── Get Profile
    Method: GET
    URL: {{baseUrl}}/{{environment}}/users/profile
    Auth: Bearer Token → {{accessToken}}

    Response will include user profile data
```

### Example: Adding a Create Order Endpoint

```
Order Service/
└── Create Order
    Method: POST
    URL: {{baseUrl}}/{{environment}}/orders/create
    Auth: Bearer Token → {{accessToken}}
    Body: {
      "items": [...],
      "total": 99.99
    }
```

## Protected Endpoint Pattern

All authenticated endpoints follow this pattern:

```json
Authorization: Bearer {{accessToken}}
```

The token is:
- Automatically saved after login
- Automatically included in requests
- Refreshed when expired
- Shared across all services

## Usage Examples

### Example 1: Complete User Flow

```
1. Create User
   → Tokens saved

2. Get User Profile (your new endpoint)
   → Uses saved token automatically

3. Update User Profile (your new endpoint)
   → Uses saved token automatically

4. [15 minutes later] Token expires

5. Refresh Token
   → New access token saved

6. Get User Profile again
   → Uses new token automatically
```

### Example 2: Multi-Service Flow

```
1. Login
   → Tokens saved

2. Create Order (Order Service)
   → Uses token

3. Get User Profile (User Service)
   → Uses same token

4. Process Payment (Payment Service)
   → Uses same token

All services use the same access token!
```

## Environment Switching

Switch between staging and production instantly:

1. Select environment from dropdown
2. All requests use correct `{{baseUrl}}` and `{{environment}}`
3. Maintain separate credentials per environment
4. Test changes in staging before production

### Updating Production URL

After deploying to production:

1. Run `make deploy-production`
2. Copy API Gateway endpoint from output
3. In Postman: **Environments** → **Project CDK - Production**
4. Update `baseUrl` variable
5. Save

## Automated Token Management

The collection includes scripts that automatically:

✅ Save access token after login/create-user
✅ Save refresh token for later use
✅ Update access token after refresh
✅ Log token operations to console
✅ Save user ID and email for reference

You never need to manually copy/paste tokens!

## Testing Tips

### Test Authentication Flow

1. **Create User**: Register new account
2. **Verify Tokens**: Check environment variables are populated
3. **Wait 16+ minutes**: Let access token expire
4. **Try Protected Endpoint**: Should fail with 401
5. **Refresh Token**: Get new access token
6. **Retry Endpoint**: Should succeed

### Test Multiple Services

1. Login once
2. Test endpoints across different services
3. All use the same token
4. Verify token works everywhere

### Test Environment Switching

1. Create user in **Staging**
2. Switch to **Production** environment
3. Create different user
4. Switch back to **Staging**
5. Original credentials still work

## Token Expiration

### Access Token (15 minutes)

- Used for API authentication
- Short expiry for security
- Refresh when expired

### Refresh Token (30 days)

- Used to get new access tokens
- Long expiry for convenience
- Stored securely in database
- Validated on each use

## CI/CD Integration

Run Postman collections in CI/CD pipelines:

```bash
# Install Newman (Postman CLI)
npm install -g newman

# Run all tests in staging
newman run project-cdk-api.postman_collection.json \
  -e staging.postman_environment.json

# Run specific folder
newman run project-cdk-api.postman_collection.json \
  -e staging.postman_environment.json \
  --folder "Auth Service"

# Generate HTML report
newman run project-cdk-api.postman_collection.json \
  -e staging.postman_environment.json \
  -r html
```

## Security Best Practices

⚠️ **Important:**

1. **Never commit tokens** to git (environment files should be in .gitignore if they contain real tokens)
2. **Use test credentials** in staging only
3. **Production credentials** should be unique and secure
4. **Tokens are visible** in Postman - don't share screenshots with tokens
5. **Refresh tokens** are like passwords - treat them securely
6. **HTTPS only** - enforced by API Gateway

## Troubleshooting

### 401 Unauthorized

**Cause**: Access token expired or invalid

**Solution**:
1. Send **Refresh Token** request
2. Or **Login** again

### Token not saved

**Cause**: Script didn't run or request failed

**Solution**:
1. Check **Tests** tab in request
2. Verify response is successful
3. Check Console (View → Show Postman Console)

### Wrong environment

**Cause**: Using staging token in production or vice versa

**Solution**:
1. Check environment dropdown
2. Login in correct environment
3. Tokens are environment-specific

### Base URL not working

**Cause**: Environment variable not set correctly

**Solution**:
1. Go to Environments
2. Select your environment
3. Verify `baseUrl` is correct
4. Save changes

## Support

For API issues:
- Check CloudWatch logs in AWS Console
- Review API Gateway logs
- Verify DynamoDB has correct data
- Use Postman Console to debug (View → Show Postman Console)

## Next Steps

1. ✅ Import collection and environments
2. ✅ Login to get tokens
3. ✅ Test auth endpoints work
4. ⬜ Add your first custom service folder
5. ⬜ Add your first protected endpoint
6. ⬜ Test token authentication works
7. ⬜ Repeat for each new service

Happy testing! 🚀
