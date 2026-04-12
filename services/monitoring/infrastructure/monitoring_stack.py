"""
CloudWatch Monitoring Dashboard stack.

Creates a unified dashboard with API Gateway, Lambda, DynamoDB,
SES, and Insights pipeline metrics.
"""

from aws_cdk import (
    Duration,
    Stack,
    aws_cloudwatch as cw,
)
from constructs import Construct


class MonitoringStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        project_name: str,
        env_name: str,
        config: any,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.project_name = project_name
        self.env_name = env_name

        api_name = f"{project_name}-{env_name}-api"

        lambda_functions = [
            ("Auth", f"{project_name}-{env_name}-auth"),
            ("Checkin", f"{project_name}-{env_name}-checkin"),
            ("User", f"{project_name}-{env_name}-user"),
            ("Entitlements", f"{project_name}-{env_name}-entitlements"),
            ("Insights", f"{project_name}-{env_name}-insights"),
            ("Email", f"{project_name}-{env_name}-email-processing"),
        ]

        dynamo_tables = [
            ("lift-sets", f"{project_name}-{env_name}-lift-sets"),
            ("estimated-1rm", f"{project_name}-{env_name}-estimated-1rm"),
            ("exercises", f"{project_name}-{env_name}-exercises"),
            ("user-properties", f"{project_name}-{env_name}-user-properties"),
            ("users", f"{project_name}-{env_name}-users"),
            ("entitlement-grants", f"{project_name}-{env_name}-entitlement-grants"),
        ]

        dashboard = cw.Dashboard(
            self,
            "Dashboard",
            dashboard_name=f"Lift-the-Bull-{env_name.capitalize()}",
            default_interval=Duration.hours(3),
        )

        # ── Section 1: API Gateway Overview ─────────────────────────────────

        dashboard.add_widgets(
            cw.TextWidget(markdown="# API Gateway", width=24, height=1),
        )

        dashboard.add_widgets(
            cw.SingleValueWidget(
                title="Total Requests (5m)",
                metrics=[cw.Metric(
                    namespace="AWS/ApiGateway",
                    metric_name="Count",
                    dimensions_map={"ApiName": api_name},
                    statistic="Sum",
                    period=Duration.minutes(5),
                )],
                width=6,
                height=4,
            ),
            cw.GraphWidget(
                title="4xx / 5xx Errors",
                left=[
                    cw.Metric(
                        namespace="AWS/ApiGateway",
                        metric_name="4XXError",
                        dimensions_map={"ApiName": api_name},
                        statistic="Sum",
                        period=Duration.minutes(5),
                        label="4xx",
                        color="#ff9900",
                    ),
                    cw.Metric(
                        namespace="AWS/ApiGateway",
                        metric_name="5XXError",
                        dimensions_map={"ApiName": api_name},
                        statistic="Sum",
                        period=Duration.minutes(5),
                        label="5xx",
                        color="#d13212",
                    ),
                ],
                width=9,
                height=4,
            ),
            cw.GraphWidget(
                title="Latency (p50 / p90 / p99)",
                left=[
                    cw.Metric(
                        namespace="AWS/ApiGateway",
                        metric_name="Latency",
                        dimensions_map={"ApiName": api_name},
                        statistic="p50",
                        period=Duration.minutes(5),
                        label="p50",
                    ),
                    cw.Metric(
                        namespace="AWS/ApiGateway",
                        metric_name="Latency",
                        dimensions_map={"ApiName": api_name},
                        statistic="p90",
                        period=Duration.minutes(5),
                        label="p90",
                    ),
                    cw.Metric(
                        namespace="AWS/ApiGateway",
                        metric_name="Latency",
                        dimensions_map={"ApiName": api_name},
                        statistic="p99",
                        period=Duration.minutes(5),
                        label="p99",
                    ),
                ],
                width=9,
                height=4,
            ),
        )

        # ── Section 2: Lambda Functions ──────────────────────────────────────

        dashboard.add_widgets(
            cw.TextWidget(markdown="# Lambda Functions", width=24, height=1),
        )

        # Invocations overview (all functions on one graph)
        dashboard.add_widgets(
            cw.GraphWidget(
                title="Invocations",
                left=[
                    cw.Metric(
                        namespace="AWS/Lambda",
                        metric_name="Invocations",
                        dimensions_map={"FunctionName": fn_name},
                        statistic="Sum",
                        period=Duration.minutes(5),
                        label=label,
                    )
                    for label, fn_name in lambda_functions
                ],
                width=12,
                height=5,
            ),
            cw.GraphWidget(
                title="Errors",
                left=[
                    cw.Metric(
                        namespace="AWS/Lambda",
                        metric_name="Errors",
                        dimensions_map={"FunctionName": fn_name},
                        statistic="Sum",
                        period=Duration.minutes(5),
                        label=label,
                    )
                    for label, fn_name in lambda_functions
                ],
                width=12,
                height=5,
            ),
        )

        dashboard.add_widgets(
            cw.GraphWidget(
                title="Duration p50",
                left=[
                    cw.Metric(
                        namespace="AWS/Lambda",
                        metric_name="Duration",
                        dimensions_map={"FunctionName": fn_name},
                        statistic="p50",
                        period=Duration.minutes(5),
                        label=label,
                    )
                    for label, fn_name in lambda_functions
                ],
                width=8,
                height=5,
            ),
            cw.GraphWidget(
                title="Duration p99",
                left=[
                    cw.Metric(
                        namespace="AWS/Lambda",
                        metric_name="Duration",
                        dimensions_map={"FunctionName": fn_name},
                        statistic="p99",
                        period=Duration.minutes(5),
                        label=label,
                    )
                    for label, fn_name in lambda_functions
                ],
                width=8,
                height=5,
            ),
            cw.GraphWidget(
                title="Throttles",
                left=[
                    cw.Metric(
                        namespace="AWS/Lambda",
                        metric_name="Throttles",
                        dimensions_map={"FunctionName": fn_name},
                        statistic="Sum",
                        period=Duration.minutes(5),
                        label=label,
                    )
                    for label, fn_name in lambda_functions
                ],
                width=8,
                height=5,
            ),
        )

        # ── Section 3: DynamoDB ──────────────────────────────────────────────

        dashboard.add_widgets(
            cw.TextWidget(markdown="# DynamoDB", width=24, height=1),
        )

        dashboard.add_widgets(
            cw.GraphWidget(
                title="Consumed Read Capacity",
                left=[
                    cw.Metric(
                        namespace="AWS/DynamoDB",
                        metric_name="ConsumedReadCapacityUnits",
                        dimensions_map={"TableName": table_name},
                        statistic="Sum",
                        period=Duration.minutes(5),
                        label=short_name,
                    )
                    for short_name, table_name in dynamo_tables
                ],
                width=12,
                height=5,
            ),
            cw.GraphWidget(
                title="Consumed Write Capacity",
                left=[
                    cw.Metric(
                        namespace="AWS/DynamoDB",
                        metric_name="ConsumedWriteCapacityUnits",
                        dimensions_map={"TableName": table_name},
                        statistic="Sum",
                        period=Duration.minutes(5),
                        label=short_name,
                    )
                    for short_name, table_name in dynamo_tables
                ],
                width=12,
                height=5,
            ),
        )

        dashboard.add_widgets(
            cw.GraphWidget(
                title="Throttled Requests",
                left=[
                    cw.Metric(
                        namespace="AWS/DynamoDB",
                        metric_name="ThrottledRequests",
                        dimensions_map={"TableName": table_name},
                        statistic="Sum",
                        period=Duration.minutes(5),
                        label=short_name,
                    )
                    for short_name, table_name in dynamo_tables
                ],
                width=12,
                height=5,
            ),
            cw.GraphWidget(
                title="Successful Request Latency (Query)",
                left=[
                    cw.Metric(
                        namespace="AWS/DynamoDB",
                        metric_name="SuccessfulRequestLatency",
                        dimensions_map={"TableName": table_name, "Operation": "Query"},
                        statistic="Average",
                        period=Duration.minutes(5),
                        label=short_name,
                    )
                    for short_name, table_name in dynamo_tables
                ],
                width=12,
                height=5,
            ),
        )

        # ── Section 4: Email (SES) ───────────────────────────────────────────

        dashboard.add_widgets(
            cw.TextWidget(markdown="# Email (SES)", width=24, height=1),
        )

        ses_metrics = [
            ("Send", "Send", "#2ca02c"),
            ("Delivery", "Delivery", "#1f77b4"),
            ("Bounce", "Bounce", "#ff7f0e"),
            ("Complaint", "Complaint", "#d62728"),
        ]

        dashboard.add_widgets(
            cw.GraphWidget(
                title="Email Activity",
                left=[
                    cw.Metric(
                        namespace="AWS/SES",
                        metric_name=metric_name,
                        statistic="Sum",
                        period=Duration.hours(1),
                        label=label,
                        color=color,
                    )
                    for label, metric_name, color in ses_metrics
                ],
                width=24,
                height=5,
            ),
        )

        # ── Section 5: Insights Pipeline ─────────────────────────────────────

        insights_fn = f"{project_name}-{env_name}-insights"

        dashboard.add_widgets(
            cw.TextWidget(markdown="# Insights Pipeline", width=24, height=1),
        )

        dashboard.add_widgets(
            cw.GraphWidget(
                title="Insights Invocations",
                left=[
                    cw.Metric(
                        namespace="AWS/Lambda",
                        metric_name="Invocations",
                        dimensions_map={"FunctionName": insights_fn},
                        statistic="Sum",
                        period=Duration.minutes(15),
                        label="Invocations",
                    ),
                ],
                width=8,
                height=5,
            ),
            cw.GraphWidget(
                title="Insights Duration",
                left=[
                    cw.Metric(
                        namespace="AWS/Lambda",
                        metric_name="Duration",
                        dimensions_map={"FunctionName": insights_fn},
                        statistic="p50",
                        period=Duration.minutes(15),
                        label="p50",
                    ),
                    cw.Metric(
                        namespace="AWS/Lambda",
                        metric_name="Duration",
                        dimensions_map={"FunctionName": insights_fn},
                        statistic="p99",
                        period=Duration.minutes(15),
                        label="p99",
                    ),
                ],
                width=8,
                height=5,
            ),
            cw.GraphWidget(
                title="Insights Errors",
                left=[
                    cw.Metric(
                        namespace="AWS/Lambda",
                        metric_name="Errors",
                        dimensions_map={"FunctionName": insights_fn},
                        statistic="Sum",
                        period=Duration.minutes(15),
                        label="Errors",
                        color="#d13212",
                    ),
                ],
                width=8,
                height=5,
            ),
        )
