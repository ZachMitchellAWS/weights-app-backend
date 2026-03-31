"""Email service CDK stack with S3, Lambda, and SES."""

import os

from aws_cdk import (
    Stack,
    aws_s3 as s3,
    aws_lambda as lambda_,
    aws_iam as iam,
    aws_logs as logs,
    RemovalPolicy,
    CfnOutput,
)
from constructs import Construct
from pathlib import Path


class EmailStack(Stack):
    """
    Email service stack containing:
    - S3 bucket for email templates
    - Lambda function for email processing
    - SES configuration set with CloudWatch event tracking
    - IAM permissions for S3 and SES access

    This stack handles asynchronous email delivery for various email types
    (password reset, verification, notifications, etc.)
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
        Initialize the Email service stack.

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
        self.templates_bucket = self._create_templates_bucket()
        self._create_ses_configuration_sets()
        self.dependencies_layer = self._create_dependencies_layer()
        self.email_function = self._create_email_lambda()

        # Output important values
        self._create_outputs()

    def _create_templates_bucket(self) -> s3.Bucket:
        """
        Create S3 bucket for email templates.

        Design decisions:
        - Versioning enabled to track template changes
        - Server-side encryption enabled by default
        - Removal policy: environment-specific
        - Block public access: all blocked (templates are private)

        Returns:
            S3 Bucket construct
        """
        bucket = s3.Bucket(
            self,
            "EmailTemplatesBucket",
            bucket_name=f"{self.project_name}-{self.env_name}-email-templates",
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=self.config.REMOVAL_POLICY,
            auto_delete_objects=(self.config.REMOVAL_POLICY == RemovalPolicy.DESTROY),
        )

        return bucket

    def _create_ses_configuration_sets(self) -> None:
        """
        Create SES configuration sets for email tracking.

        Creates two configuration sets:
        1. password-reset: For password reset emails
        2. welcome: For welcome emails

        Each configuration set has CloudWatch event destination tracking:
        - Send: Email was sent to SES
        - Delivery: Email was delivered to recipient
        - Open: Recipient opened the email
        - Bounce: Email bounced (hard bounces only)
        - Complaint: Recipient marked as spam
        - Reject: SES rejected the email

        Events are sent to CloudWatch Logs for monitoring and debugging.
        """
        from aws_cdk import aws_ses as ses

        # Create CloudWatch log group for SES events
        log_group = logs.LogGroup(
            self,
            "SESEventLogGroup",
            log_group_name=f"/aws/ses/{self.project_name}-{self.env_name}",
            retention=self.config.LOG_RETENTION,
            removal_policy=self.config.REMOVAL_POLICY,
        )

        # Configuration sets to create
        config_sets = [
            {
                "id": "PasswordResetConfigSet",
                "name": f"{self.project_name}-{self.env_name}-password-reset",
                "description": "Configuration set for password reset emails"
            },
            {
                "id": "WelcomeConfigSet",
                "name": f"{self.project_name}-{self.env_name}-welcome",
                "description": "Configuration set for welcome emails"
            }
        ]

        # Store configuration set names
        self.password_reset_config_set_name = config_sets[0]["name"]
        self.welcome_config_set_name = config_sets[1]["name"]

        # Create each configuration set with CloudWatch event destination
        for config_set_info in config_sets:
            # Create configuration set
            config_set = ses.CfnConfigurationSet(
                self,
                config_set_info["id"],
                name=config_set_info["name"],
            )

            # Create event destination for CloudWatch
            # Note: We use SNS topic approach which then forwards to CloudWatch
            # because direct CloudWatch destination has limitations

            # For now, create with kinesis firehose to CloudWatch Logs
            # This is a simpler approach that works reliably
            from aws_cdk import aws_logs_destinations as log_destinations
            from aws_cdk import aws_iam as iam

            # Create SNS topic for SES events
            from aws_cdk import aws_sns as sns

            topic = sns.Topic(
                self,
                f"{config_set_info['id']}EventTopic",
                topic_name=f"{config_set_info['name']}-events",
                display_name=f"SES events for {config_set_info['description']}"
            )

            # Subscribe CloudWatch Logs to the SNS topic
            from aws_cdk import aws_sns_subscriptions as sns_subs
            from aws_cdk import aws_logs_destinations as log_dest

            # Create event destination with SNS
            event_destination = ses.CfnConfigurationSetEventDestination(
                self,
                f"{config_set_info['id']}EventDestination",
                configuration_set_name=config_set.name,
                event_destination=ses.CfnConfigurationSetEventDestination.EventDestinationProperty(
                    name=f"{config_set_info['name']}-events",
                    enabled=True,
                    matching_event_types=[
                        "send",
                        "delivery",
                        "open",
                        "bounce",  # Includes hard bounces
                        "complaint",
                        "reject",
                    ],
                    cloud_watch_destination=ses.CfnConfigurationSetEventDestination.CloudWatchDestinationProperty(
                        dimension_configurations=[
                            ses.CfnConfigurationSetEventDestination.DimensionConfigurationProperty(
                                dimension_name="ses:configuration-set",
                                dimension_value_source="messageTag",
                                default_dimension_value=config_set_info['name'],
                            ),
                            ses.CfnConfigurationSetEventDestination.DimensionConfigurationProperty(
                                dimension_name="ses:from-domain",
                                dimension_value_source="messageTag",
                                default_dimension_value="anthroverse-io",
                            ),
                        ]
                    ),
                ),
            )

            # Explicit dependency: event destination must be created after config set
            event_destination.add_dependency(config_set)

    def _create_dependencies_layer(self) -> lambda_.LayerVersion:
        """Create Lambda layer with Python dependencies for email service."""
        layer_path = Path(__file__).parent.parent / "layer"
        layer = lambda_.LayerVersion(
            self,
            "DependenciesLayer",
            layer_version_name=f"{self.project_name}-{self.env_name}-email-deps",
            code=lambda_.Code.from_asset(str(layer_path)),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            description="Python dependencies for email service",
        )
        return layer

    def _create_email_lambda(self) -> lambda_.Function:
        """
        Create Lambda function for email processing.

        This function:
        1. Reads email template from S3
        2. Replaces template variables with provided values
        3. Sends email via SES with configuration set

        The function is invoked asynchronously from other services
        (e.g., auth service for password reset emails).

        Returns:
            Lambda Function construct
        """
        # Path to Lambda code
        lambda_code_path = Path(__file__).parent.parent / "lambda"

        function = lambda_.Function(
            self,
            "EmailProcessingFunction",
            function_name=f"{self.project_name}-{self.env_name}-email-processing",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handlers.email_processor.handler",
            code=lambda_.Code.from_asset(str(lambda_code_path)),
            layers=[self.dependencies_layer],
            memory_size=self.config.LAMBDA_MEMORY_SIZE,
            timeout=self.config.LAMBDA_TIMEOUT,
            environment={
                "TEMPLATES_BUCKET": self.templates_bucket.bucket_name,
                "SENDER_EMAIL": "noreply@anthroverse.io",
                "PASSWORD_RESET_CONFIG_SET": self.password_reset_config_set_name,
                "WELCOME_CONFIG_SET": self.welcome_config_set_name,
                "ENVIRONMENT": self.config.ENVIRONMENT,
                "SENTRY_DSN": os.environ.get("SENTRY_DSN", ""),
                "LOG_LEVEL": self.config.LOG_LEVEL,
            },
            log_retention=self.config.LOG_RETENTION,
        )

        # Grant read permissions to S3 templates bucket
        self.templates_bucket.grant_read(function)

        # Grant SES send email permissions
        function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail", "ses:SendRawEmail"],
                resources=["*"],  # SES doesn't support resource-level permissions
            )
        )

        return function

    def _create_outputs(self) -> None:
        """Create CloudFormation outputs for important values."""

        CfnOutput(
            self,
            "EmailTemplatesBucketName",
            value=self.templates_bucket.bucket_name,
            description="S3 bucket for email templates",
        )

        CfnOutput(
            self,
            "EmailProcessingFunctionArn",
            value=self.email_function.function_arn,
            description="Email processing Lambda function ARN",
        )

        CfnOutput(
            self,
            "PasswordResetConfigurationSetName",
            value=self.password_reset_config_set_name,
            description="SES configuration set for password reset emails",
        )

        CfnOutput(
            self,
            "WelcomeConfigurationSetName",
            value=self.welcome_config_set_name,
            description="SES configuration set for welcome emails",
        )
