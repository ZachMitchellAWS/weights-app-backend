"""Entitlements service CDK stack with DynamoDB, Lambda, and API Gateway integration."""

import os

from aws_cdk import (
    Stack,
    aws_dynamodb as dynamodb,
    aws_lambda as lambda_,
    aws_apigateway as apigateway,
    aws_ssm as ssm,
    aws_iam as iam,
)
from constructs import Construct
from pathlib import Path


class EntitlementsStack(Stack):
    """
    Entitlements service stack containing:
    - DynamoDB table for entitlement grants
    - Lambda function for entitlement operations
    - API Gateway routes for entitlement endpoints (integrated into existing API)

    This stack manages subscription entitlements via Apple App Store Server API integration.
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
        users_table_name: str,
        **kwargs
    ) -> None:
        """
        Initialize the EntitlementsStack.

        Args:
            scope: CDK scope
            construct_id: Unique identifier for this stack
            project_name: Project name for resource naming
            env_name: Environment name (staging/production)
            config: Configuration module (staging.py or production.py)
            api: Existing API Gateway RestApi from auth stack
            authorizer: Lambda Authorizer from auth stack
            users_table_name: Name of users DynamoDB table (for webhook user validation)
            **kwargs: Additional stack arguments
        """
        super().__init__(scope, construct_id, **kwargs)

        # Store configuration for use in resource creation
        self.project_name = project_name
        self.env_name = env_name
        self.config = config
        self.api = api
        self.authorizer = authorizer
        self.users_table_name = users_table_name

        # Create resources
        self._create_ssm_parameters()
        self.entitlement_grants_table = self._create_entitlement_grants_table()
        self.subscription_events_table = self._create_subscription_events_table()
        self.dependencies_layer = self._create_dependencies_layer()
        self.entitlements_function = self._create_entitlements_lambda()
        self._create_api_routes()

    def _create_ssm_parameters(self) -> None:
        """
        Create SSM Parameter Store parameters for Apple credentials.

        Creates parameters for:
        - apple-private-key: .p8 file contents
        - apple-key-id: Key ID from App Store Connect
        - apple-issuer-id: Issuer ID from App Store Connect
        - apple-bundle-id: App bundle ID

        Parameters are created with placeholder values. Update them
        with actual secret values using AWS Console or CLI:

        aws ssm put-parameter --name "/{project}/{env}/entitlements/apple-private-key" \
            --value "your-p8-contents" --type SecureString --overwrite

        aws ssm put-parameter --name "/{project}/{env}/entitlements/apple-key-id" \
            --value "your-key-id" --type SecureString --overwrite

        aws ssm put-parameter --name "/{project}/{env}/entitlements/apple-issuer-id" \
            --value "your-issuer-id" --type SecureString --overwrite

        aws ssm put-parameter --name "/{project}/{env}/entitlements/apple-bundle-id" \
            --value "your-bundle-id" --type SecureString --overwrite
        """
        # Create Apple private key parameter
        ssm.StringParameter(
            self,
            "ApplePrivateKeyParameter",
            parameter_name=f"/{self.project_name}/{self.env_name}/entitlements/apple-private-key",
            string_value="PLACEHOLDER-update-with-p8-contents",
            description=f"Apple App Store Connect private key (.p8 contents) for {self.env_name}",
            tier=ssm.ParameterTier.STANDARD,
        )

        # Create Apple key ID parameter
        ssm.StringParameter(
            self,
            "AppleKeyIdParameter",
            parameter_name=f"/{self.project_name}/{self.env_name}/entitlements/apple-key-id",
            string_value="PLACEHOLDER-update-with-key-id",
            description=f"Apple App Store Connect key ID for {self.env_name}",
            tier=ssm.ParameterTier.STANDARD,
        )

        # Create Apple issuer ID parameter
        ssm.StringParameter(
            self,
            "AppleIssuerIdParameter",
            parameter_name=f"/{self.project_name}/{self.env_name}/entitlements/apple-issuer-id",
            string_value="PLACEHOLDER-update-with-issuer-id",
            description=f"Apple App Store Connect issuer ID for {self.env_name}",
            tier=ssm.ParameterTier.STANDARD,
        )

        # Create Apple bundle ID parameter
        ssm.StringParameter(
            self,
            "AppleBundleIdParameter",
            parameter_name=f"/{self.project_name}/{self.env_name}/entitlements/apple-bundle-id",
            string_value="PLACEHOLDER-update-with-bundle-id",
            description=f"Apple app bundle ID for {self.env_name}",
            tier=ssm.ParameterTier.STANDARD,
        )

    def _create_entitlement_grants_table(self) -> dynamodb.Table:
        """
        Create DynamoDB table for entitlement grants.

        Design decisions:
        - userId as partition key: Links grants to user
        - startUtc as sort key: Unique identifier for each subscription period
        - GSI on endUtc: Query active entitlements sorted by expiration
        - PAY_PER_REQUEST billing: Cost-effective for variable workloads
        - Point-in-time recovery: Environment-specific (enabled in production)
        - Removal policy: Environment-specific (DESTROY in staging, RETAIN in production)

        Table schema:
        - userId (String, partition key): User's unique identifier
        - startUtc (String, sort key): Subscription start (ISO 8601)
        - endUtc (String): Subscription end date
        - entitlementName (String): e.g., "premium"
        - paymentPlatformSource (String): "apple" (future: "google", "stripe")
        - originalTransactionId (String): Apple's transaction ID
        - productId (String): Apple's product ID
        - createdDatetime (String): ISO 8601
        - lastModifiedDatetime (String): ISO 8601

        GSI (userId-endUtc-index):
        - Partition key: userId
        - Sort key: endUtc
        - Enables querying active entitlements sorted by expiration

        Returns:
            DynamoDB Table construct
        """
        table = dynamodb.Table(
            self,
            "EntitlementGrantsTable",
            table_name=f"{self.project_name}-{self.env_name}-entitlement-grants",
            partition_key=dynamodb.Attribute(
                name="userId",
                type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="startUtc",
                type=dynamodb.AttributeType.STRING
            ),
            billing_mode=self.config.DYNAMODB_BILLING_MODE,
            point_in_time_recovery=self.config.DYNAMODB_POINT_IN_TIME_RECOVERY,
            removal_policy=self.config.REMOVAL_POLICY,
        )

        # Add GSI for querying active entitlements sorted by expiration
        table.add_global_secondary_index(
            index_name="userId-endUtc-index",
            partition_key=dynamodb.Attribute(
                name="userId",
                type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="endUtc",
                type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        return table

    def _create_subscription_events_table(self) -> dynamodb.Table:
        """
        Create DynamoDB table for subscription event logging.

        Captures every Apple Server Notification V2 event for cohort analysis.
        Write-only from Lambda; reads happen via analysis scripts.

        Table schema:
        - userId (String, partition key): User's unique identifier
        - eventTimestamp (String, sort key): Server-receive time (ISO 8601, ms precision)
        - notificationType (String): Apple notification type (e.g., SUBSCRIBED, EXPIRED)
        - subtype (String): Apple notification subtype (e.g., INITIAL_BUY, VOLUNTARY)
        - originalTransactionId (String): Groups all events for one subscription
        - transactionId (String): Identifies individual renewal periods
        - productId (String): Which plan (monthly/yearly)
        - purchaseDateMs (Number): Raw Apple timestamp
        - expiresDateMs (Number): Raw Apple timestamp

        GSIs:
        - originalTransactionId-eventTimestamp-index: Reconstruct one subscription's lifecycle
        - notificationType-eventTimestamp-index: Cohort queries by event type

        Returns:
            DynamoDB Table construct
        """
        table = dynamodb.Table(
            self,
            "SubscriptionEventsTable",
            table_name=f"{self.project_name}-{self.env_name}-subscription-events",
            partition_key=dynamodb.Attribute(
                name="userId",
                type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="eventTimestamp",
                type=dynamodb.AttributeType.STRING
            ),
            billing_mode=self.config.DYNAMODB_BILLING_MODE,
            point_in_time_recovery=self.config.DYNAMODB_POINT_IN_TIME_RECOVERY,
            removal_policy=self.config.REMOVAL_POLICY,
        )

        # GSI: Reconstruct one subscription's lifecycle
        table.add_global_secondary_index(
            index_name="originalTransactionId-eventTimestamp-index",
            partition_key=dynamodb.Attribute(
                name="originalTransactionId",
                type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="eventTimestamp",
                type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # GSI: Cohort queries by event type
        table.add_global_secondary_index(
            index_name="notificationType-eventTimestamp-index",
            partition_key=dynamodb.Attribute(
                name="notificationType",
                type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="eventTimestamp",
                type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        return table

    def _create_dependencies_layer(self) -> lambda_.LayerVersion:
        """
        Create Lambda Layer with Python dependencies.

        This layer includes app-store-server-library and other dependencies.
        Dependencies must be pre-installed in the layer/python directory:
            cd services/entitlements && pip install -r requirements.txt -t layer/python/

        Returns:
            Lambda LayerVersion construct
        """
        layer_path = Path(__file__).parent.parent / "layer"

        layer = lambda_.LayerVersion(
            self,
            "DependenciesLayer",
            layer_version_name=f"{self.project_name}-{self.env_name}-entitlements-deps",
            code=lambda_.Code.from_asset(str(layer_path)),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            description="Python dependencies for entitlements service (app-store-server-library)",
        )

        return layer

    def _create_entitlements_lambda(self) -> lambda_.Function:
        """
        Create Lambda function for entitlements service operations.

        This function handles:
        - GET /entitlements/status: Get current account status (requires auth)
        - POST /entitlements: Process Apple transactions (requires auth)
        - POST /entitlements/apple-notification: Apple webhook (no auth)

        Returns:
            Lambda Function construct
        """
        lambda_code_path = Path(__file__).parent.parent / "lambda"

        # Determine Apple environment based on deployment environment
        apple_environment = "Sandbox" if self.env_name == "staging" else "Production"

        function = lambda_.Function(
            self,
            "EntitlementsFunction",
            function_name=f"{self.project_name}-{self.env_name}-entitlements",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handlers.entitlements.handler",
            code=lambda_.Code.from_asset(str(lambda_code_path)),
            layers=[self.dependencies_layer],
            memory_size=self.config.LAMBDA_MEMORY_SIZE,
            timeout=self.config.LAMBDA_TIMEOUT,
            environment={
                "ENTITLEMENT_GRANTS_TABLE_NAME": self.entitlement_grants_table.table_name,
                "SUBSCRIPTION_EVENTS_TABLE_NAME": self.subscription_events_table.table_name,
                "USERS_TABLE_NAME": self.users_table_name,
                "ENVIRONMENT": self.config.ENVIRONMENT,
                "SENTRY_DSN": os.environ.get("SENTRY_DSN", ""),
                "LOG_LEVEL": self.config.LOG_LEVEL,
                "APPLE_ENVIRONMENT": apple_environment,
                # SSM parameter names (Lambda will read values at runtime)
                "APPLE_PRIVATE_KEY_PARAM": f"/{self.project_name}/{self.env_name}/entitlements/apple-private-key",
                "APPLE_KEY_ID_PARAM": f"/{self.project_name}/{self.env_name}/entitlements/apple-key-id",
                "APPLE_ISSUER_ID_PARAM": f"/{self.project_name}/{self.env_name}/entitlements/apple-issuer-id",
                "APPLE_BUNDLE_ID_PARAM": f"/{self.project_name}/{self.env_name}/entitlements/apple-bundle-id",
            },
        )

        # Grant read/write permissions to entitlement grants table
        self.entitlement_grants_table.grant_read_write_data(function)

        # Grant write permissions to subscription events table (read not needed from Lambda)
        self.subscription_events_table.grant_write_data(function)

        # Grant read permission to users table (for webhook user validation)
        # Using IAM policy statement to avoid cross-stack reference
        function.add_to_role_policy(
            statement=iam.PolicyStatement(
                actions=["dynamodb:GetItem", "dynamodb:Query"],
                resources=[
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/{self.users_table_name}"
                ]
            )
        )

        # Grant permission to read SSM parameters
        function.add_to_role_policy(
            statement=iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:GetParameters"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter/{self.project_name}/{self.env_name}/entitlements/*"
                ]
            )
        )

        return function

    def _create_api_routes(self) -> None:
        """
        Create API Gateway routes for entitlements service.

        Adds routes to the existing API Gateway from the auth stack:
        - GET /entitlements/status → entitlements_function (requires JWT auth + API key)
        - POST /entitlements → entitlements_function (requires JWT auth + API key)
        - POST /entitlements/apple-notification → entitlements_function (no auth, API key only)
        """
        # Create Lambda integration
        entitlements_integration = apigateway.LambdaIntegration(
            self.entitlements_function,
            proxy=True,
        )

        # Create /entitlements resource
        entitlements_resource = self.api.root.add_resource("entitlements")

        # Add POST method for processing Apple transactions (requires API key + JWT)
        entitlements_resource.add_method(
            "POST",
            entitlements_integration,
            api_key_required=True,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.CUSTOM,
        )

        # Create /entitlements/status resource
        status_resource = entitlements_resource.add_resource("status")

        # Add GET method for status (requires API key + JWT)
        status_resource.add_method(
            "GET",
            entitlements_integration,
            api_key_required=True,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.CUSTOM,
        )

        # Create /entitlements/apple-notification resource
        apple_notification_resource = entitlements_resource.add_resource("apple-notification")

        # Add POST method for Apple webhook (no API key, no JWT auth)
        # Apple sends notifications here and cannot include our API key or JWT.
        # Security: payload is cryptographically verified via Apple's JWS signature.
        apple_notification_resource.add_method(
            "POST",
            entitlements_integration,
            api_key_required=False,
        )
