"""OpenAI client for session generation, with structured output.

Deliberately different from the insights client in one respect: **there is no retry loop.**

Insights retries three times with 1s and 2s backoff, which is fine for an async task. This
endpoint is synchronous behind API Gateway's fixed 29-second ceiling, and three attempts plus
backoff would blow through it — turning a slow model into a gateway 504 with no error body
the client can act on. One attempt, a hard wall-clock deadline inside the budget, and a clean
503 the app can offer Retry against.

**On the deadline.** Passing `timeout=20.0` to the OpenAI client is NOT a 20-second cap.
The SDK hands that to httpx, which applies it per phase — connect, read, write, pool —
and httpx has no concept of a total timeout at all. A response that keeps trickling resets
the read clock on every chunk, so a "20 second" request observed 28 seconds of wall clock
and was killed by the Lambda instead, which returns a bare 502 through API Gateway with no
body the client can act on.

So the real bound is a SIGALRM deadline around the whole call, and it is derived from the
Lambda's own remaining time rather than hardcoded — a slow cold start or a heavy payload
build eats into the same budget, and a fixed constant cannot see that.
"""

import json
import logging
import os
import signal
import threading
from contextlib import contextmanager

import boto3

logger = logging.getLogger(__name__)

# Module-level caches, persisting across warm invocations.
_api_key = None
_client = None

# Handed to the SDK's transport, which applies it per phase (connect, read, write). A
# backstop only — a response that keeps trickling resets the read clock on every chunk, so
# this bounds nothing on its own. `_deadline` is the real limit.
TRANSPORT_TIMEOUT_SECONDS = 25.0

# Fallback when the caller cannot supply a deadline (no Lambda context).
DEFAULT_DEADLINE_SECONDS = 20.0


class GenerationTimeout(Exception):
    """The generation ran past its wall-clock deadline."""


@contextmanager
def _deadline(seconds: float):
    """Raise `GenerationTimeout` if the enclosed block runs longer than `seconds`.

    SIGALRM, because it is the only thing here that interrupts a blocked socket read in
    the thread that is doing the reading. A watchdog thread cannot cancel the call, and
    httpx cannot express a total timeout.

    Signals are deliverable only on the main thread. Lambda runs the handler there, so this
    is the normal path — but it degrades to the httpx bounds rather than raising if that
    ever stops being true.
    """
    if threading.current_thread() is not threading.main_thread():
        logger.warning("Not on the main thread; falling back to httpx timeouts")
        yield
        return

    if seconds <= 0:
        raise GenerationTimeout("No time left to attempt generation")

    def _fire(_signum, _frame):
        raise GenerationTimeout(f"Generation exceeded its {seconds:.1f}s deadline")

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        # Always disarm, even on the raise — a live timer would fire into whatever the
        # warm container runs next.
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)

SESSION_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "generated_session",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "exercise_id": {"type": "string"},
                            "exercise_name": {"type": "string"},
                            "set_plan_id": {"type": "string"},
                            "set_plan_name": {"type": "string"},
                            "rationale": {"type": "string"},
                        },
                        "required": [
                            "exercise_id", "exercise_name",
                            "set_plan_id", "set_plan_name", "rationale",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["summary", "items"],
            "additionalProperties": False,
        },
    },
}


def _get_api_key() -> str:
    global _api_key
    if _api_key is not None:
        return _api_key

    param_name = os.environ.get("OPENAI_API_KEY_PARAM")
    if not param_name:
        raise ValueError("OPENAI_API_KEY_PARAM environment variable not set")

    ssm = boto3.client("ssm")
    response = ssm.get_parameter(Name=param_name, WithDecryption=True)
    _api_key = response["Parameter"]["Value"]

    if _api_key.startswith("PLACEHOLDER"):
        raise ValueError(f"OpenAI API key has not been set in SSM parameter {param_name}")

    return _api_key


def _get_client():
    global _client
    if _client is not None:
        return _client

    from openai import OpenAI

    # A plain float, NOT an httpx.Timeout. openai 3.x depends on `httpx2`, not `httpx` —
    # there is no `httpx` module in the layer, and importing one to build a Timeout object
    # crashed every request with ModuleNotFoundError. A float is applied to all phases by
    # whichever transport the SDK vendors, which is the whole point of it here, and it
    # keeps this file from caring what that transport is called this year.
    #
    # Either way it is only a backstop: `_deadline` below is what actually bounds the call.
    _client = OpenAI(
        api_key=_get_api_key(),
        timeout=TRANSPORT_TIMEOUT_SECONDS,
        max_retries=0,
    )
    return _client


def generate_session(
    system_prompt: str,
    payload_json: str,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
) -> dict:
    """One attempt, bounded by a real wall-clock deadline.

    Raises `GenerationTimeout` when the deadline passes and whatever the SDK raises
    otherwise; the caller maps both to a retryable 503.

    Returns the parsed {summary, items[]} dict. The caller must still validate the ids
    against what was sent — a schema guarantees shape, never truthfulness.
    """
    client = _get_client()
    model = os.environ.get("OPENAI_MODEL", "gpt-5.4")

    logger.info("Generating with a %.1fs deadline", deadline_seconds)

    with _deadline(deadline_seconds):
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload_json},
            ],
            response_format=SESSION_SCHEMA,
            temperature=0.7,
        )

    parsed = json.loads(response.choices[0].message.content)
    logger.info(
        "Generated session with %d items using %s",
        len(parsed.get("items", [])), model,
    )
    return parsed
