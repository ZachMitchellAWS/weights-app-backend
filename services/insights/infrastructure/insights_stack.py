"""Insights service CDK stack with Lambda, DynamoDB, EventBridge, and API Gateway integration."""

import os

from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_dynamodb as dynamodb,
    aws_lambda as lambda_,
    aws_apigateway as apigateway,
    aws_ssm as ssm,
    aws_iam as iam,
    aws_events as events,
    aws_events_targets as targets,
    aws_s3 as s3,
)
from constructs import Construct
from pathlib import Path


class InsightsStack(Stack):
    """
    Insights service stack containing:
    - DynamoDB tables for insight tasks and cache
    - Lambda function for GPT-powered training insights
    - Lambda layer for OpenAI dependency
    - EventBridge rule for periodic task processing
    - API Gateway route for weekly insights endpoint

    This stack reads from checkin tables (lift-sets, exercises, estimated-1rm,
    set-plan-templates, accessory-goal-checkins), user-properties,
    and entitlement-grants to analyze training data and generate insights.
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
        accessory_goal_checkins_table: dynamodb.Table,
        estimated_1rm_table: dynamodb.Table,
        set_plan_templates_table: dynamodb.Table,
        user_properties_table: dynamodb.Table,
        entitlement_grants_table: dynamodb.Table,
        groups_table: dynamodb.Table,
        **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.project_name = project_name
        self.env_name = env_name
        self.config = config
        self.api = api
        self.authorizer = authorizer

        # Create resources
        self.insight_tasks_table = self._create_insight_tasks_table()
        self.insights_cache_table = self._create_insights_cache_table()
        self.audio_bucket = self._create_audio_bucket()
        self._create_ssm_parameters()
        self.dependencies_layer = self._create_dependencies_layer()
        self.insights_function = self._create_insights_lambda(
            lift_sets_table,
            exercises_table,
            accessory_goal_checkins_table,
            estimated_1rm_table,
            set_plan_templates_table,
            user_properties_table,
            entitlement_grants_table,
            groups_table,
        )
        self._create_eventbridge_rule()
        self._create_api_routes()

    def _create_insight_tasks_table(self) -> dynamodb.Table:
        """
        Create DynamoDB table for insight generation tasks.

        Tasks track which user+week combinations need insight generation.
        Lifecycle: pending → processing → (deleted on success)

        GSI enables efficient scan of ripe pending tasks by the cron processor.
        """
        table = dynamodb.Table(
            self,
            "InsightTasksTable",
            table_name=f"{self.project_name}-{self.env_name}-insight-tasks",
            partition_key=dynamodb.Attribute(
                name="userId",
                type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="insightWeek",
                type=dynamodb.AttributeType.STRING
            ),
            billing_mode=self.config.DYNAMODB_BILLING_MODE,
            point_in_time_recovery=self.config.DYNAMODB_POINT_IN_TIME_RECOVERY,
            removal_policy=self.config.REMOVAL_POLICY,
            time_to_live_attribute="ttl",
        )

        # GSI for querying ripe pending tasks
        table.add_global_secondary_index(
            index_name="taskStatus-eligibleAfterUtc-index",
            partition_key=dynamodb.Attribute(
                name="taskStatus",
                type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="eligibleAfterUtc",
                type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        return table

    def _create_insights_cache_table(self) -> dynamodb.Table:
        """
        Create DynamoDB table for caching generated insights.

        Stores the GPT-generated narrative sections keyed by user+week.
        """
        table = dynamodb.Table(
            self,
            "InsightsCacheTable",
            table_name=f"{self.project_name}-{self.env_name}-insights-cache",
            partition_key=dynamodb.Attribute(
                name="userId",
                type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="insightWeek",
                type=dynamodb.AttributeType.STRING
            ),
            billing_mode=self.config.DYNAMODB_BILLING_MODE,
            point_in_time_recovery=self.config.DYNAMODB_POINT_IN_TIME_RECOVERY,
            removal_policy=self.config.REMOVAL_POLICY,
            time_to_live_attribute="ttl",
        )

        return table

    def _create_audio_bucket(self) -> s3.Bucket:
        """
        Create S3 bucket for storing TTS audio files of insight narratives.

        Objects auto-expire after 90 days to match the DynamoDB cache TTL.
        """
        bucket = s3.Bucket(
            self,
            "InsightsAudioBucket",
            bucket_name=f"{self.project_name}-{self.env_name}-insights-audio",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=self.config.REMOVAL_POLICY,
            lifecycle_rules=[
                s3.LifecycleRule(
                    expiration=Duration.days(90),
                ),
            ],
        )
        return bucket

    def _create_ssm_parameters(self) -> None:
        """
        Create SSM Parameter Store parameter for OpenAI API key.

        Update with actual key via CLI:
        aws ssm put-parameter --name "/{project}/{env}/insights/openai-api-key" \
            --value "sk-..." --type SecureString --overwrite
        """
        ssm.StringParameter(
            self,
            "OpenAIApiKeyParameter",
            parameter_name=f"/{self.project_name}/{self.env_name}/insights/openai-api-key",
            string_value="PLACEHOLDER-update-with-openai-api-key",
            description=f"OpenAI API key for insights service in {self.env_name}",
            tier=ssm.ParameterTier.STANDARD,
        )

    def _create_dependencies_layer(self) -> lambda_.LayerVersion:
        """
        Create Lambda Layer with Python dependencies (openai).

        Dependencies must be pre-installed in the layer/python directory:
            cd services/insights && pip install -r requirements.txt -t layer/python/
        """
        layer_path = Path(__file__).parent.parent / "layer"

        layer = lambda_.LayerVersion(
            self,
            "DependenciesLayer",
            layer_version_name=f"{self.project_name}-{self.env_name}-insights-deps",
            code=lambda_.Code.from_asset(str(layer_path)),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            description="Python dependencies for insights service (openai)",
        )

        return layer

    def _create_insights_lambda(
        self,
        lift_sets_table: dynamodb.Table,
        exercises_table: dynamodb.Table,
        accessory_goal_checkins_table: dynamodb.Table,
        estimated_1rm_table: dynamodb.Table,
        set_plan_templates_table: dynamodb.Table,
        user_properties_table: dynamodb.Table,
        entitlement_grants_table: dynamodb.Table,
        groups_table: dynamodb.Table,
    ) -> lambda_.Function:
        """Create Lambda function for insights service."""
        lambda_code_path = Path(__file__).parent.parent / "lambda"

        function = lambda_.Function(
            self,
            "InsightsFunction",
            function_name=f"{self.project_name}-{self.env_name}-insights",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handlers.insights.handler",
            code=lambda_.Code.from_asset(str(lambda_code_path)),
            layers=[self.dependencies_layer],
            memory_size=self.config.LAMBDA_MEMORY_SIZE,
            timeout=Duration.seconds(60),
            environment={
                "LIFT_SETS_TABLE_NAME": lift_sets_table.table_name,
                "EXERCISES_TABLE_NAME": exercises_table.table_name,
                "ACCESSORY_GOAL_CHECKINS_TABLE_NAME": accessory_goal_checkins_table.table_name,
                "ESTIMATED_1RM_TABLE_NAME": estimated_1rm_table.table_name,
                "SET_PLAN_TEMPLATES_TABLE_NAME": set_plan_templates_table.table_name,
                "USER_PROPERTIES_TABLE_NAME": user_properties_table.table_name,
                "ENTITLEMENT_GRANTS_TABLE_NAME": entitlement_grants_table.table_name,
                "GROUPS_TABLE_NAME": groups_table.table_name,
                "INSIGHT_TASKS_TABLE_NAME": self.insight_tasks_table.table_name,
                "INSIGHTS_CACHE_TABLE_NAME": self.insights_cache_table.table_name,
                "OPENAI_API_KEY_PARAM": f"/{self.project_name}/{self.env_name}/insights/openai-api-key",
                "OPENAI_MODEL": "gpt-5.4",
                "INSIGHTS_AUDIO_BUCKET": self.audio_bucket.bucket_name,
                "INSIGHTS_FUNCTION_NAME": f"{self.project_name}-{self.env_name}-insights",
                "ENVIRONMENT": self.config.ENVIRONMENT,
                "SENTRY_DSN": os.environ.get("SENTRY_DSN", ""),
                "LOG_LEVEL": self.config.LOG_LEVEL,
            },
            log_retention=self.config.LOG_RETENTION,
        )

        # Grant read access to all input tables
        lift_sets_table.grant_read_data(function)
        exercises_table.grant_read_data(function)
        accessory_goal_checkins_table.grant_read_data(function)
        estimated_1rm_table.grant_read_data(function)
        set_plan_templates_table.grant_read_data(function)
        user_properties_table.grant_read_data(function)
        entitlement_grants_table.grant_read_data(function)
        groups_table.grant_read_data(function)

        # Grant read/write to insights-owned tables
        self.insight_tasks_table.grant_read_write_data(function)
        self.insights_cache_table.grant_read_write_data(function)

        # Grant S3 access for TTS audio files
        self.audio_bucket.grant_read_write(function)

        # Grant self-invoke for async TTS generation
        function.add_to_role_policy(
            statement=iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[
                    f"arn:aws:lambda:{self.region}:{self.account}:function:{self.project_name}-{self.env_name}-insights"
                ]
            )
        )

        # Grant SSM GetParameter permission for OpenAI API key
        function.add_to_role_policy(
            statement=iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter/{self.project_name}/{self.env_name}/insights/*"
                ]
            )
        )

        return function

    def _create_eventbridge_rule(self) -> None:
        """Create EventBridge rule that triggers task processing every 15 minutes."""
        rule = events.Rule(
            self,
            "InsightsProcessTasksRule",
            rule_name=f"{self.project_name}-{self.env_name}-insights-process-tasks",
            schedule=events.Schedule.rate(Duration.minutes(15)),
            description="Trigger insights Lambda to process ripe insight generation tasks",
        )

        rule.add_target(
            targets.LambdaFunction(
                self.insights_function,
                event=events.RuleTargetInput.from_object({
                    "invocationType": "PROCESS_TASKS"
                }),
            )
        )

    def _create_api_routes(self) -> None:
        """
        Create API Gateway routes for insights service.

        Adds routes to the existing API Gateway from the auth stack:
        - GET /insights/weekly → insights_function (requires JWT auth + API key)
        """
        insights_integration = apigateway.LambdaIntegration(
            self.insights_function,
            proxy=True,
        )

        # Create /insights resource
        insights_resource = self.api.root.add_resource("insights")

        # Create /insights/weekly resource
        weekly_resource = insights_resource.add_resource("weekly")

        # Add GET method (requires API key + JWT authentication)
        weekly_resource.add_method(
            "GET",
            insights_integration,
            api_key_required=True,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.CUSTOM,
        )

        # Create /insights/starter resource (no premium check — available to all users)
        starter_resource = insights_resource.add_resource("starter")
        starter_resource.add_method(
            "GET",
            insights_integration,
            api_key_required=True,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.CUSTOM,
        )

        # Create /insights/tier-unlock resource (POST, no premium check)
        tier_unlock_resource = insights_resource.add_resource("tier-unlock")
        tier_unlock_resource.add_method(
            "POST",
            insights_integration,
            api_key_required=True,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.CUSTOM,
        )

        # Create /insights/tier-unlocks resource (GET, no premium check)
        tier_unlocks_resource = insights_resource.add_resource("tier-unlocks")
        tier_unlocks_resource.add_method(
            "GET",
            insights_integration,
            api_key_required=True,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.CUSTOM,
        )
