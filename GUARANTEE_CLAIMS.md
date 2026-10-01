# The New-Best Guarantee — handling a claim

> *"Log 3 sessions a week for 8 weeks. No new best? We add 3 months free."*
> — the guarantee card on the first-week paywall.

Claims are handled **by hand**. There is no detection job, no automated fulfilment, and
deliberately so — see "Why manual" at the end.

---

## The four steps

### 1. Verify the claim

Both conditions must hold:

- **≥3 distinct training dates in each of 8 consecutive weeks.** Distinct *dates*, not sets —
  three sets in one evening is one session.
- **No increase in `estimated-1rm` across that window.** The table stores a running max, so any
  increase at all disqualifies the claim.

`scripts/plot_user_lift_timelines.py --env production --per-user` renders both, or query
`lift-sets` and `estimated-1rm` for the user directly.

If they fall short, say so plainly and offer something else. Do not argue the edge case — the
goodwill is worth more than the 90 days.

### 2. Tell them to turn off auto-renew — **do not skip this**

This is the step that is easy to miss and ruins the remedy. If auto-renew stays on, Apple bills
them on schedule and the comp period overlaps with time they have already paid for. They end up
with nothing.

> Turn off auto-renew in Settings → Apple ID → Subscriptions. Your paid access continues to
> *[end date]*, and the 3 free months start from then.

Their end date is the `ends` column in `scripts/report_subscription_status.py`.

### 3. Write the comp grant

One row in `liftthebull-production-entitlement-grants`. **`startUtc` is the moment their paid
access ends**, so the free months land *after* what they paid for instead of on top of it.

```bash
aws dynamodb put-item --region us-west-1 \
  --table-name liftthebull-production-entitlement-grants \
  --item '{
    "userId":                {"S": "<userId>"},
    "startUtc":              {"S": "2027-04-22T00:00:00Z"},
    "endUtc":                {"S": "2027-07-21T00:00:00Z"},
    "entitlementName":       {"S": "com.weightapp.premium.comp.guarantee"},
    "paymentPlatformSource": {"S": "comp"},
    "compReason":            {"S": "new-best-guarantee"},
    "createdDatetime":       {"S": "<today>"},
    "lastModifiedDatetime":  {"S": "<today>"}
  }'
```

Three things that matter, and only three:

- **`entitlementName` MUST start with `com.weightapp.premium`.** That prefix is the entire
  premium check — `EntitlementGrant.isPremium` in the iOS app tests
  `entitlementName.hasPrefix("com.weightapp.premium") && isActive` and nothing else. Nothing
  validates the remainder against real product ids, so `.comp.guarantee` is safe and
  self-documenting.
- **`startUtc` is the sort key**, so it must be unique for that user. Using their paid end date
  gives uniqueness for free.
- **`endUtc` = `startUtc` + 90 days.**

### 4. Tell them it is live

Nothing else is needed. `GET /entitlements` reads this table directly and the app replaces its
local copy on the next sync — app launch or foreground.

---

## Why this works at all

The app decides premium from **our** table, not from Apple:

- `_get_active_entitlements` (`services/entitlements/lambda/handlers/entitlements.py`) queries
  `entitlement-grants` on the `userId-endUtc-index` GSI for `endUtc > now`. It never re-derives
  from StoreKit.
- Grant creation is a conditional put on
  `attribute_not_exists(userId) AND attribute_not_exists(startUtc)` — **additive only**. Nothing
  reconciles or deletes, so an Apple sync cannot remove a comp row.

What this does **not** do is stop Apple charging anyone. That is the whole reason step 2 exists.
Only Apple can move a billing date; offer codes or promotional offers would be the route, and
both are far more machinery than the claim rate justifies.

---

## Two things to remember when you do this

**It will skew the subscription report.** A 90-day grant is `>= PAID_MIN_DAYS`, so
`scripts/report_subscription_status.py` classifies it as a paying customer — inflating
conversion with someone who paid nothing. Either exclude `paymentPlatformSource == "comp"`
there, or subtract it by hand. Worth fixing the script the *first* time a comp is issued rather
than the third.

**Nothing revokes it.** There is no expiry logic beyond `endUtc`. That is the intent, but it
also means a typo in `endUtc` grants premium for exactly as long as was typed.

---

## Why manual

Measured 2026-09-22, against the whole production base:

| | |
|---|---|
| Accounts old enough to have qualified (≥8 weeks) | 66 |
| Of those, logged any set at all | 24 |
| Have **ever** hit 3 sessions/week for 8 consecutive weeks | **2** |

Both of those two are internal — the owner's account and the App Store review account. **No
real user has ever reached the first half of the bar**, and the second half is rarer still:
someone training three times a week for two months almost always sets a new best, especially a
beginner.

So the realistic claim rate is near zero, and building detection plus an Apple offer-code flow
would be days of work serving nobody.

**Revisit when the numbers change.** If two genuine claims ever arrive, that is the signal to
automate — both the 8-week detection and a real Apple-side remedy.
