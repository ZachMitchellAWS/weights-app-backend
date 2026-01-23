# Python AWS CDK Microservices Bootstrap

A production-ready Python-based AWS CDK microservices monorepo with an auth service example, supporting staging and production environments.

## Overview

This project provides a complete foundation for building microservices on AWS using:

- **AWS CDK** - Infrastructure as Code with Python
- **AWS Lambda** - Serverless compute for API handlers
- **Amazon API Gateway** - RESTful API endpoints
- **Amazon DynamoDB** - NoSQL database for user data
- **CloudWatch Logs** - Centralized logging
- **Environment-based Configuration** - Separate staging and production settings

## Technology Stack

- **Language**: Python 3.12
- **IaC Framework**: AWS CDK 2.118.0
- **Runtime**: AWS Lambda (Python 3.12)
- **Database**: Amazon DynamoDB
- **API**: Amazon API Gateway (REST API)
- **Testing**: pytest with coverage
- **Linting**: flake8, mypy
- **Formatting**: black

## Architecture

```
┌─────────────┐
│   Client    │
└──────┬──────┘
       │
       ▼
┌─────────────────┐
│  API Gateway    │
│  /auth/login    │
│  /auth/register │
└────────┬────────┘
         │
    ┌────┴────┐
    │         │
    ▼         ▼
┌────────┐ ┌────────┐
│ Login  │ │Register│
│ Lambda │ │ Lambda │
└───┬────┘ └───┬────┘
    │          │
    └────┬─────┘
         ▼
    ┌──────────┐
    │ DynamoDB │
    │  Users   │
    └──────────┘
```

## Prerequisites

Before getting started, ensure you have:

- **Python 3.9+** installed
- **AWS CLI** configured with valid credentials for account `569134947863`
- **AWS CDK CLI** installed: `npm install -g aws-cdk`
- **Make** (standard on macOS/Linux, available via Git Bash on Windows)
- **AWS Account**: 569134947863 (pre-configured in this project)

## Quick Start

Get up and running in minutes:

```bash
# 1. Install dependencies
make install

# 2. Bootstrap CDK (first time only)
make bootstrap-staging

# 3. Deploy to staging
make deploy-staging
```

After deployment completes, the API Gateway endpoint URL will be displayed in the outputs.

## Manual Configuration Steps

After your first deployment, you must configure secrets in AWS Systems Manager Parameter Store. These secrets are used by the Lambda functions for JWT token signing and password hashing.

### 1. Configure SSM Parameters

The deployment creates SSM parameters with placeholder values. You must update them with actual secret values:

**For Staging Environment:**

```bash
# Update JWT secret key (used for signing JWT tokens)
aws ssm put-parameter \
  --name "/project/staging/auth/jwt-secret-key" \
  --value "your-actual-jwt-secret-here-change-this" \
  --type SecureString \
  --overwrite \
  --region us-west-1

# Update password pepper (used for password hashing)
aws ssm put-parameter \
  --name "/project/staging/auth/password-pepper" \
  --value "your-actual-pepper-here-change-this" \
  --type SecureString \
  --overwrite \
  --region us-west-1
```

**For Production Environment:**

```bash
# Update JWT secret key
aws ssm put-parameter \
  --name "/project/production/auth/jwt-secret-key" \
  --value "your-production-jwt-secret-here" \
  --type SecureString \
  --overwrite \
  --region us-west-1

# Update password pepper
aws ssm put-parameter \
  --name "/project/production/auth/password-pepper" \
  --value "your-production-pepper-here" \
  --type SecureString \
  --overwrite \
  --region us-west-1
```

**Generating Strong Secrets:**

Use strong random strings for these values. You can generate them using:

```bash
# Generate a random secret (macOS/Linux)
openssl rand -base64 32

# Or using Python
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

**Important Notes:**
- Use **different** secrets for staging and production
- Store these secrets securely (password manager, secrets vault)
- Never commit these values to version control
- These parameters are encrypted using AWS KMS
- Lambda functions read these values at runtime (cached for performance)

### 2. Verify Configuration

After updating the SSM parameters, verify they're set correctly:

```bash
# List all auth parameters for staging
aws ssm get-parameters-by-path \
  --path "/project/staging/auth" \
  --with-decryption \
  --region us-west-1

# Check specific parameter value
aws ssm get-parameter \
  --name "/project/staging/auth/jwt-secret-key" \
  --with-decryption \
  --region us-west-1 \
  --query "Parameter.Value" \
  --output text
```

### 3. Test Authentication

After configuring SSM parameters, test that authentication works:

```bash
# Create a test user
curl -X POST ${API_ENDPOINT}/auth/create-user \
  -H "Content-Type: application/json" \
  -d '{
    "emailAddress": "test@example.com",
    "password": "testpassword123"
  }'

# Login with the test user
curl -X POST ${API_ENDPOINT}/auth/login \
  -H "Content-Type: application/json" \
  -d '{
    "emailAddress": "test@example.com",
    "password": "testpassword123"
  }'
```

If you see JWT tokens in the response, the configuration is working correctly.

## Available Make Commands

### Installation

```bash
make install        # Install CDK dependencies
make install-dev    # Install development dependencies (includes testing tools)
```

### CDK Operations

```bash
# Staging Environment
make bootstrap-staging    # Bootstrap CDK for staging (first time only)
make synth-staging        # Synthesize CloudFormation templates
make diff-staging         # Preview changes before deploying
make deploy-staging       # Deploy to staging
make destroy-staging      # Destroy staging resources

# Production Environment
make bootstrap-production    # Bootstrap CDK for production (first time only)
make synth-production        # Synthesize CloudFormation templates
make diff-production         # Preview changes before deploying
make deploy-production       # Deploy to production (with confirmation)
make destroy-production      # Destroy production resources (with confirmation)
```

### Development

```bash
make test      # Run unit tests with coverage
make lint      # Run flake8 and mypy linting
make format    # Format code with black
make clean     # Remove generated files and caches
```

## Configuration

The project includes environment-specific configuration.

### Project Name (`config/base.py`)

The project name is defined centrally in `config/base.py`:

```python
PROJECT_NAME = "project-cdk"
```

**To change the project name**, edit this value in `config/base.py`. This will automatically update all resource names across the entire project.

**Resource Naming Convention**: All AWS resources follow the pattern:
```
{project_name}-{environment}-{description}
```

Examples:
- Stack: `project-cdk-staging-auth`
- DynamoDB Table: `project-cdk-staging-users`
- Lambda Function: `project-cdk-staging-auth-login`
- API Gateway: `project-cdk-staging-auth`

### Staging Environment (`config/staging.py`)

- **Account**: 569134947863
- **Region**: us-west-1
- **Removal Policy**: DESTROY (allows easy cleanup)
- **Log Retention**: 7 days
- **DynamoDB**: PAY_PER_REQUEST billing, no point-in-time recovery
- **Lambda**: 512MB memory, 30s timeout

### Production Environment (`config/production.py`)

- **Account**: 569134947863
- **Region**: us-west-1
- **Removal Policy**: RETAIN (protects data)
- **Log Retention**: 30 days
- **DynamoDB**: PAY_PER_REQUEST billing, point-in-time recovery enabled
- **Lambda**: 512MB memory, 30s timeout

## Project Structure

```
project-cdk/
├── app.py                          # CDK app entry point
├── cdk.json                        # CDK configuration
├── Makefile                        # All commands
├── requirements.txt                # CDK dependencies
├── requirements-dev.txt            # Dev dependencies
├── config/                         # Environment configs
│   ├── base.py                    # Project name and shared config
│   ├── staging.py
│   └── production.py
├── infrastructure/                 # Shared infrastructure
│   ├── shared/                     # Cross-service resources
│   └── constructs/                 # Reusable CDK constructs
├── postman/                        # Postman collection & environments
│   ├── project-cdk-api.postman_collection.json
│   ├── staging.postman_environment.json
│   ├── production.postman_environment.json
│   └── README.md                  # Detailed Postman usage guide
├── scripts/                        # Automation scripts
└── services/                       # Microservices
    └── auth/                       # Auth service
        ├── lambda/                 # Lambda function code
        │   ├── handlers/          # Request handlers
        │   ├── models/            # Data models
        │   └── utils/             # Utilities
        ├── infrastructure/        # CDK stack
        │   └── auth_stack.py
        └── tests/                 # Tests
            ├── unit/
            └── integration/
```

## Testing

Run the test suite:

```bash
# Run all unit tests
make test

# Run tests with verbose output
pytest services/auth/tests/unit -v

# Run with coverage report
pytest services/auth/tests/unit --cov=services/auth/lambda --cov-report=html
```

## Testing API Endpoints

### Using Postman (Recommended)

Postman collection and environment files are available in the `postman/` directory:

**Quick Start:**
1. Import `postman/project-cdk-api.postman_collection.json` into Postman
2. Import `postman/staging.postman_environment.json`
3. Select "Project CDK - Staging" environment
4. Login to get tokens (automatically saved)
5. Add your service folders and endpoints
6. All endpoints automatically use saved tokens!

**Features:**
- Switch between staging/production environments
- Automatic token management (saves JWT tokens after login)
- Tokens automatically used in all protected endpoints
- Organized by service (Auth, and add your own)
- Example protected endpoints showing auth pattern
- Detailed request/response documentation
- Test credentials pre-configured

See `postman/README.md` for detailed instructions.

### Using cURL

After deploying, test the API endpoints:

### Login Endpoint

```bash
# Get API endpoint from deployment outputs
API_ENDPOINT="https://[API-ID].execute-api.us-west-1.amazonaws.com/staging"

# Test login
curl -X POST ${API_ENDPOINT}/auth/login \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test@example.com",
    "password": "test123"
  }'

# Expected response:
# {
#   "message": "Login successful (stub)",
#   "token": "stub-token-123",
#   "user": {
#     "email": "test@example.com",
#     "name": "Test User"
#   }
# }
```

### Register Endpoint

```bash
# Test registration
curl -X POST ${API_ENDPOINT}/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "email": "newuser@example.com",
    "password": "securepass123",
    "name": "New User"
  }'

# Expected response:
# {
#   "message": "User registered successfully (stub)",
#   "user": {
#     "email": "newuser@example.com",
#     "name": "New User",
#     "created_at": "2024-01-01T00:00:00"
#   }
# }
```

## Extending the Project

### Adding a New Microservice

Follow the auth service pattern:

1. Create service directory structure:

```bash
mkdir -p services/my-service/{lambda/{handlers,models,utils},infrastructure,tests/{unit,integration}}
```

2. Implement Lambda handlers in `services/my-service/lambda/handlers/`

3. Create CDK stack in `services/my-service/infrastructure/my_service_stack.py`

4. Import and instantiate in `app.py`:

```python
from services.my_service.infrastructure.my_service_stack import MyServiceStack

my_service_stack = MyServiceStack(
    app,
    f"{project_name}-my-service-{env_name}",
    project_name=project_name,
    env_name=env_name,
    config=config,
    env=env,
)
```

5. Apply tags and deploy

### Adding Shared Infrastructure

Create shared resources in `infrastructure/shared/`:

```python
# infrastructure/shared/network_stack.py
from aws_cdk import Stack
from constructs import Construct

class NetworkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        # Create VPC, subnets, etc.
```

### Creating Reusable Constructs

Build higher-level abstractions in `infrastructure/constructs/`:

```python
# infrastructure/constructs/standard_lambda.py
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

class StandardLambdaFunction(Construct):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id)
        # Create Lambda with standard settings
```

## AWS Resources Created

### Staging Environment

- **DynamoDB Table**: `project-cdk-staging-users`
  - Billing: PAY_PER_REQUEST
  - Point-in-time recovery: Disabled
  - Removal policy: DESTROY

- **Lambda Functions**:
  - `project-cdk-staging-auth-login` (512MB, 30s timeout)
  - `project-cdk-staging-auth-register` (512MB, 30s timeout)

- **API Gateway**: `project-cdk-staging-auth`
  - Stage: staging
  - CORS enabled
  - CloudWatch logging enabled

- **CloudWatch Log Groups**: 7-day retention

- **IAM Roles**: Least privilege access for Lambda functions

### Production Environment

Same resources with production-specific configuration:
- Removal policy: RETAIN
- Point-in-time recovery: Enabled
- Log retention: 30 days

## Estimated Costs

### Staging (Light Development Use)

- **DynamoDB**: $1-5/month (low traffic)
- **Lambda**: Free tier eligible
- **API Gateway**: $1-3/month (low traffic)
- **CloudWatch Logs**: ~$1/month
- **Total**: ~$3-10/month

### Production

Costs scale with usage. Monitor using AWS Cost Explorer.

## Troubleshooting

### CDK Bootstrap Issues

If bootstrap fails:

```bash
# Ensure AWS credentials are configured
aws sts get-caller-identity

# Verify account ID
# Should show: 569134947863

# Re-run bootstrap
make bootstrap-staging
```

### Deployment Failures

Check CloudFormation events:

```bash
# View stack events
aws cloudformation describe-stack-events \
  --stack-name project-cdk-staging-auth \
  --region us-west-1
```

### Lambda Function Errors

View CloudWatch logs:

```bash
# List log groups
aws logs describe-log-groups \
  --log-group-name-prefix /aws/lambda/project-cdk-auth

# Tail logs
aws logs tail /aws/lambda/project-cdk-staging-auth-login --follow
```

### Permission Errors

Ensure your AWS credentials have sufficient permissions:
- CloudFormation full access
- Lambda full access
- DynamoDB full access
- API Gateway full access
- IAM role creation
- CloudWatch Logs write access

## Best Practices Implemented

- **Infrastructure as Code**: All resources defined in CDK
- **Environment Separation**: Distinct staging and production configs
- **Least Privilege IAM**: Functions only get needed permissions
- **Centralized Logging**: All logs in CloudWatch with retention policies
- **Resource Tagging**: All resources tagged for cost allocation
- **CORS Enabled**: API Gateway configured for frontend integration
- **Error Handling**: Comprehensive error handling in Lambda functions
- **Testing**: Unit tests for all handlers
- **Type Safety**: Type hints throughout Python code
- **Code Formatting**: Consistent style with black
- **Documentation**: Extensive comments explaining design decisions

## Development Workflow

Typical development cycle:

```bash
# 1. Make code changes to Lambda handlers or CDK stacks

# 2. Format code
make format

# 3. Run tests
make test

# 4. Preview changes
make diff-staging

# 5. Deploy
make deploy-staging

# 6. Test endpoints
curl -X POST https://[API-ID].execute-api.us-west-1.amazonaws.com/staging/auth/login ...

# 7. View logs if needed
aws logs tail /aws/lambda/project-cdk-staging-auth-login --follow
```

## CI/CD Integration

This project is ready for CI/CD integration. Example GitHub Actions workflow:

```yaml
name: Deploy to Staging
on:
  push:
    branches: [main]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - uses: actions/setup-python@v2
      - uses: actions/setup-node@v2
      - run: npm install -g aws-cdk
      - run: make install
      - run: make test
      - run: make deploy-staging
```

## Security Considerations

- **Secrets Management**: Use AWS Secrets Manager for sensitive data
- **API Authentication**: Implement JWT validation in production
- **Password Hashing**: Use bcrypt or argon2 for password hashing
- **CORS**: Restrict origins in production (not wildcard)
- **Rate Limiting**: Add API Gateway usage plans and throttling
- **Input Validation**: Validate and sanitize all inputs
- **DynamoDB**: Enable encryption at rest (enabled by default)
- **VPC**: Consider VPC Lambda for additional network isolation

## Support

For issues or questions:

- Check the troubleshooting section above
- Review CloudWatch logs for errors
- Examine CDK synthesis output: `make synth-staging`
- Review AWS CloudFormation console for stack events

## License

MIT License - Feel free to use this as a starting point for your projects.

## Next Steps

1. **Implement Real Authentication**: Replace stub implementations with actual DynamoDB operations and JWT token generation
2. **Add More Services**: Follow the auth service pattern to add additional microservices
3. **Set Up CI/CD**: Automate testing and deployment
4. **Add Monitoring**: Create CloudWatch dashboards and alarms
5. **Implement Secrets Management**: Use AWS Secrets Manager for sensitive configuration
6. **Add API Documentation**: Generate OpenAPI/Swagger docs
7. **Implement Integration Tests**: Test deployed resources
8. **Add Custom Domain**: Configure Route53 and ACM for custom domain

Happy building! 🚀
