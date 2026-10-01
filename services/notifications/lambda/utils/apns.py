"""APNs delivery — ES256 provider token + HTTP/2 push.

WHY httpx. APNs speaks HTTP/2 exclusively; `urllib`/`requests` cannot talk to it at all. The
layer pulls `httpx[http2]`, which brings the h2 stack.

WHY THE JWT IS CACHED. Apple rejects provider tokens refreshed more often than once every 20
minutes and accepts a given token for an hour. Minting one per send gets the sender throttled
with `TooManyProviderTokenUpdates`, which looks like a delivery failure and is not. The cache
lives in module scope so it survives across warm invocations.

THE ENVIRONMENT TRAP, which is the part that silently breaks everything. `WeightApp.entitlements`
declares `aps-environment: development`, but Xcode rewrites that to `production` for App Store
AND TestFlight distribution. So a TestFlight build of the *staging* app yields a PRODUCTION APNs
token, while an Xcode-installed build of the same app yields a SANDBOX one. Because
`APP_BUNDLE_ID_SUFFIX` is empty in both xcconfigs, staging and production share one bundle id
and one APNs namespace, so the staging backend legitimately holds both kinds for one user.

Host selection is therefore PER TOKEN, never per backend environment. Getting this wrong
presents as "notifications silently never arrive", with a 400 BadDeviceToken buried in a log —
so on that specific response the sender flips host and retries once, and reports which
environment actually worked so the stored value can be corrected.
"""

import json
import os
import time
import uuid
from dataclasses import dataclass

import boto3
import httpx
import jwt

# Apple's two hosts. Which one a token belongs to is a property of the BUILD that produced it.
HOST_PRODUCTION = "https://api.push.apple.com"
HOST_SANDBOX = "https://api.sandbox.push.apple.com"

ENV_PRODUCTION = "production"
ENV_SANDBOX = "sandbox"

# Apple's floor is 20 minutes and its ceiling is 60. Refreshing at 45 keeps us clear of both
# the throttle and the expiry without needing to reason about clock skew.
_TOKEN_TTL_SECONDS = 45 * 60

_ssm = None
_credentials: dict | None = None
_provider_token: tuple[str, float] | None = None  # (jwt, minted_at_epoch)


@dataclass
class ApnsResult:
    """Outcome of one delivery attempt against one token."""
    status_code: int
    apns_id: str | None
    reason: str | None
    environment: str          # the environment that actually answered
    ok: bool

    @property
    def token_is_dead(self) -> bool:
        """Apple saying this token will never work again.

        410 Unregistered means the app was uninstalled. 400 BadDeviceToken after BOTH hosts have
        been tried means it is malformed rather than merely pointed at the wrong environment.
        Either way the right move is to stop using it, not to retry.
        """
        if self.status_code == 410:
            return True
        return self.status_code == 400 and self.reason in {
            "BadDeviceToken", "DeviceTokenNotForTopic",
        }

    @property
    def is_transient(self) -> bool:
        """Worth retrying on the next sweep rather than giving up on."""
        return self.status_code in {429, 500, 503} or self.status_code == 0


def _ssm_parameter(name: str) -> str:
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm")
    return _ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def _load_credentials() -> dict:
    """Read the APNs signing credentials from SSM once per container.

    Same shape as the entitlements service's Apple credentials
    (`services/entitlements/lambda/utils/apple_api.py`): one parameter each, SecureString for the
    key itself, resolved at runtime rather than baked into the environment.
    """
    global _credentials
    if _credentials is None:
        _credentials = {
            "private_key": _ssm_parameter(os.environ["APNS_KEY_PARAM"]),
            "key_id": _ssm_parameter(os.environ["APNS_KEY_ID_PARAM"]),
            "team_id": _ssm_parameter(os.environ["APNS_TEAM_ID_PARAM"]),
        }
    return _credentials


def _provider_jwt() -> str:
    """The `bearer` token for the authorization header, minted at most every 45 minutes."""
    global _provider_token
    now = time.time()
    if _provider_token and (now - _provider_token[1]) < _TOKEN_TTL_SECONDS:
        return _provider_token[0]

    creds = _load_credentials()
    token = jwt.encode(
        {"iss": creds["team_id"], "iat": int(now)},
        creds["private_key"],
        algorithm="ES256",
        headers={"kid": creds["key_id"]},
    )
    _provider_token = (token, now)
    return token


def _host_for(environment: str) -> str:
    return HOST_SANDBOX if environment == ENV_SANDBOX else HOST_PRODUCTION


def _post(host: str, device_token: str, payload: dict, topic: str,
          collapse_id: str | None) -> ApnsResult:
    headers = {
        "authorization": f"bearer {_provider_jwt()}",
        "apns-topic": topic,
        "apns-push-type": "alert",
        "apns-priority": "10",
        "apns-id": str(uuid.uuid4()),
    }
    if collapse_id:
        headers["apns-collapse-id"] = collapse_id[:64]

    environment = ENV_SANDBOX if host == HOST_SANDBOX else ENV_PRODUCTION
    try:
        with httpx.Client(http2=True, timeout=10.0) as client:
            resp = client.post(f"{host}/3/device/{device_token}",
                               content=json.dumps(payload), headers=headers)
    except httpx.HTTPError as e:
        # Network-level failure. Status 0 marks it transient — the task stays queued rather than
        # being deleted on what may be a blip on our side, not Apple's.
        return ApnsResult(0, None, f"transport: {type(e).__name__}", environment, False)

    reason = None
    if resp.status_code != 200 and resp.content:
        try:
            reason = resp.json().get("reason")
        except ValueError:
            reason = resp.text[:120]

    return ApnsResult(
        status_code=resp.status_code,
        apns_id=resp.headers.get("apns-id"),
        reason=reason,
        environment=environment,
        ok=resp.status_code == 200,
    )


def send(device_token: str, title: str, body: str, environment: str,
         category: str | None = None, collapse_id: str | None = None) -> ApnsResult:
    """Deliver one alert to one token.

    `environment` is the token's own recorded environment, not the backend's. On a
    BadDeviceToken the other host is tried once — that is precisely the symptom of a token
    filed under the wrong environment, and the returned `environment` tells the caller which
    host actually worked so the stored value can be corrected.
    """
    topic = os.environ.get("APNS_TOPIC")
    if not topic:
        raise ValueError("APNS_TOPIC environment variable not set")

    aps: dict = {"alert": {"title": title, "body": body}, "sound": "default"}
    if category:
        aps["category"] = category
    payload = {"aps": aps}

    result = _post(_host_for(environment), device_token, payload, topic, collapse_id)

    if result.status_code == 400 and result.reason in {"BadDeviceToken", "DeviceTokenNotForTopic"}:
        other = ENV_PRODUCTION if environment == ENV_SANDBOX else ENV_SANDBOX
        retry = _post(_host_for(other), device_token, payload, topic, collapse_id)
        # Only prefer the retry if it actually succeeded; a second failure should surface the
        # original environment's answer rather than implying the token belongs elsewhere.
        if retry.ok:
            return retry
        return retry if retry.status_code != 400 else result

    return result
