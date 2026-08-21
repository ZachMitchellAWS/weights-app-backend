"""Sessions service CDK stack — Lambda + API Gateway route for generated sessions."""

import os

from aws_cdk import (
    Stack,
    Duration,
    aws_dynamodb as dynamodb,
    aws_lambda as lambda_,
    aws_apigateway as apigateway,
    aws_iam as iam,
)
from constructs import Construct
from pathlib import Path


class SessionsStack(Stack):
    """
    Sessions service stack containing:
    - Lambda function that assembles a training payload and generates today's session
    - Lambda layer for the OpenAI dependency
    - API Gateway route for POST /sessions/generate

    It reads from checkin tables (lift-sets, exercises, estimated-1rm), user-properties, and
    entitlement-grants, and owns one table of its own: `generated-sessions`, a record of what
    was asked for and what came back.

    The generated session itself is still returned to the client and never read back — the
    record exists for moderation review and analysis, not to serve requests.

    It also creates no SSM parameter of its own: it reads the insights service's OpenAI key,
    so there is only one key to populate per environment.
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
        lift_sets_table: dynamodb.Table,
        exercises_table: dynamodb.Table,
        estimated_1rm_table: dynamodb.Table,
        user_properties_table: dynamodb.Table,
        entitlement_grants_table: dynamodb.Table,
        **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.project_name = project_name
        self.env_name = env_name
        self.config = config
        self.api = api
        self.authorizer = authorizer

        self.generated_sessions_table = self._create_generated_sessions_table()
        self.dependencies_layer = self._create_dependencies_layer()
        self.sessions_function = self._create_sessions_lambda(
            lift_sets_table,
            exercises_table,
            estimated_1rm_table,
            user_properties_table,
            entitlement_grants_table,
        )
        self._create_api_routes()

    def _create_generated_sessions_table(self) -> dynamodb.Table:
        """One item per generation request, plus one aggregate item per user.

        Two record shapes share the table, separated by sort key:
          * `sessionId` = uuid  — a request: the chips and note that went in, whether the
            note survived moderation, the model's response, and the outcome.
          * `sessionId` = "#violations" — the per-user abuse counter.

        The counter lives here rather than on user-properties because that table's GET
        returns the whole item verbatim to the client, and this count must never leave the
        backend.

        NO TTL. Retained indefinitely by product decision, which makes this the only table
        holding user-written free text forever — including text moderation rejected. It must
        therefore be covered by `scripts/delete_user.py`, or account deletion is incomplete.
        """
        return dynamodb.Table(
            self,
            "GeneratedSessionsTable",
            table_name=f"{self.project_name}-{self.env_name}-generated-sessions",
            partition_key=dynamodb.Attribute(name="userId", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sessionId", type=dynamodb.AttributeType.STRING),
            billing_mode=self.config.DYNAMODB_BILLING_MODE,
            point_in_time_recovery=self.config.DYNAMODB_POINT_IN_TIME_RECOVERY,
            removal_policy=self.config.REMOVAL_POLICY,
        )

    def _create_dependencies_layer(self) -> lambda_.LayerVersion:
        """
        Create Lambda Layer with Python dependencies (openai, tzdata).

        Dependencies must be pre-installed in the layer/python directory:
            cd services/sessions && pip install -r requirements.txt -t layer/python/
        """
        layer_path = Path(__file__).parent.parent / "layer"

        return lambda_.LayerVersion(
            self,
            "DependenciesLayer",
            layer_version_name=f"{self.project_name}-{self.env_name}-sessions-deps",
            code=lambda_.Code.from_asset(str(layer_path)),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_13],
            description="Python dependencies for sessions service (openai, tzdata)",
        )

    def _create_sessions_lambda(
        self,
        lift_sets_table: dynamodb.Table,
        exercises_table: dynamodb.Table,
        estimated_1rm_table: dynamodb.Table,
        user_properties_table: dynamodb.Table,
        entitlement_grants_table: dynamodb.Table,
    ) -> lambda_.Function:
        """Create Lambda function for sessions service."""
        lambda_code_path = Path(__file__).parent.parent / "lambda"

        function = lambda_.Function(
            self,
            "SessionsFunction",
            function_name=f"{self.project_name}-{self.env_name}-sessions",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="handlers.sessions.handler",
            code=lambda_.Code.from_asset(str(lambda_code_path)),
            layers=[self.dependencies_layer],
            memory_size=self.config.LAMBDA_MEMORY_SIZE,
            # Synchronous behind API Gateway's fixed 29s integration timeout. 28s keeps the
            # failure ours: the Lambda returns a structured error instead of the gateway
            # returning a bare 504.
            #
            # The model call is bounded by a SIGALRM deadline derived from THIS number at
            # runtime (remaining time minus a 4s reserve), not by a second hardcoded
            # constant — so changing 28 here moves the model's budget with it. Do not
            # assume the OpenAI client's own timeout bounds anything: it is per-phase, and
            # a trickling response ran 28s under a nominal 20s setting.
            timeout=Duration.seconds(28),
            environment={
                "LIFT_SETS_TABLE_NAME": lift_sets_table.table_name,
                "EXERCISES_TABLE_NAME": exercises_table.table_name,
                "ESTIMATED_1RM_TABLE_NAME": estimated_1rm_table.table_name,
                "USER_PROPERTIES_TABLE_NAME": user_properties_table.table_name,
                "ENTITLEMENT_GRANTS_TABLE_NAME": entitlement_grants_table.table_name,
                # Shared with insights on purpose — one key to rotate, one placeholder to fill.
                "OPENAI_API_KEY_PARAM": f"/{self.project_name}/{self.env_name}/insights/openai-api-key",
                "OPENAI_MODEL": "gpt-5.4",
                # Alias by default. Pin to a dated snapshot here once one is confirmed —
                # the alias can shift its false-positive rate with no deploy, and since
                # moderation fails closed, drift means silently dropping real notes.
                "OPENAI_MODERATION_MODEL": "omni-moderation-latest",
                "GENERATED_SESSIONS_TABLE_NAME": self.generated_sessions_table.table_name,
                "ENVIRONMENT": self.config.ENVIRONMENT,
                "SENTRY_DSN": os.environ.get("SENTRY_DSN", ""),
                "LOG_LEVEL": self.config.LOG_LEVEL,
            },
        )

        # Read-only on everything it did not create. The service still never modifies a
        # user's training data, their properties, or their entitlements.
        lift_sets_table.grant_read_data(function)
        exercises_table.grant_read_data(function)
        estimated_1rm_table.grant_read_data(function)
        user_properties_table.grant_read_data(function)
        entitlement_grants_table.grant_read_data(function)

        # Its own table: writes only. Scoped to the two calls it actually makes rather than
        # `grant_read_write_data`, which would hand over DeleteItem and BatchWriteItem that
        # nothing here uses. Add Query later if a read path is ever built.
        function.add_to_role_policy(
            statement=iam.PolicyStatement(
                actions=["dynamodb:PutItem", "dynamodb:UpdateItem"],
                resources=[self.generated_sessions_table.table_arn],
            )
        )

        # Read the insights service's OpenAI key.
        function.add_to_role_policy(
            statement=iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter/{self.project_name}/{self.env_name}/insights/*"
                ]
            )
        )

        return function

    def _create_api_routes(self) -> None:
        """
        Create API Gateway routes for sessions service.

        Adds routes to the existing API Gateway from the auth stack:
        - POST /sessions/generate → sessions_function (requires JWT auth + API key)
        """
        sessions_integration = apigateway.LambdaIntegration(
            self.sessions_function,
            proxy=True,
        )

        sessions_resource = self.api.root.add_resource("sessions")
        generate_resource = sessions_resource.add_resource("generate")

        generate_resource.add_method(
            "POST",
            sessions_integration,
            api_key_required=True,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.CUSTOM,
        )
