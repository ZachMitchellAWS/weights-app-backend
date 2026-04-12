"""Production environment configuration."""

from aws_cdk import RemovalPolicy, Duration
from aws_cdk.aws_dynamodb import BillingMode
from .base import PROJECT_NAME, ACCOUNT_ID, REGION

# Environment Name
ENVIRONMENT = "production"

# Custom Domain
API_SUBDOMAIN = "api"

# Website
WEBSITE_SUBDOMAIN = ""  # apex domain
CLOUDFRONT_CERT_ARN = "arn:aws:acm:us-east-1:569134947863:certificate/ba3eb50a-2c90-4c91-8ada-f13eb6bc7dbb"

# Removal Policy - RETAIN for production protects data
REMOVAL_POLICY = RemovalPolicy.RETAIN

# CloudWatch Logs Configuration
# Log retention is set via `make set-log-retention-production` (90 days)
LOG_LEVEL = "INFO"

# DynamoDB Configuration
DYNAMODB_BILLING_MODE = BillingMode.PAY_PER_REQUEST  # On-demand pricing
DYNAMODB_POINT_IN_TIME_RECOVERY = True  # Enable PITR for production data protection

# Lambda Configuration
LAMBDA_MEMORY_SIZE = 512  # MB
LAMBDA_TIMEOUT = Duration.seconds(29)  # Match API Gateway timeout
LAMBDA_RUNTIME_VERSION = "3.12"

# Auth Token Configuration
ACCESS_TOKEN_EXPIRATION_MINUTES = 60  # 1 hour
REFRESH_TOKEN_EXPIRATION_MINUTES = 5256000  # 10 years (10 * 365 * 24 * 60)

# Tags applied to all resources
TAGS = {
    "Project": PROJECT_NAME,
    "Environment": "production",
    "ManagedBy": "CDK",
    "CostCenter": "production",
}
