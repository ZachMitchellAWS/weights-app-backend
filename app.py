#!/usr/bin/env python3
"""
CDK Application Entry Point

This is the main entry point for the CDK application. It:
1. Loads environment-specific configuration (staging or production)
2. Creates CDK stacks for each microservice
3. Applies global tags to all resources
4. Synthesizes CloudFormation templates

Usage:
    # Deploy to staging (default)
    cdk deploy

    # Deploy to production
    cdk deploy -c env=production

    # Synthesize staging templates
    cdk synth -c env=staging
"""

import aws_cdk as cdk
from aws_cdk import Tags

# Import configuration modules
from config import base, staging, production

# Import service stacks
from services.auth.infrastructure.auth_stack import AuthStack
from services.user.infrastructure.user_stack import UserStack
from services.email.infrastructure.email_stack import EmailStack


def main():
    """
    Main application function.

    This function:
    1. Creates the CDK App instance
    2. Determines which environment to deploy (staging or production)
    3. Loads the appropriate configuration
    4. Creates all service stacks
    5. Applies global tags
    6. Synthesizes the CloudFormation templates
    """
    # Create CDK app
    app = cdk.App()

    # Get environment from context (default to staging)
    # This allows: cdk deploy -c env=production
    env_name = app.node.try_get_context("env")
    if env_name is None:
        env_name = "staging"
        print(f"No environment specified, defaulting to: {env_name}")

    # Validate environment
    if env_name not in ["staging", "production"]:
        raise ValueError(f"Invalid environment: {env_name}. Must be 'staging' or 'production'")

    # Load configuration module based on environment
    config = staging if env_name == "staging" else production

    # Get project name from base configuration
    project_name = base.PROJECT_NAME

    print(f"Deploying to environment: {env_name}")
    print(f"Project: {project_name}")
    print(f"Account: {config.ACCOUNT_ID}, Region: {config.REGION}")

    # Create CDK environment object
    # This specifies which AWS account and region to deploy to
    env = cdk.Environment(
        account=config.ACCOUNT_ID,
        region=config.REGION
    )

    # Create Auth service stack
    # Stack naming pattern: {project_name}-{env_name}-{service_name}
    auth_stack = AuthStack(
        app,
        f"{project_name}-{env_name}-auth",
        project_name=project_name,
        env_name=env_name,
        config=config,
        env=env,
        description=f"Auth service stack for {env_name} environment",
    )

    # Apply global tags to all resources in the stack
    # Tags help with cost allocation, resource organization, and automation
    for key, value in config.TAGS.items():
        Tags.of(auth_stack).add(key, value)

    # Add service-specific tag
    Tags.of(auth_stack).add("Service", "auth")

    # Create Email service stack
    # This stack provides email sending functionality via SES
    email_stack = EmailStack(
        app,
        f"{project_name}-{env_name}-email",
        project_name=project_name,
        env_name=env_name,
        config=config,
        env=env,
        description=f"Email service stack for {env_name} environment",
    )

    # Apply global tags to email stack
    for key, value in config.TAGS.items():
        Tags.of(email_stack).add(key, value)

    # Add service-specific tag
    Tags.of(email_stack).add("Service", "email")

    # Grant auth Lambda permission to invoke email Lambda
    # This allows the auth service to send password reset emails
    email_stack.email_function.grant_invoke(auth_stack.auth_function)

    # Add email Lambda ARN to auth Lambda environment
    # This allows the auth service to invoke the email Lambda
    auth_stack.auth_function.add_environment(
        "EMAIL_LAMBDA_ARN",
        email_stack.email_function.function_arn
    )

    # Create User service stack
    # This stack depends on the auth stack because it needs the API and authorizer
    user_stack = UserStack(
        app,
        f"{project_name}-{env_name}-user",
        project_name=project_name,
        env_name=env_name,
        config=config,
        api=auth_stack.api,
        authorizer=auth_stack.authorizer,
        env=env,
        description=f"User service stack for {env_name} environment",
    )

    # Apply global tags to user stack
    for key, value in config.TAGS.items():
        Tags.of(user_stack).add(key, value)

    # Add service-specific tag
    Tags.of(user_stack).add("Service", "user")

    # Grant auth Lambda write permissions to user_properties table
    # This allows the auth service to create user_properties items when creating users
    user_stack.user_properties_table.grant_write_data(auth_stack.auth_function)

    # Add user_properties table name to auth Lambda environment
    # This allows the auth service to create user_properties items during user creation
    auth_stack.auth_function.add_environment(
        "USER_PROPERTIES_TABLE_NAME",
        user_stack.user_properties_table.table_name
    )

    # Synthesize CloudFormation templates
    # This generates the templates that will be deployed to AWS
    app.synth()


if __name__ == "__main__":
    main()
