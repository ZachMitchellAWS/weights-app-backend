"""Notifications service CDK stack — APNs delivery, sharded task queue, fan-out cron."""

from pathlib import Path

from aws_cdk import (
    Stack,
    Duration,
    aws_dynamodb as dynamodb,
    aws_lambda as lambda_,
    aws_apigateway as apigateway,
    aws_events as events,
    aws_events_targets as targets,
    aws_ssm as ssm,
    aws_iam as iam,
)
from constructs import Construct


class NotificationsStack(Stack):
    """
    Notifications service stack containing:
    - `notification-tasks`  — the sharded, time-binned queue
    - `notification-log`    — what was sent (and what was deliberately not sent)
    - Lambda serving four pathways: cron fan-out, shard worker, direct send, dev API route
    - Lambda layer (httpx[http2] for APNs, PyJWT[crypto] for the provider token)
    - EventBridge rule firing every 15 minutes with the fan-out concurrency in its payload
    - SSM parameters for the APNs signing credentials
    - `POST /notifications/test` — STAGING ONLY, see `_create_api_routes`

    The `apns-tokens` table is deliberately NOT here: it lives in the user stack, because the
    user Lambda writes it on registration while this Lambda needs to read user-properties. See
    the note in `app.py`.
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
        user_properties_table: dynamodb.Table,
        apns_tokens_table: dynamodb.Table,
        **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.project_name = project_name
        self.env_name = env_name
        self.config = config
        self.api = api
        self.authorizer = authorizer

        self.tasks_table = self._create_tasks_table()
        self.log_table = self._create_log_table()
        self._create_ssm_parameters()
        self.dependencies_layer = self._create_dependencies_layer()
        self.notifications_function = self._create_notifications_lambda(
            user_properties_table, apns_tokens_table
        )
        self._create_eventbridge_rule()
        self._create_api_routes()

    # ----------------------------------------------------------------- tables
    def _create_tasks_table(self) -> dynamodb.Table:
        """The queue.

        Partitioned on `shard` and sorted on `{dueBinUtc}#{taskId}`. Because the sort key leads
        with a fixed-width UTC timestamp, ripeness is a plain key condition on the main table —
        there is no GSI for the scheduler, and none is needed.

        The shard exists for two reasons at once: it spreads writes that would otherwise all
        land on one bin's partition, and it is the unit of parallelism a worker claims. See
        `lambda/utils/tasks.py` for why there are 100 of them and not 1000.
        """
        table = dynamodb.Table(
            self,
            "NotificationTasksTable",
            table_name=f"{self.project_name}-{self.env_name}-notification-tasks",
            partition_key=dynamodb.Attribute(
                name="shard",
                type=dynamodb.AttributeType.NUMBER
            ),
            sort_key=dynamodb.Attribute(
                name="dueAtTaskId",
                type=dynamodb.AttributeType.STRING
            ),
            billing_mode=self.config.DYNAMODB_BILLING_MODE,
            point_in_time_recovery=self.config.DYNAMODB_POINT_IN_TIME_RECOVERY,
            removal_policy=self.config.REMOVAL_POLICY,
            # Orphan backstop only. Nothing relies on TTL for correctness — a task failing
            # transiently should be visible in the log, not quietly aged out.
            time_to_live_attribute="ttl",
        )

        # Not used by the scheduler. Needed the moment anything enqueues automatically and has
        # to answer "does this user already have one of these pending?" before writing another.
        table.add_global_secondary_index(
            index_name="userId-index",
            partition_key=dynamodb.Attribute(
                name="userId",
                type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        return table

    def _create_log_table(self) -> dynamodb.Table:
        """Every delivery attempt, including the ones that deliberately sent nothing.

        The question this answers is "why did this user not get a notification", so skips are
        rows too — `outcome` distinguishes a cancelled precondition from a dead token from a
        transient failure. Holds only the last 8 characters of a device token, never the token.
        """
        return dynamodb.Table(
            self,
            "NotificationLogTable",
            table_name=f"{self.project_name}-{self.env_name}-notification-log",
            partition_key=dynamodb.Attribute(
                name="userId",
                type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="sentAtTaskId",
                type=dynamodb.AttributeType.STRING
            ),
            billing_mode=self.config.DYNAMODB_BILLING_MODE,
            point_in_time_recovery=self.config.DYNAMODB_POINT_IN_TIME_RECOVERY,
            removal_policy=self.config.REMOVAL_POLICY,
            time_to_live_attribute="ttl",
        )

    # -------------------------------------------------------------------- ssm
    def _create_ssm_parameters(self) -> None:
        """APNs signing credentials, created as placeholders and filled in out of band.

        Same shape as the entitlements service's Apple credentials: the `.p8` contents, the key
        id and the team id, each its own parameter. Populate before the first send:

            aws ssm put-parameter --name "/{project}/{env}/notifications/apns-key" \\
                --value "$(cat AuthKey_XXXX.p8)" --type SecureString --overwrite
        """
        for construct_id, suffix, description in (
            ("ApnsKeyParameter", "apns-key", "APNs .p8 signing key"),
            ("ApnsKeyIdParameter", "apns-key-id", "APNs key ID"),
            ("ApnsTeamIdParameter", "apns-team-id", "Apple team ID"),
        ):
            ssm.StringParameter(
                self,
                construct_id,
                parameter_name=f"/{self.project_name}/{self.env_name}/notifications/{suffix}",
                string_value=f"PLACEHOLDER-update-with-{suffix}",
                description=f"{description} for notifications service in {self.env_name}",
                tier=ssm.ParameterTier.STANDARD,
            )

    # ------------------------------------------------------------------ lambda
    def _create_dependencies_layer(self) -> lambda_.LayerVersion:
        """Layer holding httpx[http2] and PyJWT[crypto].

        Build before the first deploy or the Lambda dies on import — the layer directory is
        gitignored:
            cd services/notifications && pip install -r requirements.txt -t layer/python/
        """
        layer_path = Path(__file__).parent.parent / "layer"
        return lambda_.LayerVersion(
            self,
            "DependenciesLayer",
            layer_version_name=f"{self.project_name}-{self.env_name}-notifications-deps",
            code=lambda_.Code.from_asset(str(layer_path)),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_13],
            description="Python dependencies for notifications service (httpx[http2], PyJWT)",
        )

    def _create_notifications_lambda(
        self,
        user_properties_table: dynamodb.Table,
        apns_tokens_table: dynamodb.Table,
    ) -> lambda_.Function:
        lambda_code_path = Path(__file__).parent.parent / "lambda"
        function_name = f"{self.project_name}-{self.env_name}-notifications"

        function = lambda_.Function(
            self,
            "NotificationsFunction",
            function_name=function_name,
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="handlers.notifications.handler",
            code=lambda_.Code.from_asset(str(lambda_code_path)),
            layers=[self.dependencies_layer],
            memory_size=self.config.LAMBDA_MEMORY_SIZE,
            # Generous because a shard worker sweeps up to 100 shards and the direct pathway
            # may sleep before sending. Not API-bound: the only HTTP route returns immediately
            # after an async self-invoke.
            timeout=Duration.seconds(300),
            environment={
                "ENVIRONMENT": self.env_name,
                "NOTIFICATION_TASKS_TABLE_NAME": self.tasks_table.table_name,
                "NOTIFICATION_LOG_TABLE_NAME": self.log_table.table_name,
                "APNS_TOKENS_TABLE_NAME": apns_tokens_table.table_name,
                "USER_PROPERTIES_TABLE_NAME": user_properties_table.table_name,
                # Staging and production share one bundle id: APP_BUNDLE_ID_SUFFIX is empty in
                # both xcconfigs, so this is the APNs topic for both environments.
                "APNS_TOPIC": "io.anthroverse.WeightApp",
                "APNS_KEY_PARAM": f"/{self.project_name}/{self.env_name}/notifications/apns-key",
                "APNS_KEY_ID_PARAM": f"/{self.project_name}/{self.env_name}/notifications/apns-key-id",
                "APNS_TEAM_ID_PARAM": f"/{self.project_name}/{self.env_name}/notifications/apns-team-id",
                "SELF_FUNCTION_NAME": function_name,
                "LOG_LEVEL": self.config.LOG_LEVEL,
            },
        )

        self.tasks_table.grant_read_write_data(function)
        self.log_table.grant_read_write_data(function)
        # Read/write: this service stamps lastDeliveredUtc and retires tokens Apple rejects.
        apns_tokens_table.grant_read_write_data(function)
        # Read only: the precondition needs hasMetStrengthTierConditions, and the transitional
        # fallback needs apnsDeviceToken. This service never writes user properties.
        user_properties_table.grant_read_data(function)

        # Self-invoke, for both the fan-out and the delayed direct send.
        function.add_to_role_policy(
            statement=iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[
                    f"arn:aws:lambda:{self.region}:{self.account}:function:{function_name}"
                ]
            )
        )

        function.add_to_role_policy(
            statement=iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter"
                    f"/{self.project_name}/{self.env_name}/notifications/*"
                ]
            )
        )

        return function

    # ------------------------------------------------------------------- cron
    def _create_eventbridge_rule(self) -> None:
        """Fire the fan-out every 15 minutes, matching the task bin width.

        The concurrency lives in the rule's PAYLOAD, not in the Lambda — that is what makes it
        a dial. Raising `NOTIFICATION_FANOUT_CONCURRENCY` in `config/` and redeploying splits
        the same shard space across more workers with no code change.
        """
        rule = events.Rule(
            self,
            "NotificationScheduleFanoutRule",
            rule_name=f"{self.project_name}-{self.env_name}-notification-fanout",
            schedule=events.Schedule.rate(Duration.minutes(15)),
            description="Fan out notification task processing across shard ranges",
        )
        rule.add_target(
            targets.LambdaFunction(
                self.notifications_function,
                event=events.RuleTargetInput.from_object({
                    "invocationType": "SCHEDULE_FANOUT",
                    "concurrency": self.config.NOTIFICATION_FANOUT_CONCURRENCY,
                }),
            )
        )

    # -------------------------------------------------------------------- api
    def _create_api_routes(self) -> None:
        """`POST /notifications/test` — STAGING ONLY.

        The route is not created at all outside staging, so the direct-send pathway cannot be
        reached in production even with a valid JWT and API key. This is deliberately stronger
        than hiding the button client-side: a test endpoint that ships to production is a live
        push channel with no rate limit in front of it.

        The handler takes `userId` from the JWT claims and never from the body, so it can only
        ever push to the caller's own devices.
        """
        if self.env_name != "staging":
            return

        integration = apigateway.LambdaIntegration(self.notifications_function)
        notifications_resource = self.api.root.add_resource("notifications")
        test_resource = notifications_resource.add_resource("test")
        test_resource.add_method(
            "POST",
            integration,
            api_key_required=True,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.CUSTOM,
        )
