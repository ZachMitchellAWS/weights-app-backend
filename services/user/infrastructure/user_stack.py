"""User service CDK stack with DynamoDB, Lambda, and API Gateway integration."""

import os

from aws_cdk import (
    Stack,
    aws_dynamodb as dynamodb,
    aws_lambda as lambda_,
    aws_apigateway as apigateway,
)
from constructs import Construct
from pathlib import Path


class UserStack(Stack):
    """
    User service stack containing:
    - DynamoDB table for user properties
    - Lambda function for user operations
    - API Gateway routes for user endpoints (integrated into existing auth API)

    This stack creates a user_properties table and Lambda function that handles
    user-specific operations like getting and updating user properties.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        project_name: str,
        env_name: str,
        config: any,
        api: apigateway.RestApi,
        authorizer: apigateway.TokenAuthorizer,
        **kwargs
    ) -> None:
        """
        Initialize the UserStack.

        Args:
            scope: CDK scope
            construct_id: Unique identifier for this stack
            project_name: Project name for resource naming
            env_name: Environment name (staging/production)
            config: Configuration module (staging.py or production.py)
            api: Existing API Gateway RestApi from auth stack
            authorizer: Lambda Authorizer from auth stack
            **kwargs: Additional stack arguments
        """
        super().__init__(scope, construct_id, **kwargs)

        # Store configuration for use in resource creation
        self.project_name = project_name
        self.env_name = env_name
        self.config = config
        self.api = api
        self.authorizer = authorizer

        # Create resources
        self.user_properties_table = self._create_user_properties_table()
        self.deletion_requests_table = self._create_deletion_requests_table()
        self.feedback_table = self._create_feedback_table()
        self.dependencies_layer = self._create_dependencies_layer()
        self.user_function = self._create_user_lambda()
        self._create_api_routes()

    def _create_user_properties_table(self) -> dynamodb.Table:
        """
        Create DynamoDB table for user properties storage.

        Design decisions:
        - userId as partition key: Links properties to user
        - PAY_PER_REQUEST billing: Cost-effective for variable workloads
        - Point-in-time recovery: Environment-specific (enabled in production)
        - Removal policy: Environment-specific (DESTROY in staging, RETAIN in production)

        Table schema:
        - userId (String, partition key): User's unique identifier
        - placeholderBool (Boolean): Example boolean property
        - createdDatetime (String): ISO 8601 timestamp when created
        - lastModifiedDatetime (String): ISO 8601 timestamp when last modified

        Returns:
            DynamoDB Table construct
        """
        table = dynamodb.Table(
            self,
            "UserPropertiesTable",
            table_name=f"{self.project_name}-{self.env_name}-user-properties",
            partition_key=dynamodb.Attribute(
                name="userId",
                type=dynamodb.AttributeType.STRING
            ),
            billing_mode=self.config.DYNAMODB_BILLING_MODE,
            point_in_time_recovery=self.config.DYNAMODB_POINT_IN_TIME_RECOVERY,
            removal_policy=self.config.REMOVAL_POLICY,
        )

        return table

    def _create_deletion_requests_table(self) -> dynamodb.Table:
        """Create DynamoDB table for account deletion requests."""
        table = dynamodb.Table(
            self,
            "AccountDeletionRequestsTable",
            table_name=f"{self.project_name}-{self.env_name}-account-deletion-requests",
            partition_key=dynamodb.Attribute(
                name="userId",
                type=dynamodb.AttributeType.STRING
            ),
            billing_mode=self.config.DYNAMODB_BILLING_MODE,
            point_in_time_recovery=self.config.DYNAMODB_POINT_IN_TIME_RECOVERY,
            removal_policy=self.config.REMOVAL_POLICY,
        )

        return table

    def _create_feedback_table(self) -> dynamodb.Table:
        """Create DynamoDB table for user feedback submissions."""
        table = dynamodb.Table(
            self,
            "FeedbackTable",
            table_name=f"{self.project_name}-{self.env_name}-feedback",
            partition_key=dynamodb.Attribute(
                name="userId",
                type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="createdDatetime",
                type=dynamodb.AttributeType.STRING
            ),
            billing_mode=self.config.DYNAMODB_BILLING_MODE,
            point_in_time_recovery=self.config.DYNAMODB_POINT_IN_TIME_RECOVERY,
            removal_policy=self.config.REMOVAL_POLICY,
        )

        return table

    def _create_dependencies_layer(self) -> lambda_.LayerVersion:
        """Create Lambda layer with Python dependencies for user service."""
        layer_path = Path(__file__).parent.parent / "layer"
        layer = lambda_.LayerVersion(
            self,
            "DependenciesLayer",
            layer_version_name=f"{self.project_name}-{self.env_name}-user-deps",
            code=lambda_.Code.from_asset(str(layer_path)),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            description="Python dependencies for user service",
        )
        return layer

    def _create_user_lambda(self) -> lambda_.Function:
        """
        Create Lambda function for user service operations.

        This function handles:
        - GET /user/properties: Retrieve user properties
        - POST /user/properties: Update user properties

        Both endpoints require authentication via Lambda Authorizer.
        The user ID is extracted from the authorizer context.

        Returns:
            Lambda Function construct
        """
        # Path to Lambda code (services/user/lambda directory)
        lambda_code_path = Path(__file__).parent.parent / "lambda"

        function = lambda_.Function(
            self,
            "UserFunction",
            function_name=f"{self.project_name}-{self.env_name}-user",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handlers.user.handler",
            code=lambda_.Code.from_asset(str(lambda_code_path)),
            layers=[self.dependencies_layer],
            memory_size=self.config.LAMBDA_MEMORY_SIZE,
            timeout=self.config.LAMBDA_TIMEOUT,
            environment={
                "USER_PROPERTIES_TABLE_NAME": self.user_properties_table.table_name,
                "DELETION_REQUESTS_TABLE_NAME": self.deletion_requests_table.table_name,
                "FEEDBACK_TABLE_NAME": self.feedback_table.table_name,
                "ENVIRONMENT": self.config.ENVIRONMENT,
                "SENTRY_DSN": os.environ.get("SENTRY_DSN", ""),
                "LOG_LEVEL": self.config.LOG_LEVEL,
            },
            log_retention=self.config.LOG_RETENTION,
        )

        # Grant read/write permissions to DynamoDB tables
        self.user_properties_table.grant_read_write_data(function)
        self.deletion_requests_table.grant_read_write_data(function)
        self.feedback_table.grant_read_write_data(function)

        return function

    def _create_api_routes(self) -> None:
        """
        Create API Gateway routes for user service.

        Adds routes to the existing API Gateway from the auth stack:
        - GET /user/properties → user_function (requires JWT auth)
        - POST /user/properties → user_function (requires JWT auth)

        Both endpoints use the Lambda Authorizer to validate JWT tokens
        and extract the user ID from the token.
        """
        # Create Lambda integration
        user_integration = apigateway.LambdaIntegration(
            self.user_function,
            proxy=True,  # Lambda proxy integration passes full request to handler
        )

        # Create /user resource
        user_resource = self.api.root.add_resource("user")

        # Create /user/properties resource
        properties_resource = user_resource.add_resource("properties")

        # Add GET method (requires API key + JWT authentication)
        properties_resource.add_method(
            "GET",
            user_integration,
            api_key_required=True,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.CUSTOM,
        )

        # Add POST method (requires API key + JWT authentication)
        properties_resource.add_method(
            "POST",
            user_integration,
            api_key_required=True,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.CUSTOM,
        )

        # Create /user/feedback resource
        feedback_resource = user_resource.add_resource("feedback")

        # Add POST method (requires API key + JWT authentication)
        feedback_resource.add_method(
            "POST",
            user_integration,
            api_key_required=True,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.CUSTOM,
        )

        # Create /user/delete-account resource
        delete_account_resource = user_resource.add_resource("delete-account")

        # Add POST method (requires API key + JWT authentication)
        delete_account_resource.add_method(
            "POST",
            user_integration,
            api_key_required=True,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.CUSTOM,
        )
