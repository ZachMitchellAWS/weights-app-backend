# Notifications API

Push notifications are almost entirely a backend concern: the app registers a device token and
then receives pushes. There is exactly one endpoint, it exists only in staging, and it is a
development affordance rather than product surface.

---

## `POST /notifications/test` — staging only

Sends the caller a push after a short delay, so both the delivery path and the device's
notification settings can be verified without waiting for a scheduled task.

**This route does not exist in production.** It is not hidden behind a feature flag — the CDK
stack only creates the API Gateway resource when `env == "staging"`, so a production request
returns 403 from API Gateway with no Lambda involvement. Gate the client button on
`APIConfig.environment == "staging"` as well, following the `PremiumOverride` pattern.

### Request

```jsonc
{
  "notificationType": "unlock-strength-tier-nudge",  // optional, this is the default
  "delaySeconds": 5                                   // optional, default 5, capped at 30
}
```

**`userId` is never read from the body.** It comes from the JWT claims, so this can only ever
push to the caller's own devices. A test endpoint that accepts an arbitrary target is not a
test endpoint.

### Response — `202 Accepted`

```json
{
  "message": "Queued unlock-strength-tier-nudge, sending in 5s",
  "notificationType": "unlock-strength-tier-nudge",
  "delaySeconds": 5
}
```

Returns immediately; the delay is served off the request path by an async self-invoke. A 202
means *queued*, not *delivered* — the push can still be skipped by the type's precondition or
fail at Apple. `notification-log` is where the actual outcome lands.

### Errors

| Status | When |
|---|---|
| 400 | `notificationType` is not a registered type |
| 403 | Production, or a missing/invalid API key or JWT |

### Why a send can produce no notification

A 202 followed by silence is usually correct behaviour, not a bug. Check `notification-log`
for the `outcome`:

- `skipped_precondition` — the type decided the user no longer needs it. For
  `unlock-strength-tier-nudge` that means `hasMetStrengthTierConditions` is now true. **Testing
  against your own account will hit this if you have already unlocked your tier.** To send
  anyway, set `bypassPrecondition: true` on the task item or in the `SEND_NOW` payload; the
  resulting log row carries a `-bypass` suffix on `pathway` so it is never mistaken for a
  normal send. Token checks still apply.
- `skipped_no_token` — no usable token: none registered, or all marked `invalid`/`loggedOut`.
- `failed_invalid_token` — Apple rejected the token permanently; it is now marked `invalid`.

---

## Token registration

There is no dedicated registration endpoint. The token still goes through
`POST /user/properties` as `apnsDeviceToken`, alongside every other user property, and the
backend mirrors it into the `apns-tokens` table.

```jsonc
{
  "apnsDeviceToken": "a1b2c3…",     // null to deregister
  "apnsEnvironment": "production"    // "sandbox" | "production", optional
}
```

**`apnsEnvironment` is a property of the build, not of which backend it talks to.** Xcode
rewrites the `aps-environment` entitlement to `production` for App Store *and TestFlight*
distribution, so only an Xcode-installed build produces a sandbox token. Send `sandbox` under
`#if DEBUG` and `production` otherwise. Omitting it defaults to `production`, and a wrong value
self-corrects: the sender retries the other host on `BadDeviceToken` and rewrites the stored
value from whichever one answered.

**Sending `apnsDeviceToken: null` marks every token for that user as logged out**, because the
request does not say which device it came from. That is safe because re-registering clears the
flag — signing back in on a device revives that pairing and leaves the others muted.

Call it on logout *before* auth credentials are cleared, or the request will 401.

### Phase-out in progress

The token is currently written to **both** `user-properties` and `apns-tokens`, and the sender
reads the table first and falls back to user-properties. Sends lazily backfill a row for any
user still on the fallback. Once that fallback is removed, `user-properties.apnsDeviceToken`
stops being read at all — see the comment at the write site in
`services/user/lambda/handlers/user.py`.
