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
import time
from contextlib import contextmanager
from dataclasses import dataclass

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

# A dropped connection is retried ONCE, and only when this much budget survives it.
#
# Measured over 170 production generations: median 15.7s, p90 20.3s, against a ~25s budget
# (28s Lambda minus the 3s response reserve). So a retry usually has less than one median
# generation left to work with, and retrying below this floor converts a fast, actionable
# failure into a full-deadline wait for the same Retry button — worse than not retrying.
#
# At 16s roughly half of generations complete; 18.0 would lift that to about two thirds at the
# cost of firing even less often. THE FLOOR IS THE DESIGN: removing it to "simplify" the retry
# is what reintroduces the slow-failure case.
MIN_RETRY_BUDGET_SECONDS = 16.0


class GenerationTimeout(Exception):
    """The generation ran past its wall-clock deadline."""


@dataclass
class GenerationResult:
    """What came back, and what it took to get it.

    Returned rather than tracked in module state: these containers are reused across warm
    invocations, so a counter would attribute one request's retry to the next request.
    """
    session: dict
    attempts: int
    retry_reason: str | None = None   # "connection" when a retry fired, else None


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
                        # NO UUIDs. The model used to echo `exercise_id` and `set_plan_id`
                        # back, and it got them wrong: production logged
                        # `00000000-0000-0000-000000000121` — four groups instead of five,
                        # a dropped `0000-` — against a real id of
                        # `00000000-0000-0000-0000-000000000121`. It was not hallucinating,
                        # it was counting zeros and miscounted. The built-in ids are
                        # pathologically low-entropy and transcribing them is a token-level
                        # task models are bad at, which no prompt instruction can fix.
                        #
                        # So it returns a NAME (five fundamentals, all distinct) and a short
                        # `set_plan_ref` like "p7". The backend resolves both to real ids —
                        # see `_resolve_response`. A ref rather than a plan name because the
                        # catalog carries user-written plans and nothing stops someone naming
                        # one "Standard"; matching by name would silently pick the built-in.
                        "properties": {
                            "exercise_name": {"type": "string"},
                            "set_plan_ref": {"type": "string"},
                            "set_plan_name": {"type": "string"},
                            "rationale": {"type": "string"},
                        },
                        "required": [
                            "exercise_name", "set_plan_ref", "set_plan_name", "rationale",
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
    """Up to two attempts, bounded by ONE shared wall-clock deadline.

    A second attempt happens only when the first died of a dropped connection AND at least
    `MIN_RETRY_BUDGET_SECONDS` of the budget survives — see that constant for why the floor
    matters more than the retry.

    Raises `GenerationTimeout` when the deadline passes and whatever the SDK raises
    otherwise; the caller maps both to a retryable 503.

    Returns a `GenerationResult`. The caller must still validate the ids against what was
    sent — a schema guarantees shape, never truthfulness.
    """
    client = _get_client()
    # Terra rather than Sol deliberately. This endpoint is bounded by API Gateway's fixed 29s
    # timeout with ONE attempt and no retry, so wall clock is the binding constraint, not
    # reasoning depth — and the task is structured selection from a catalog we supply against
    # rules we state, which is not where a flagship model earns its latency.
    #
    # `reasoning_effort` is left at the model's default (medium). It is the lever to reach for
    # first if generations start hitting the deadline; the bundled SDK accepts
    # none | minimal | low | medium | high | xhigh | max.
    model = os.environ.get("OPENAI_MODEL", "gpt-5.6-terra")

    # Imported here rather than at module scope, matching the lazy `from openai import OpenAI`
    # in `_get_client()` — nothing in this module should pay the SDK import on a cold start
    # that never reaches generation.
    from openai import APIConnectionError

    logger.info("Generating with a %.1fs deadline", deadline_seconds)

    started = time.monotonic()
    attempts = 0
    retry_reason = None

    while True:
        attempts += 1
        # Derived from the ORIGINAL start on every pass, so both attempts share one budget.
        # A fresh deadline for attempt 2 could total ~46s against API Gateway's fixed 29s
        # ceiling, which returns a bare 504 with no body the client can act on — the exact
        # failure this endpoint's no-retry design exists to avoid.
        remaining = deadline_seconds - (time.monotonic() - started)
        try:
            with _deadline(remaining):
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": payload_json},
                    ],
                    response_format=SESSION_SCHEMA,
                )
            break
        except APIConnectionError as e:
            # ONLY this exception is retried. Not `GenerationTimeout` — retrying something
            # that just consumed the budget is incoherent. Not a rate limit — an immediate
            # retry fails the same way and there is no budget for backoff. Not an
            # `APIStatusError` — a 4xx is deterministic, so the same request returns the same
            # error.
            elapsed = time.monotonic() - started
            left = deadline_seconds - elapsed
            if attempts >= 2 or left < MIN_RETRY_BUDGET_SECONDS:
                logger.warning(
                    "Connection dropped after %.1fs; not retrying (attempt %d, %.1fs left)",
                    elapsed, attempts, left,
                )
                # Carried on the exception so the record reflects what actually happened —
                # a request that tried twice and still failed must not log as one attempt.
                e.generation_attempts = attempts
                raise
            logger.warning(
                "Connection dropped after %.1fs (%s); retrying with %.1fs left",
                elapsed, type(e).__name__, left,
            )
            retry_reason = "connection"

    parsed = json.loads(response.choices[0].message.content)
    logger.info(
        "Generated session with %d items using %s in %d attempt(s)",
        len(parsed.get("items", [])), model, attempts,
    )
    return GenerationResult(session=parsed, attempts=attempts, retry_reason=retry_reason)
