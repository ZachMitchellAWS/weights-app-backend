import os
import sentry_sdk
from sentry_sdk.integrations.aws_lambda import AwsLambdaIntegration

_initialized = False


def init_sentry():
    global _initialized
    if _initialized:
        return

    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return

    environment = os.environ.get("ENVIRONMENT", "staging")

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        send_default_pii=True,
        integrations=[AwsLambdaIntegration(timeout_warning=True)],
        traces_sample_rate=0.1,
    )
    _initialized = True


def set_sentry_user(user_id: str):
    """Set user context - matches the userId set on the iOS client."""
    sentry_sdk.set_user({"id": user_id})

