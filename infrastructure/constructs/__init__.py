"""
Reusable CDK constructs.

This directory is a placeholder for custom reusable CDK constructs.
Constructs allow you to create higher-level abstractions that can be
reused across multiple stacks.

Example constructs you might create:
- StandardLambdaFunction: Lambda with common settings
- StandardApiGateway: API Gateway with standard logging/CORS
- MonitoredTable: DynamoDB table with CloudWatch alarms
- SecureS3Bucket: S3 bucket with encryption and access logging

Example:
    from infrastructure.constructs.standard_lambda import StandardLambdaFunction

    my_function = StandardLambdaFunction(
        self, "MyFunction",
        handler="index.handler",
        code=lambda_.Code.from_asset("path/to/code")
    )
"""
