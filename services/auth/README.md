# Auth Service

User authentication microservice providing login and registration endpoints.

## Overview

The auth service handles user authentication operations including:
- User registration
- User login
- Token generation (stub implementation)

## API Endpoints

### POST /auth/register

Register a new user account.

**Request Body:**
```json
{
  "email": "user@example.com",
  "password": "securepassword123",
  "name": "John Doe"
}
```

**Success Response (201 Created):**
```json
{
  "message": "User registered successfully (stub)",
  "user": {
    "email": "user@example.com",
    "name": "John Doe",
    "created_at": "2024-01-01T12:00:00"
  }
}
```

**Error Response (400 Bad Request):**
```json
{
  "error": "Missing required fields",
  "message": "email, password, and name are all required"
}
```

### POST /auth/login

Authenticate an existing user.

**Request Body:**
```json
{
  "email": "user@example.com",
  "password": "securepassword123"
}
```

**Success Response (200 OK):**
```json
{
  "message": "Login successful (stub)",
  "token": "stub-token-123",
  "user": {
    "email": "user@example.com",
    "name": "John Doe"
  }
}
```

**Error Response (400 Bad Request):**
```json
{
  "error": "Missing required fields",
  "message": "Both email and password are required"
}
```

## Architecture

### DynamoDB Table

**Table Name:** `project-cdk-users-{environment}`

**Schema:**
- **Partition Key**: `email` (String)
- **Attributes**:
  - `email` - User's email address
  - `name` - User's display name
  - `password_hash` - Hashed password (bcrypt/argon2)
  - `created_at` - Account creation timestamp
  - `updated_at` - Last modification timestamp

**Indexes:** None (simple key schema)

**Configuration:**
- **Billing Mode**: PAY_PER_REQUEST (on-demand)
- **Staging**: Point-in-time recovery disabled, DESTROY removal policy
- **Production**: Point-in-time recovery enabled, RETAIN removal policy

### Lambda Functions

#### Login Function

- **Name**: `project-cdk-auth-login-{environment}`
- **Runtime**: Python 3.12
- **Memory**: 512MB
- **Timeout**: 30 seconds
- **Handler**: `handlers.login.handler`
- **Permissions**: Read access to Users table

**Environment Variables:**
- `USERS_TABLE_NAME` - DynamoDB table name
- `ENVIRONMENT` - Current environment (staging/production)
- `LOG_LEVEL` - Logging level (INFO/DEBUG)

#### Register Function

- **Name**: `project-cdk-auth-register-{environment}`
- **Runtime**: Python 3.12
- **Memory**: 512MB
- **Timeout**: 30 seconds
- **Handler**: `handlers.register.handler`
- **Permissions**: Read/write access to Users table

**Environment Variables:**
- `USERS_TABLE_NAME` - DynamoDB table name
- `ENVIRONMENT` - Current environment (staging/production)
- `LOG_LEVEL` - Logging level (INFO/DEBUG)

### API Gateway

- **API Name**: `project-cdk-auth-{environment}`
- **Type**: REST API
- **Stage**: {environment} (staging/production)
- **CORS**: Enabled for all origins (restrict in production)
- **Logging**: INFO level with data trace
- **Metrics**: Enabled

## Code Structure

```
services/auth/
├── lambda/
│   ├── handlers/
│   │   ├── login.py          # Login endpoint handler
│   │   └── register.py       # Registration endpoint handler
│   ├── models/
│   │   └── user.py           # User data model (Pydantic)
│   └── utils/
│       └── response.py       # API Gateway response utilities
├── infrastructure/
│   └── auth_stack.py         # CDK stack definition
├── tests/
│   ├── unit/
│   │   └── test_handlers.py  # Unit tests
│   └── integration/          # Integration tests (placeholder)
├── requirements.txt          # Lambda runtime dependencies
└── README.md                 # This file
```

## Local Development

### Install Dependencies

```bash
# Install Lambda runtime dependencies
cd services/auth
pip install -r requirements.txt

# Or install from project root
make install-dev
```

### Run Unit Tests

```bash
# From project root
make test

# Or directly with pytest
pytest services/auth/tests/unit -v

# With coverage
pytest services/auth/tests/unit --cov=services/auth/lambda --cov-report=html
```

### Test Locally

You can test Lambda handlers locally:

```python
from services.auth.lambda.handlers import login_handler, register_handler
import json

# Test login handler
event = {
    "body": json.dumps({
        "email": "test@example.com",
        "password": "password123"
    })
}
response = login_handler(event, {})
print(response)
```

## Testing Deployed API

### Using curl

```bash
# Set your API endpoint
API_ENDPOINT="https://[API-ID].execute-api.us-west-1.amazonaws.com/staging"

# Test registration
curl -X POST ${API_ENDPOINT}/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test@example.com",
    "password": "securepass123",
    "name": "Test User"
  }'

# Test login
curl -X POST ${API_ENDPOINT}/auth/login \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test@example.com",
    "password": "securepass123"
  }'
```

### Using Python

```python
import requests

API_ENDPOINT = "https://[API-ID].execute-api.us-west-1.amazonaws.com/staging"

# Register
response = requests.post(
    f"{API_ENDPOINT}/auth/register",
    json={
        "email": "test@example.com",
        "password": "securepass123",
        "name": "Test User"
    }
)
print(response.json())

# Login
response = requests.post(
    f"{API_ENDPOINT}/auth/login",
    json={
        "email": "test@example.com",
        "password": "securepass123"
    }
)
print(response.json())
```

## Current Implementation Status

### ✅ Implemented

- API Gateway REST API with CORS
- Lambda functions for login and register
- DynamoDB table for user storage
- Request validation
- Error handling
- Response formatting with CORS headers
- Unit tests
- CloudWatch logging
- IAM permissions (least privilege)

### 🚧 Stub Implementation (TODO)

The following features are stubbed and need implementation:

1. **Password Hashing**
   - Current: Plaintext password in request
   - TODO: Hash passwords with bcrypt/argon2 before storage
   - Location: `handlers/register.py`

2. **DynamoDB Operations**
   - Current: No actual database writes/reads
   - TODO: Implement boto3 DynamoDB operations
   - Location: `handlers/login.py`, `handlers/register.py`

3. **JWT Token Generation**
   - Current: Returns stub token "stub-token-123"
   - TODO: Generate real JWT tokens with claims
   - Location: `handlers/login.py`

4. **Token Validation**
   - Current: No token validation
   - TODO: Add middleware to validate JWT tokens
   - New file: `lambda/middleware/auth.py`

5. **Email Validation**
   - Current: Basic Pydantic validation
   - TODO: Add email verification flow
   - New endpoints: `/auth/verify-email`, `/auth/resend-verification`

6. **Duplicate User Check**
   - Current: No check for existing users
   - TODO: Query DynamoDB before registration
   - Location: `handlers/register.py`

7. **Password Policy**
   - Current: No password complexity requirements
   - TODO: Enforce password strength rules
   - New file: `lambda/utils/password.py`

## Production Checklist

Before going to production, implement:

- [ ] Replace stub implementations with real logic
- [ ] Add password hashing (bcrypt/argon2)
- [ ] Implement JWT token generation and validation
- [ ] Add DynamoDB read/write operations
- [ ] Check for duplicate users on registration
- [ ] Implement password complexity requirements
- [ ] Add rate limiting (API Gateway usage plans)
- [ ] Restrict CORS to specific origins
- [ ] Add email verification flow
- [ ] Implement password reset functionality
- [ ] Add refresh token mechanism
- [ ] Set up CloudWatch alarms for errors
- [ ] Enable X-Ray tracing for debugging
- [ ] Add integration tests
- [ ] Implement proper logging (structured logs)
- [ ] Add API Gateway request validation models
- [ ] Set up WAF rules for API protection
- [ ] Document all API endpoints (OpenAPI/Swagger)
- [ ] Add monitoring dashboard
- [ ] Implement audit logging
- [ ] Add multi-factor authentication (MFA)

## Implementation Guide

### Adding Real DynamoDB Operations

Update `handlers/register.py`:

```python
import boto3
import hashlib
from datetime import datetime

dynamodb = boto3.resource('dynamodb')

def handler(event, context):
    # ... validation code ...

    table_name = os.environ['USERS_TABLE_NAME']
    table = dynamodb.Table(table_name)

    # Check if user exists
    response = table.get_item(Key={'email': email})
    if 'Item' in response:
        return create_response(400, {'error': 'User already exists'})

    # Hash password (use bcrypt in production!)
    password_hash = hashlib.sha256(password.encode()).hexdigest()

    # Create user
    table.put_item(Item={
        'email': email,
        'name': name,
        'password_hash': password_hash,
        'created_at': datetime.utcnow().isoformat()
    })

    return create_response(201, {'message': 'User created', 'user': {'email': email, 'name': name}})
```

### Adding JWT Token Generation

Install dependencies:
```bash
pip install pyjwt
```

Update `handlers/login.py`:

```python
import jwt
from datetime import datetime, timedelta

SECRET_KEY = os.environ['JWT_SECRET_KEY']  # Store in Secrets Manager!

def handler(event, context):
    # ... validation and user lookup ...

    # Generate JWT token
    payload = {
        'email': user['email'],
        'name': user['name'],
        'exp': datetime.utcnow() + timedelta(hours=24),
        'iat': datetime.utcnow()
    }
    token = jwt.encode(payload, SECRET_KEY, algorithm='HS256')

    return create_response(200, {'token': token, 'user': {'email': user['email'], 'name': user['name']}})
```

## Monitoring

### CloudWatch Logs

View Lambda logs:

```bash
# Login function logs
aws logs tail /aws/lambda/project-cdk-staging-auth-login --follow

# Register function logs
aws logs tail /aws/lambda/project-cdk-staging-auth-register --follow
```

### CloudWatch Metrics

Key metrics to monitor:
- Lambda invocation count
- Lambda error count
- Lambda duration
- API Gateway 4xx errors
- API Gateway 5xx errors
- DynamoDB read/write capacity (if provisioned)
- DynamoDB throttled requests

### CloudWatch Alarms

Set up alarms for:
- Lambda errors > 5 in 5 minutes
- API Gateway 5xx errors > 10 in 5 minutes
- Lambda duration > 10 seconds

## Security Considerations

### Current Security Measures

- HTTPS-only (API Gateway enforced)
- CORS headers for cross-origin requests
- Input validation on all endpoints
- Error handling prevents information disclosure
- Least privilege IAM roles
- CloudWatch logging for audit trail

### Security Enhancements Needed

1. **Password Security**: Implement bcrypt/argon2 hashing
2. **Secrets Management**: Use AWS Secrets Manager for JWT secret
3. **Rate Limiting**: Add API Gateway throttling
4. **Token Expiration**: Implement token refresh mechanism
5. **Input Sanitization**: Prevent injection attacks
6. **CORS Restriction**: Limit to known frontend domains
7. **MFA**: Add multi-factor authentication
8. **Account Lockout**: Prevent brute force attacks
9. **WAF Rules**: Add AWS WAF for API protection
10. **Encryption**: Enable encryption at rest for DynamoDB (default)

## Troubleshooting

### Lambda Function Errors

Check environment variables are set:
```bash
aws lambda get-function-configuration \
  --function-name project-cdk-staging-auth-login \
  --query 'Environment'
```

### DynamoDB Access Issues

Verify IAM role has correct permissions:
```bash
aws iam get-role-policy \
  --role-name project-cdk-staging-auth-LoginFunctionRole-XXX \
  --policy-name LoginFunctionRoleDefaultPolicy-XXX
```

### API Gateway 403 Errors

Check CORS configuration if calling from browser:
- Ensure preflight OPTIONS requests are handled
- Verify response includes CORS headers
- Check browser console for CORS errors

## Contributing

When extending this service:

1. Add new handlers in `lambda/handlers/`
2. Update `auth_stack.py` to create Lambda functions
3. Add API Gateway routes
4. Write unit tests in `tests/unit/`
5. Update this README with new endpoints
6. Test thoroughly before deploying to production

## Support

For issues specific to the auth service:
- Check CloudWatch logs for Lambda errors
- Review DynamoDB table items in AWS Console
- Test endpoints with verbose curl output: `curl -v ...`
- Verify API Gateway logs in CloudWatch
