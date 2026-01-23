"""Production environment configuration."""

from aws_cdk import RemovalPolicy, Duration
from aws_cdk.aws_logs import RetentionDays
from aws_cdk.aws_dynamodb import BillingMode
from .base import PROJECT_NAME, ACCOUNT_ID, REGION

# Environment Name
ENVIRONMENT = "production"

# Removal Policy - RETAIN for production protects data
REMOVAL_POLICY = RemovalPolicy.RETAIN

# CloudWatch Logs Configuration
LOG_RETENTION = RetentionDays.ONE_MONTH  # 30 days retention for production
LOG_LEVEL = "INFO"

# DynamoDB Configuration
DYNAMODB_BILLING_MODE = BillingMode.PAY_PER_REQUEST  # On-demand pricing
DYNAMODB_POINT_IN_TIME_RECOVERY = True  # Enable PITR for production data protection

# Lambda Configuration
LAMBDA_MEMORY_SIZE = 512  # MB
LAMBDA_TIMEOUT = Duration.seconds(30)
LAMBDA_RUNTIME_VERSION = "3.12"

# Tags applied to all resources
TAGS = {
    "Project": PROJECT_NAME,
    "Environment": "production",
    "ManagedBy": "CDK",
    "CostCenter": "production",
}
