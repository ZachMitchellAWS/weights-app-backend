"""Checkin service CDK stack with DynamoDB, Lambda, and API Gateway integration."""

from aws_cdk import (
    Stack,
    aws_dynamodb as dynamodb,
    aws_lambda as lambda_,
    aws_apigateway as apigateway,
)
from constructs import Construct
from pathlib import Path


class CheckinStack(Stack):
    """
    Checkin service stack containing:
    - DynamoDB table for exercise check-ins
    - Lambda function for exercise operations
    - API Gateway routes for checkin endpoints (integrated into existing API)

    This stack creates an exercises table and Lambda function that handles
    exercise check-in operations like creating, retrieving, and soft-deleting exercises.
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
        Initialize the CheckinStack.

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
        self.exercises_table = self._create_exercises_table()
        self.lift_sets_table = self._create_lift_sets_table()
        self.estimated_1rm_table = self._create_estimated_1rm_table()
        self.checkin_function = self._create_checkin_lambda()
        self._create_api_routes()

    def _create_exercises_table(self) -> dynamodb.Table:
        """
        Create DynamoDB table for exercise check-ins.

        Design decisions:
        - userId as partition key: Links exercises to user
        - exerciseItemId as sort key: Unique identifier for each exercise entry
        - PAY_PER_REQUEST billing: Cost-effective for variable workloads
        - Point-in-time recovery: Environment-specific (enabled in production)
        - Removal policy: Environment-specific (DESTROY in staging, RETAIN in production)

        Table schema:
        - userId (String, partition key): User's unique identifier
        - exerciseItemId (String, sort key): Unique exercise item ID (UUID from frontend)
        - name (String): Exercise name
        - isCustom (Boolean): Whether exercise is custom or predefined
        - loadType (String): Type of load (Barbell, Bodyweight + Single Load, Single Load)
        - notes (String): Optional notes about the exercise
        - createdTimezone (String): Timezone when exercise was created
        - createdDatetime (String): ISO 8601 timestamp when created
        - lastModifiedDatetime (String): ISO 8601 timestamp when last modified
        - deleted (Boolean): Soft delete flag (only present when True)

        Returns:
            DynamoDB Table construct
        """
        table = dynamodb.Table(
            self,
            "ExercisesTable",
            table_name=f"{self.project_name}-{self.env_name}-exercises",
            partition_key=dynamodb.Attribute(
                name="userId",
                type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="exerciseItemId",
                type=dynamodb.AttributeType.STRING
            ),
            billing_mode=self.config.DYNAMODB_BILLING_MODE,
            point_in_time_recovery=self.config.DYNAMODB_POINT_IN_TIME_RECOVERY,
            removal_policy=self.config.REMOVAL_POLICY,
        )

        return table

    def _create_lift_sets_table(self) -> dynamodb.Table:
        """
        Create DynamoDB table for lift sets.

        Design decisions:
        - userId as partition key: Links lift sets to user
        - liftSetId as sort key: Unique identifier for each lift set (UUID from frontend)
        - GSI on createdDatetime: Enables efficient "most recent first" pagination
        - PAY_PER_REQUEST billing: Cost-effective for variable workloads
        - Point-in-time recovery: Environment-specific (enabled in production)
        - Removal policy: Environment-specific (DESTROY in staging, RETAIN in production)

        Table schema:
        - userId (String, partition key): User's unique identifier
        - liftSetId (String, sort key): Unique lift set ID (UUID from frontend)
        - exerciseId (String): Links to exercise
        - reps (Number): Number of repetitions
        - weight (Number): Weight used (decimal)
        - createdTimezone (String): Timezone when lift set was created
        - createdDatetime (String): ISO 8601 timestamp when created
        - lastModifiedDatetime (String): ISO 8601 timestamp when last modified
        - deleted (Boolean): Soft delete flag

        GSI (userId-createdDatetime-index):
        - Partition key: userId
        - Sort key: createdDatetime
        - Enables querying with ScanIndexForward=False for most recent first

        Returns:
            DynamoDB Table construct
        """
        table = dynamodb.Table(
            self,
            "LiftSetsTable",
            table_name=f"{self.project_name}-{self.env_name}-lift-sets",
            partition_key=dynamodb.Attribute(
                name="userId",
                type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="liftSetId",
                type=dynamodb.AttributeType.STRING
            ),
            billing_mode=self.config.DYNAMODB_BILLING_MODE,
            point_in_time_recovery=self.config.DYNAMODB_POINT_IN_TIME_RECOVERY,
            removal_policy=self.config.REMOVAL_POLICY,
        )

        # Add GSI for efficient "most recent first" pagination
        table.add_global_secondary_index(
            index_name="userId-createdDatetime-index",
            partition_key=dynamodb.Attribute(
                name="userId",
                type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="createdDatetime",
                type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        return table

    def _create_estimated_1rm_table(self) -> dynamodb.Table:
        """
        Create DynamoDB table for estimated one rep max records.

        Design decisions:
        - userId as partition key: Links E1RM records to user
        - liftSetId as sort key: Links to the lift set that achieved this E1RM
        - GSI on createdDatetime: Enables efficient "most recent first" pagination
        - PAY_PER_REQUEST billing: Cost-effective for variable workloads
        - Point-in-time recovery: Environment-specific (enabled in production)
        - Removal policy: Environment-specific (DESTROY in staging, RETAIN in production)

        Table schema:
        - userId (String, partition key): User's unique identifier
        - liftSetId (String, sort key): ID of the lift set that achieved this E1RM
        - estimated1RMId (String): Unique ID for this E1RM record (UUID from frontend)
        - exerciseId (String): Links to exercise
        - value (Number): The estimated one rep max value (decimal)
        - createdTimezone (String): Timezone when E1RM was created
        - createdDatetime (String): ISO 8601 timestamp when created
        - lastModifiedDatetime (String): ISO 8601 timestamp when last modified
        - deleted (Boolean): Soft delete flag

        GSI (userId-createdDatetime-index):
        - Partition key: userId
        - Sort key: createdDatetime
        - Enables querying with ScanIndexForward=False for most recent first

        Returns:
            DynamoDB Table construct
        """
        table = dynamodb.Table(
            self,
            "Estimated1RMTable",
            table_name=f"{self.project_name}-{self.env_name}-estimated-1rm",
            partition_key=dynamodb.Attribute(
                name="userId",
                type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="liftSetId",
                type=dynamodb.AttributeType.STRING
            ),
            billing_mode=self.config.DYNAMODB_BILLING_MODE,
            point_in_time_recovery=self.config.DYNAMODB_POINT_IN_TIME_RECOVERY,
            removal_policy=self.config.REMOVAL_POLICY,
        )

        # Add GSI for efficient "most recent first" pagination
        table.add_global_secondary_index(
            index_name="userId-createdDatetime-index",
            partition_key=dynamodb.Attribute(
                name="userId",
                type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="createdDatetime",
                type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        return table

    def _create_checkin_lambda(self) -> lambda_.Function:
        """
        Create Lambda function for checkin service operations.

        This function handles:
        - POST /checkin/exercises: Create new exercise check-ins (batch support)
        - GET /checkin/exercises: Retrieve all non-deleted exercises for user
        - DELETE /checkin/exercises: Soft delete exercises (batch support)

        All endpoints require authentication via Lambda Authorizer.
        The user ID is extracted from the authorizer context.

        Returns:
            Lambda Function construct
        """
        # Path to Lambda code (services/checkin/lambda directory)
        lambda_code_path = Path(__file__).parent.parent / "lambda"

        function = lambda_.Function(
            self,
            "CheckinFunction",
            function_name=f"{self.project_name}-{self.env_name}-checkin",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handlers.checkin.handler",
            code=lambda_.Code.from_asset(str(lambda_code_path)),
            memory_size=self.config.LAMBDA_MEMORY_SIZE,
            timeout=self.config.LAMBDA_TIMEOUT,
            environment={
                "EXERCISES_TABLE_NAME": self.exercises_table.table_name,
                "LIFT_SETS_TABLE_NAME": self.lift_sets_table.table_name,
                "ESTIMATED_1RM_TABLE_NAME": self.estimated_1rm_table.table_name,
                "ENVIRONMENT": self.config.ENVIRONMENT,
                "LOG_LEVEL": self.config.LOG_LEVEL,
            },
            log_retention=self.config.LOG_RETENTION,
        )

        # Grant read/write permissions to DynamoDB tables
        self.exercises_table.grant_read_write_data(function)
        self.lift_sets_table.grant_read_write_data(function)
        self.estimated_1rm_table.grant_read_write_data(function)

        return function

    def _create_api_routes(self) -> None:
        """
        Create API Gateway routes for checkin service.

        Adds routes to the existing API Gateway from the auth stack:
        - POST /checkin/exercises → checkin_function (requires JWT auth + API key) - batch create
        - GET /checkin/exercises → checkin_function (requires JWT auth + API key)
        - DELETE /checkin/exercises → checkin_function (requires JWT auth + API key) - batch delete

        All endpoints use the Lambda Authorizer to validate JWT tokens
        and extract the user ID from the token.
        """
        # Create Lambda integration
        checkin_integration = apigateway.LambdaIntegration(
            self.checkin_function,
            proxy=True,  # Lambda proxy integration passes full request to handler
        )

        # Create /checkin resource
        checkin_resource = self.api.root.add_resource("checkin")

        # Create /checkin/exercises resource (all exercise operations use plural)
        exercises_resource = checkin_resource.add_resource("exercises")

        # Add POST method (requires API key + JWT authentication) - batch create
        exercises_resource.add_method(
            "POST",
            checkin_integration,
            api_key_required=True,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.CUSTOM,
        )

        # Add GET method (requires API key + JWT authentication)
        exercises_resource.add_method(
            "GET",
            checkin_integration,
            api_key_required=True,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.CUSTOM,
        )

        # Add DELETE method (requires API key + JWT authentication) - batch delete
        exercises_resource.add_method(
            "DELETE",
            checkin_integration,
            api_key_required=True,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.CUSTOM,
        )

        # Create /checkin/lift-sets resource
        lift_sets_resource = checkin_resource.add_resource("lift-sets")

        # Add POST method for lift-sets (batch create)
        lift_sets_resource.add_method(
            "POST",
            checkin_integration,
            api_key_required=True,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.CUSTOM,
        )

        # Add GET method for lift-sets (paginated, most recent first)
        lift_sets_resource.add_method(
            "GET",
            checkin_integration,
            api_key_required=True,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.CUSTOM,
        )

        # Add DELETE method for lift-sets (batch soft delete)
        lift_sets_resource.add_method(
            "DELETE",
            checkin_integration,
            api_key_required=True,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.CUSTOM,
        )

        # Create /checkin/estimated-1rm resource
        estimated_1rm_resource = checkin_resource.add_resource("estimated-1rm")

        # Add POST method for estimated-1rm (batch create)
        estimated_1rm_resource.add_method(
            "POST",
            checkin_integration,
            api_key_required=True,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.CUSTOM,
        )

        # Add GET method for estimated-1rm (paginated, most recent first)
        estimated_1rm_resource.add_method(
            "GET",
            checkin_integration,
            api_key_required=True,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.CUSTOM,
        )

        # Add DELETE method for estimated-1rm (batch soft delete)
        estimated_1rm_resource.add_method(
            "DELETE",
            checkin_integration,
            api_key_required=True,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.CUSTOM,
        )
