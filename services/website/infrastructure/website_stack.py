"""Website Stack - S3, CloudFront, Route53, DynamoDB, Lambda, API Gateway route.

Hosts the static React SPA on S3 behind CloudFront with a custom domain,
and provides a public support form endpoint via API Gateway + Lambda + DynamoDB.
"""

import os
from pathlib import Path

from aws_cdk import (
    Stack,
    CfnOutput,
    RemovalPolicy,
    aws_s3 as s3,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_route53 as route53,
    aws_route53_targets as route53_targets,
    aws_certificatemanager as acm,
    aws_dynamodb as dynamodb,
    aws_lambda as lambda_,
    aws_apigateway as apigateway,
)
from constructs import Construct

from config import base


class WebsiteStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        project_name: str,
        env_name: str,
        config,
        api: apigateway.RestApi,
        web_acl_arn: str = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.project_name = project_name
        self.env_name = env_name
        self.config = config
        self.api = api
        self.web_acl_arn = web_acl_arn

        self.website_bucket = self._create_s3_bucket()
        self.distribution = self._create_cloudfront_distribution()
        self._create_route53_records()
        self.support_tickets_table = self._create_support_tickets_table()
        self.dependencies_layer = self._create_dependencies_layer()
        self.support_function = self._create_support_lambda()
        self._create_api_route()

    def _create_s3_bucket(self) -> s3.Bucket:
        """Create private S3 bucket for website static files."""
        return s3.Bucket(
            self,
            "WebsiteBucket",
            bucket_name=f"{self.project_name}-{self.env_name}-website",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=self.config.REMOVAL_POLICY,
            auto_delete_objects=(self.config.REMOVAL_POLICY == RemovalPolicy.DESTROY),
        )

    def _create_cloudfront_distribution(self) -> cloudfront.Distribution:
        """Create CloudFront distribution with OAC for S3."""
        # Determine domain names for this environment
        if self.config.WEBSITE_SUBDOMAIN:
            domain_names = [f"{self.config.WEBSITE_SUBDOMAIN}.{base.DOMAIN_NAME}"]
        else:
            domain_names = [base.DOMAIN_NAME, f"www.{base.DOMAIN_NAME}"]

        # Import certificate from ARN (must be in us-east-1)
        certificate = None
        if self.config.CLOUDFRONT_CERT_ARN:
            certificate = acm.Certificate.from_certificate_arn(
                self,
                "ImportedCertificate",
                self.config.CLOUDFRONT_CERT_ARN,
            )

        response_headers_policy = cloudfront.ResponseHeadersPolicy(
            self,
            "WebsiteCorsPolicy",
            response_headers_policy_name=f"{self.project_name}-{self.env_name}-website-cors",
            cors_behavior=cloudfront.ResponseHeadersCorsBehavior(
                access_control_allow_origins=domain_names,
                access_control_allow_methods=["GET", "HEAD"],
                access_control_allow_headers=["*"],
                access_control_allow_credentials=False,
                origin_override=False,
            ),
        )

        distribution = cloudfront.Distribution(
            self,
            "WebsiteDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3Origin(self.website_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                response_headers_policy=response_headers_policy,
            ),
            domain_names=domain_names if certificate else None,
            certificate=certificate,
            web_acl_id=self.web_acl_arn if self.web_acl_arn else None,
            default_root_object="index.html",
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
            ],
        )

        CfnOutput(
            self,
            "DistributionId",
            value=distribution.distribution_id,
            description="CloudFront distribution ID (for cache invalidation)",
        )

        CfnOutput(
            self,
            "DistributionDomainName",
            value=distribution.distribution_domain_name,
            description="CloudFront distribution domain name",
        )

        return distribution

    def _create_route53_records(self) -> None:
        """Create Route53 A records aliased to CloudFront."""
        if not self.config.CLOUDFRONT_CERT_ARN:
            # Skip DNS records until cert is configured
            return

        hosted_zone = route53.HostedZone.from_lookup(
            self,
            "HostedZone",
            domain_name=base.DOMAIN_NAME,
        )

        target = route53.RecordTarget.from_alias(
            route53_targets.CloudFrontTarget(self.distribution)
        )

        if self.config.WEBSITE_SUBDOMAIN:
            # Staging: single subdomain record
            route53.ARecord(
                self,
                "WebsiteARecord",
                zone=hosted_zone,
                record_name=self.config.WEBSITE_SUBDOMAIN,
                target=target,
            )
        else:
            # Production: apex + www
            route53.ARecord(
                self,
                "WebsiteApexARecord",
                zone=hosted_zone,
                record_name="",
                target=target,
            )
            route53.ARecord(
                self,
                "WebsiteWwwARecord",
                zone=hosted_zone,
                record_name="www",
                target=target,
            )

    def _create_support_tickets_table(self) -> dynamodb.Table:
        """Create DynamoDB table for support form submissions."""
        return dynamodb.Table(
            self,
            "SupportTicketsTable",
            table_name=f"{self.project_name}-{self.env_name}-support-tickets",
            partition_key=dynamodb.Attribute(
                name="ticketId",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=self.config.DYNAMODB_BILLING_MODE,
            point_in_time_recovery=self.config.DYNAMODB_POINT_IN_TIME_RECOVERY,
            removal_policy=self.config.REMOVAL_POLICY,
        )

    def _create_dependencies_layer(self) -> lambda_.LayerVersion:
        """Create Lambda layer with Python dependencies for website service."""
        layer_path = Path(__file__).parent.parent / "layer"
        layer = lambda_.LayerVersion(
            self,
            "DependenciesLayer",
            layer_version_name=f"{self.project_name}-{self.env_name}-website-deps",
            code=lambda_.Code.from_asset(str(layer_path)),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_13],
            description="Python dependencies for website service",
        )
        return layer

    def _create_support_lambda(self) -> lambda_.Function:
        """Create Lambda for support form submissions."""
        lambda_code_path = Path(__file__).parent.parent / "lambda"

        function = lambda_.Function(
            self,
            "WebsiteSupportFunction",
            function_name=f"{self.project_name}-{self.env_name}-website-support",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="handlers.support.handler",
            code=lambda_.Code.from_asset(str(lambda_code_path)),
            layers=[self.dependencies_layer],
            memory_size=self.config.LAMBDA_MEMORY_SIZE,
            timeout=self.config.LAMBDA_TIMEOUT,
            environment={
                "SUPPORT_TICKETS_TABLE_NAME": self.support_tickets_table.table_name,
                "ENVIRONMENT": self.config.ENVIRONMENT,
                "SENTRY_DSN": os.environ.get("SENTRY_DSN", ""),
                "LOG_LEVEL": self.config.LOG_LEVEL,
            },
        )

        self.support_tickets_table.grant_write_data(function)

        return function

    def _create_api_route(self) -> None:
        """Add POST /website/support to existing API Gateway."""
        support_integration = apigateway.LambdaIntegration(
            self.support_function,
            proxy=True,
        )

        website_resource = self.api.root.add_resource("website")
        support_resource = website_resource.add_resource("support")

        support_resource.add_method(
            "POST",
            support_integration,
            api_key_required=False,
        )
