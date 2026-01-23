"""Auth service CDK stack with DynamoDB, Lambda, and API Gateway."""

from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    aws_dynamodb as dynamodb,
    aws_lambda as lambda_,
    aws_apigateway as apigateway,
    aws_ssm as ssm,
    aws_iam as iam,
)
from constructs import Construct
from pathlib import Path


class AuthStack(Stack):
    """
    Auth service stack containing:
    - DynamoDB table for user storage with userId as primary key
    - Single Lambda function handling multiple auth endpoints
    - API Gateway REST API with CORS enabled

    This stack demonstrates CDK best practices:
    - Using L2 constructs for simplified resource creation
    - Environment-driven configuration
    - Proper IAM permission grants
    - Centralized logging configuration
    - Resource naming conventions
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        project_name: str,
        env_name: str,
        config: any,
        **kwargs
    ) -> None:
        """
        Initialize the Auth service stack.

        Args:
            scope: CDK scope
            construct_id: Unique identifier for this stack
            project_name: Project name for resource naming
            env_name: Environment name (staging/production)
            config: Configuration module (staging.py or production.py)
            **kwargs: Additional stack arguments
        """
        super().__init__(scope, construct_id, **kwargs)

        # Store configuration for use in resource creation
        self.project_name = project_name
        self.env_name = env_name
        self.config = config

        # Create resources
        self.users_table = self._create_dynamodb_table()
        self.password_reset_codes_table = self._create_password_reset_codes_table()
        self._create_ssm_parameters()
        self.dependencies_layer = self._create_dependencies_layer()
        self.auth_function = self._create_auth_lambda()
        self.api = self._create_api_gateway()

    def _create_dynamodb_table(self) -> dynamodb.Table:
        """
        Create DynamoDB table for user storage.

        Design decisions:
        - userId as partition key: Unique identifier for each user
        - emailAddress GSI: Allows lookups by email for login
        - PAY_PER_REQUEST billing: Cost-effective for variable workloads
        - Point-in-time recovery: Enabled in production for data protection
        - Removal policy: Environment-specific (DESTROY for staging, RETAIN for production)

        Returns:
            DynamoDB Table construct
        """
        table = dynamodb.Table(
            self,
            "UsersTable",
            table_name=f"{self.project_name}-{self.env_name}-users",
            partition_key=dynamodb.Attribute(
                name="userId",
                type=dynamodb.AttributeType.STRING
            ),
            billing_mode=self.config.DYNAMODB_BILLING_MODE,
            point_in_time_recovery=self.config.DYNAMODB_POINT_IN_TIME_RECOVERY,
            removal_policy=self.config.REMOVAL_POLICY,
        )

        # Add Global Secondary Index on emailAddress for login lookups
        # This allows querying users by email without scanning the table
        table.add_global_secondary_index(
            index_name="emailAddress-index",
            partition_key=dynamodb.Attribute(
                name="emailAddress",
                type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,  # Include all attributes in the index
        )

        return table

    def _create_password_reset_codes_table(self) -> dynamodb.Table:
        """
        Create DynamoDB table for password reset codes.

        Design decisions:
        - userId as partition key: One active reset code per user
        - TTL enabled: Auto-delete expired codes after 1 hour
        - PAY_PER_REQUEST billing: Low, sporadic traffic pattern
        - No GSI needed: Only lookup by userId

        Table schema:
        - userId (String, partition key): User's unique identifier
        - code (String): 6-digit reset code
        - createdDatetime (String): ISO 8601 timestamp
        - expiryTime (Number): Unix timestamp for TTL (auto-delete)
        - resetAttempts (Number): Rate limiting counter

        Returns:
            DynamoDB Table construct
        """
        table = dynamodb.Table(
            self,
            "PasswordResetCodesTable",
            table_name=f"{self.project_name}-{self.env_name}-password-reset-codes",
            partition_key=dynamodb.Attribute(
                name="userId",
                type=dynamodb.AttributeType.STRING
            ),
            billing_mode=self.config.DYNAMODB_BILLING_MODE,
            time_to_live_attribute="expiryTime",  # Auto-delete after expiry
            removal_policy=self.config.REMOVAL_POLICY,
        )

        return table

    def _create_ssm_parameters(self) -> None:
        """
        Create SSM Parameter Store parameters for secrets.

        Creates SecureString parameters for:
        - JWT_SECRET_KEY: Used for signing and verifying JWT tokens
        - PASSWORD_PEPPER: Used as additional secret for password hashing

        Parameters are created with placeholder values. You should update them
        with actual secret values using AWS Console or CLI:

        aws ssm put-parameter --name "/{project}/{env}/auth/jwt-secret-key" \\
            --value "your-actual-secret-here" --type SecureString --overwrite

        aws ssm put-parameter --name "/{project}/{env}/auth/password-pepper" \\
            --value "your-actual-pepper-here" --type SecureString --overwrite

        The Lambda function will read these at runtime using IAM permissions.
        """
        # Create JWT secret key parameter
        ssm.StringParameter(
            self,
            "JWTSecretParameter",
            parameter_name=f"/{self.project_name}/{self.env_name}/auth/jwt-secret-key",
            string_value="PLACEHOLDER-change-this-value-in-ssm",
            description=f"JWT secret key for {self.env_name} environment",
            tier=ssm.ParameterTier.STANDARD,
        )

        # Create password pepper parameter
        ssm.StringParameter(
            self,
            "PasswordPepperParameter",
            parameter_name=f"/{self.project_name}/{self.env_name}/auth/password-pepper",
            string_value="PLACEHOLDER-change-this-value-in-ssm",
            description=f"Password pepper for {self.env_name} environment",
            tier=ssm.ParameterTier.STANDARD,
        )

    def _create_dependencies_layer(self) -> lambda_.LayerVersion:
        """
        Create Lambda Layer with Python dependencies.

        This layer includes PyJWT and other dependencies from requirements.txt.
        Dependencies must be pre-installed in the layer/python directory:
            cd services/auth && pip install -r requirements.txt -t layer/python/

        Returns:
            Lambda LayerVersion construct
        """
        # Path to layer directory with pre-installed dependencies
        layer_path = Path(__file__).parent.parent / "layer"

        layer = lambda_.LayerVersion(
            self,
            "DependenciesLayer",
            layer_version_name=f"{self.project_name}-{self.env_name}-auth-deps",
            code=lambda_.Code.from_asset(str(layer_path)),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            description="Python dependencies for auth service (PyJWT, boto3, pydantic)",
        )

        return layer

    def _create_auth_lambda(self) -> lambda_.Function:
        """
        Create single Lambda function for all auth endpoints AND Lambda Authorizer.

        This function handles two types of requests:

        **API Endpoints** (when path exists in event):
        - POST /create-user: Create new user with emailAddress and password
        - POST /login: Authenticate user and return JWT credentials
        - POST /refresh: Refresh access token using refresh token
        - POST /logout: Remove refresh token (requires authentication)

        **Lambda Authorizer** (when authorizationToken exists in event):
        - Validates JWT access tokens for protected endpoints
        - Returns IAM policy (Allow/Deny)

        The function uses internal routing to detect event type and handle appropriately.
        It has read/write access to the DynamoDB table.

        Returns:
            Lambda Function construct
        """
        # Path to Lambda code (services/auth/lambda directory)
        lambda_code_path = Path(__file__).parent.parent / "lambda"

        function = lambda_.Function(
            self,
            "AuthFunction",
            function_name=f"{self.project_name}-{self.env_name}-auth",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handlers.auth.handler",
            code=lambda_.Code.from_asset(str(lambda_code_path)),
            layers=[self.dependencies_layer],  # Add dependencies layer
            memory_size=self.config.LAMBDA_MEMORY_SIZE,
            timeout=self.config.LAMBDA_TIMEOUT,
            environment={
                "USERS_TABLE_NAME": self.users_table.table_name,
                "EMAIL_INDEX_NAME": "emailAddress-index",
                "PASSWORD_RESET_CODES_TABLE_NAME": self.password_reset_codes_table.table_name,
                "ENVIRONMENT": self.config.ENVIRONMENT,
                "LOG_LEVEL": self.config.LOG_LEVEL,
                # SSM parameter names (Lambda will read values at runtime)
                "JWT_SECRET_KEY_PARAM": f"/{self.project_name}/{self.env_name}/auth/jwt-secret-key",
                "PASSWORD_PEPPER_PARAM": f"/{self.project_name}/{self.env_name}/auth/password-pepper",
                # Email Lambda ARN will be added after email stack is created
            },
            log_retention=self.config.LOG_RETENTION,
        )

        # Grant read/write permissions to DynamoDB tables
        # The function needs to write new users and query by email
        self.users_table.grant_read_write_data(function)

        # Grant read/write permissions to password reset codes table
        # The function needs to create, read, and delete reset codes
        self.password_reset_codes_table.grant_read_write_data(function)

        # Grant permission to read SSM parameters
        # The function needs to read JWT secret key and password pepper
        function.add_to_role_policy(
            statement=iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:GetParameters"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter/{self.project_name}/{self.env_name}/auth/*"
                ]
            )
        )

        return function

    def _create_api_gateway(self) -> apigateway.RestApi:
        """
        Create API Gateway REST API with Lambda integrations.

        Architecture decisions:
        - Single Lambda function with internal routing: Simpler deployment and shared logic
        - Lambda proxy integration: Simplifies request/response handling
        - CORS enabled: Allows frontend apps from any origin (restrict in production)
        - Logging enabled: CloudWatch logs for API access and errors

        API Structure:
        - POST /auth/create-user → auth_function (no auth)
        - POST /auth/login → auth_function (no auth)
        - POST /auth/refresh → auth_function (no auth)
        - POST /auth/logout → auth_function (requires JWT auth via Lambda Authorizer)

        Returns:
            API Gateway RestApi construct
        """
        # Create REST API
        api = apigateway.RestApi(
            self,
            "Api",
            rest_api_name=f"{self.project_name}-{self.env_name}-api",
            description=f"Main API for {self.env_name} environment (auth, user, and other services)",
            deploy_options=apigateway.StageOptions(
                stage_name=self.env_name,
                logging_level=apigateway.MethodLoggingLevel.INFO,
                data_trace_enabled=True,
                metrics_enabled=True,
            ),
            # CORS configuration - allows all origins
            # TODO: In production, restrict to specific frontend domains
            default_cors_preflight_options=apigateway.CorsOptions(
                allow_origins=apigateway.Cors.ALL_ORIGINS,
                allow_methods=apigateway.Cors.ALL_METHODS,
                allow_headers=[
                    "Content-Type",
                    "X-Amz-Date",
                    "Authorization",
                    "X-Api-Key",
                    "X-Amz-Security-Token",
                ],
            ),
        )

        # Create Lambda integration (reused for all endpoints)
        auth_integration = apigateway.LambdaIntegration(
            self.auth_function,
            proxy=True,  # Lambda proxy integration passes full request to handler
        )

        # Create Lambda Authorizer for protected endpoints
        # Uses the same Lambda function, which detects authorizer requests by event structure
        token_authorizer = apigateway.TokenAuthorizer(
            self,
            "JWTAuthorizer",
            handler=self.auth_function,  # Same function handles both API and authorizer requests
            identity_source="method.request.header.Authorization",
            authorizer_name=f"{self.project_name}-{self.env_name}-jwt-authorizer",
            results_cache_ttl=Duration.seconds(0),  # Disable caching for development (enable in production)
        )

        # Create API Key for this environment
        # This provides an additional layer of security and usage tracking
        api_key = api.add_api_key(
            f"{self.project_name}-{self.env_name}-api-key",
            api_key_name=f"{self.project_name}-{self.env_name}-api-key",
            description=f"API key for {self.env_name} environment",
        )

        # Create usage plan (no restrictions)
        # This associates the API key with the API stage
        usage_plan = api.add_usage_plan(
            f"{self.project_name}-{self.env_name}-usage-plan",
            name=f"{self.project_name}-{self.env_name}-usage-plan",
            description=f"Usage plan for {self.env_name} environment (no restrictions)",
            throttle=None,  # No throttling
            quota=None,     # No quota limits
        )

        # Add API stage to usage plan
        usage_plan.add_api_stage(
            stage=api.deployment_stage,
        )

        # Associate API key with usage plan
        usage_plan.add_api_key(api_key)

        # Store API key for outputs
        self.api_key = api_key

        # Create /auth resource to group all auth endpoints
        auth_resource = api.root.add_resource("auth")

        # Create /auth/create-user endpoint (requires API key)
        create_user_resource = auth_resource.add_resource("create-user")
        create_user_resource.add_method(
            "POST",
            auth_integration,
            api_key_required=True,
        )

        # Create /auth/login endpoint (requires API key)
        login_resource = auth_resource.add_resource("login")
        login_resource.add_method(
            "POST",
            auth_integration,
            api_key_required=True,
        )

        # Create /auth/refresh endpoint (requires API key)
        refresh_resource = auth_resource.add_resource("refresh")
        refresh_resource.add_method(
            "POST",
            auth_integration,
            api_key_required=True,
        )

        # Create /auth/logout endpoint (requires API key + JWT auth)
        logout_resource = auth_resource.add_resource("logout")
        logout_resource.add_method(
            "POST",
            auth_integration,
            api_key_required=True,
            authorizer=token_authorizer,  # Require JWT token validation
            authorization_type=apigateway.AuthorizationType.CUSTOM,
        )

        # Create /auth/initiate-password-reset endpoint (requires API key)
        initiate_reset_resource = auth_resource.add_resource("initiate-password-reset")
        initiate_reset_resource.add_method(
            "POST",
            auth_integration,
            api_key_required=True,
        )

        # Create /auth/confirm-password-reset endpoint (requires API key)
        confirm_reset_resource = auth_resource.add_resource("confirm-password-reset")
        confirm_reset_resource.add_method(
            "POST",
            auth_integration,
            api_key_required=True,
        )

        # Store authorizer as instance variable for use by other stacks
        self.authorizer = token_authorizer

        # Output API key value for use in Postman/clients
        CfnOutput(
            self,
            "ApiKeyValue",
            value=api_key.key_id,
            description=f"API Key ID for {self.env_name} environment (use with x-api-key header)",
        )

        return api
