"""Staging environment configuration."""

from aws_cdk import RemovalPolicy, Duration
from aws_cdk.aws_dynamodb import BillingMode
from .base import PROJECT_NAME, ACCOUNT_ID, REGION

# Environment Name
ENVIRONMENT = "staging"

# Custom Domain
API_SUBDOMAIN = "api-staging"

# Website
WEBSITE_SUBDOMAIN = "staging"
CLOUDFRONT_CERT_ARN = "arn:aws:acm:us-east-1:569134947863:certificate/04f56f59-62aa-46ae-b1ee-ffb9a2cf6331"

# Removal Policy - DESTROY for staging allows easy cleanup
REMOVAL_POLICY = RemovalPolicy.DESTROY

# CloudWatch Logs Configuration
# Log retention is set via `make set-log-retention-staging` (30 days)
LOG_LEVEL = "INFO"

# DynamoDB Configuration
DYNAMODB_BILLING_MODE = BillingMode.PAY_PER_REQUEST  # On-demand pricing
DYNAMODB_POINT_IN_TIME_RECOVERY = False  # Not needed for staging

# Lambda Configuration
LAMBDA_MEMORY_SIZE = 512  # MB
LAMBDA_TIMEOUT = Duration.seconds(60)
LAMBDA_RUNTIME_VERSION = "3.12"

# Auth Token Configuration
REFRESH_TOKEN_EXPIRATION_MINUTES = 43200  # 30 days (30 * 24 * 60)

# Tags applied to all resources
TAGS = {
    "Project": PROJECT_NAME,
    "Environment": "staging",
    "ManagedBy": "CDK",
    "CostCenter": "development",
}
