"""OpenAI client for generating training insights with structured output."""

import os
import json
import logging
import time

import boto3

logger = logging.getLogger(__name__)

# Module-level caches (persist across warm Lambda invocations)
_api_key = None
_client = None

INSIGHTS_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "weekly_insights",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "sections": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "body": {"type": "string"},
                        },
                        "required": ["title", "body"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["sections"],
            "additionalProperties": False,
        },
    },
}


def _get_api_key() -> str:
    """Retrieve OpenAI API key from SSM Parameter Store with module-level cache."""
    global _api_key
    if _api_key is not None:
        return _api_key

    param_name = os.environ.get('OPENAI_API_KEY_PARAM')
    if not param_name:
        raise ValueError("OPENAI_API_KEY_PARAM environment variable not set")

    ssm = boto3.client('ssm')
    response = ssm.get_parameter(Name=param_name, WithDecryption=True)
    _api_key = response['Parameter']['Value']

    if _api_key.startswith('PLACEHOLDER'):
        raise ValueError(f"OpenAI API key has not been set in SSM parameter {param_name}")

    return _api_key


def _get_client():
    """Get OpenAI client with module-level cache."""
    global _client
    if _client is not None:
        return _client

    from openai import OpenAI
    _client = OpenAI(api_key=_get_api_key())
    return _client


STARTER_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "starter_insight",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "body": {"type": "string"},
            },
            "required": ["body"],
            "additionalProperties": False,
        },
    },
}


def generate_starter_insight(system_prompt: str, curated_data: str) -> str:
    """
    Call OpenAI with structured output to generate a one-time starter insight.

    Args:
        system_prompt: The starter context markdown
        curated_data: Pre-computed strength status for the user prompt

    Returns:
        Single string body of the starter insight

    Raises:
        Exception: If all retry attempts fail
    """
    client = _get_client()
    model = os.environ.get('OPENAI_MODEL', 'gpt-5.4')

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": curated_data},
                ],
                response_format=STARTER_SCHEMA,
                temperature=0.7,
            )

            content = response.choices[0].message.content
            parsed = json.loads(content)
            body = parsed.get("body", "")

            logger.info(f"Generated starter insight using {model}")
            return body

        except Exception as e:
            logger.warning(f"OpenAI starter attempt {attempt + 1}/{max_retries} failed: {e}")
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                time.sleep(wait_time)
            else:
                raise


def generate_insights(system_prompt: str, curated_data: str) -> list[dict]:
    """
    Call OpenAI with structured output to generate weekly insights.

    Args:
        system_prompt: The app context markdown (cached by OpenAI across calls)
        curated_data: Pre-computed training summary for the user prompt

    Returns:
        List of {title, body} section dicts

    Raises:
        Exception: If all retry attempts fail
    """
    client = _get_client()
    model = os.environ.get('OPENAI_MODEL', 'gpt-5.4')

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": curated_data},
                ],
                response_format=INSIGHTS_SCHEMA,
                temperature=0.7,
            )

            content = response.choices[0].message.content
            parsed = json.loads(content)
            sections = parsed.get("sections", [])

            logger.info(f"Generated {len(sections)} insight sections using {model}")
            return sections

        except Exception as e:
            logger.warning(f"OpenAI attempt {attempt + 1}/{max_retries} failed: {e}")
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                time.sleep(wait_time)
            else:
                raise
