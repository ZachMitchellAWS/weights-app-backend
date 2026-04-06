"""Email processing Lambda handler.

This Lambda function processes email sending requests by:
1. Reading email template from S3
2. Replacing template variables with provided values
3. Sending email via SES with configuration set for tracking

Invocation payload format:
{
    "emailAddress": "user@example.com",
    "templateType": "password-reset",
    "variables": {
        "PASSWORD_RESET_CODE": "123456",
        "EXPIRY_TIME": "1 hour"
    }
}
"""

import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import boto3
import traceback
from typing import Dict, Any
from utils.sentry_init import init_sentry
import sentry_sdk

init_sentry()

# Initialize AWS clients
s3_client = boto3.client('s3')
ses_client = boto3.client('ses')


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Process email sending request.

    Args:
        event: Lambda event containing email details
        context: Lambda context

    Returns:
        Success/failure status
    """
    try:
        print(f"Processing email request: {json.dumps(event)}")

        # Extract parameters
        email_address = event.get('emailAddress')
        template_type = event.get('templateType')
        variables = event.get('variables', {})

        # Validate required fields
        if not email_address or not template_type:
            raise ValueError("emailAddress and templateType are required")

        # Get configuration from environment
        templates_bucket = os.environ.get('TEMPLATES_BUCKET')
        sender_email = os.environ.get('SENDER_EMAIL')
        password_reset_config_set = os.environ.get('PASSWORD_RESET_CONFIG_SET')
        welcome_config_set = os.environ.get('WELCOME_CONFIG_SET')

        if not all([templates_bucket, sender_email, password_reset_config_set, welcome_config_set]):
            raise ValueError("Missing required environment variables")

        # Map template type to configuration set
        config_set_mapping = {
            'password-reset': password_reset_config_set,
            'welcome': welcome_config_set,
        }

        configuration_set = config_set_mapping.get(template_type)
        if not configuration_set:
            raise ValueError(f"Unknown template type: {template_type}")

        # Read template from S3
        template_key = f"{template_type}.html"
        print(f"Reading template from s3://{templates_bucket}/{template_key}")

        response = s3_client.get_object(
            Bucket=templates_bucket,
            Key=template_key
        )
        template_content = response['Body'].read().decode('utf-8')

        # Replace variables in template
        email_html = template_content
        for var_name, var_value in variables.items():
            placeholder = f"{{{{{var_name}}}}}"  # {{VARIABLE}}
            email_html = email_html.replace(placeholder, str(var_value))

        # Determine subject based on template type
        subjects = {
            'password-reset': 'Password Reset Code',
            'welcome': 'Welcome!',
        }
        subject = subjects.get(template_type, 'Notification')

        # Send email via SES
        print(f"Sending email to {email_address} with configuration set {configuration_set}")

        ses_response = ses_client.send_email(
            Source=sender_email,
            Destination={'ToAddresses': [email_address]},
            Message={
                'Subject': {'Data': subject, 'Charset': 'UTF-8'},
                'Body': {
                    'Html': {'Data': email_html, 'Charset': 'UTF-8'}
                }
            },
            ConfigurationSetName=configuration_set
        )

        message_id = ses_response['MessageId']
        print(f"Email sent successfully. MessageId: {message_id}")

        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Email sent successfully',
                'messageId': message_id
            })
        }

    except Exception as e:
        sentry_sdk.capture_exception(e)
        print(f"Error processing email: {str(e)}")
        print(traceback.format_exc())

        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': 'Failed to send email',
                'message': str(e)
            })
        }
