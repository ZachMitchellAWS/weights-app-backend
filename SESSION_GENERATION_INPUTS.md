# Session Generation — Input Inventory

**Status: reference only.** Nothing here is built. This is a working document for deciding
*what data* a session-generation endpoint would need, ahead of deciding how it is called or
what it returns. Delete freely.

Scope is deliberately **inputs only**. Output shape and prompt wording are separate problems.

---

## The framing

We know vastly more about this data than a fresh endpoint does. A naive payload would dump
raw `lift_sets` rows and force the model to re-derive things we already compute exactly. So
the payload sends **conclusions, not raw material** — and once that principle is applied
consistently, most of the obvious fields fall away.

Three consequences:

1. **Effort levels are computed by us.** An effort level is `Epley(weight, reps)` compared
   against the estimated 1RM *standing at the moment that set was logged*. That requires
   interleaving sets with `estimated_1rms` history — the model cannot reconstruct it
   reliably, and would be subtly wrong if it tried.
2. **No weights or reps at all.** Once effort is classified, absolute load is redundant for
   plan selection. The only numbers that survive are e1RM values, and only where they mean
   something: the current value per lift, and the before/after of each progress set.
3. **No biological sex, no bodyweight.** These exist only to compute strength tier. Send the
   tier and how far through it the user is, and the raw inputs are unnecessary — the model
   gets the normalised conclusion without the arithmetic.

---

## Request

```jsonc
{
  "generated_at": "2026-08-17T14:32:00Z",

  // Broad account age: today | this_week | this_month | over_a_month | unknown.
  //
  // Exists because an empty 30-day window is ambiguous and the model was resolving it the
  // wrong way — an account created an hour ago got "today is effectively a fresh start,
  // there is no real training history". Nothing was missing; it could not exist yet.
  //
  // A bucket, not a date, on purpose: the model needs to know which story it is telling
  // (brand new vs returning after a gap), and an exact timestamp only invites arithmetic.
  "account_created": "today",
  "local_date": "2026-08-17",          // identifies which recent_training key is today
  "timezone": "America/Los_Angeles",
  "weight_unit": "lbs",

  "strength": {
    "overall_tier": "Intermediate",    // lowest of the five

    // Keyed by name so the model reads them as words; `id` carried inside so the
    // response can be resolved unambiguously. Five keys, fixed set.
    "lifts": {
      "Squats": {
        "id": "uuid",
        "current_e1rm": 315,
        "tier": "Intermediate",
        "tier_progress": 0.62            // 0–1 from this tier's floor to the next
      },
      "Bench Press":    { /* same shape */ },
      "Deadlifts":      { /* same shape */ },
      "Barbell Rows":   { /* same shape */ },
      "Overhead Press": { /* same shape */ }
    }
  },

  // 30 local calendar dates, oldest first. Every date present and every lift key
  // present, empty where nothing was logged — the gaps ARE the training rhythm.
  //
  // Keyed by date, not by session: an entry with five empty arrays is a rest day. The
  // container is the recent training period, and most days in it are empty.
  "recent_training": {
    "2026-07-19": {
      "Squats": [], "Bench Press": [], "Deadlifts": [],
      "Barbell Rows": [], "Overhead Press": []
    },

    "2026-08-14": {
      "Squats": [
        { "at": "2026-08-14T18:31:04-07:00", "effort": "easy" },
        { "at": "2026-08-14T18:36:22-07:00", "effort": "moderate" },
        { "at": "2026-08-14T18:42:10-07:00", "effort": "hard" },
        { "at": "2026-08-14T18:49:55-07:00", "effort": "progress",
          "e1rm_before": 310, "e1rm_after": 315 }
      ],
      "Bench Press": [
        { "at": "2026-08-14T19:02:41-07:00", "effort": "moderate" },
        { "at": "2026-08-14T19:08:19-07:00", "effort": "hard" }
      ],
      "Deadlifts": [], "Barbell Rows": [], "Overhead Press": []
    },

    "2026-08-17": {                      // today — see `local_date`
      "Squats": [
        { "at": "2026-08-17T07:14:02-07:00", "effort": "easy" }
      ],
      "Bench Press": [], "Deadlifts": [],
      "Barbell Rows": [], "Overhead Press": []
    }
  },

  // Per lift: is there genuinely nothing left to do today? THE authority on what the
  // model may skip — it is told not to substitute its own reading of "already trained".
  //
  // Exists because the intuitive reading is wrong. One baseline set from the strength-tier
  // journey made a lift look finished, which made the whole day look finished, and a user
  // who had logged five calibration sets was told "you're covered for today".
  //
  // covered = sets_today >= 6  OR  a non-baseline progress set landed today.
  // Baselines are excluded from the progress test because the first ever set for an
  // exercise classifies as `progress` almost by construction; they still count toward
  // sets_today, since the work was performed.
  "today_coverage": {
    "Squats":         { "covered": true,  "sets_today": 6, "progress_set_today": false },
    "Bench Press":    { "covered": false, "sets_today": 1, "progress_set_today": false },
    "Deadlifts":      { "covered": false, "sets_today": 0, "progress_set_today": false },
    "Barbell Rows":   { "covered": false, "sets_today": 0, "progress_set_today": false },
    "Overhead Press": { "covered": false, "sets_today": 0, "progress_set_today": false }
  },

  // How to SAY each date above. Same keys as `recent_training`, one label each.
  //
  // Exists because the model was writing "an easy set on 2026-08-14", which is not how
  // anyone refers to their own training. Resolved here rather than left to the model:
  // it is arithmetic against the user's local today, with exactly one right answer per
  // date, and models produce confidently wrong weekday names.
  //
  // Bands, by days before today: 0 "today" · 1 "yesterday" · 2-6 bare weekday ·
  // 7-13 "last <weekday>" · 14+ no date reference at all. Offset 7 is the same weekday
  // as today, so it takes the "last" prefix — that is what keeps every phrase in the
  // first two weeks unambiguous.
  "date_labels": {
    "2026-08-17": "today",
    "2026-08-16": "yesterday",
    "2026-08-14": "Friday",
    "2026-08-10": "last Monday",
    "2026-07-28": "more than two weeks ago"
  },

  // What the effort keys mean. Sent rather than assumed — see "Why definitions travel
  // with the payload". Bounds are percent of the estimated 1RM standing at the time,
  // min inclusive, max exclusive.
  "effort_level_definitions": {
    "easy":     { "min_percent_1rm": 0,  "max_percent_1rm": 70,
                  "description": "Well below working weight. Warm-up or recovery volume." },
    "moderate": { "min_percent_1rm": 70, "max_percent_1rm": 82,
                  "description": "Working weight. Repeatable without approaching failure." },
    "hard":     { "min_percent_1rm": 82, "max_percent_1rm": 92,
                  "description": "Demanding. Close to but short of a maximal effort." },
    "near_max": { "min_percent_1rm": 92, "max_percent_1rm": null,
                  "description": "At or near the current ceiling, without exceeding it." },
    "progress": { "min_percent_1rm": null, "max_percent_1rm": null,
                  "description": "NOT an intensity band. In recent_training: the set exceeded the standing estimated 1RM, so the ceiling moved. In a set plan sequence: attempt to exceed it." }
  },

  // The complete catalog as it exists on the client — 21 built-ins plus any the user
  // created. Sent from the frontend, so the backend never stores or versions it.
  "set_plan_catalog": [
    { "id": "uuid", "name": "Standard",
      "sequence": ["easy","easy","moderate","moderate","hard","progress"],
      "description": "Warm-up sets followed by a progress set" }
  ],

  "user_context": {
    "chips": ["Legs are sore"],
    "note": "left knee twinged on Tuesday"
  }
}
```

### A set

One element per set performed, in chronological order within its lift:

```jsonc
{ "at": "2026-08-12T18:42:10-07:00", "effort": "hard" }
{ "at": "2026-08-12T18:49:55-07:00", "effort": "progress", "e1rm_before": 310, "e1rm_after": 315 }
```

**`at` is ISO 8601 with the local UTC offset**, not a bare `Z` instant. Sets are bucketed by
*local* calendar date, so a bare UTC timestamp would contradict its own key — an 11pm set on
the 12th in California is `2026-08-13T06:00Z`, which reads as the wrong day. Carrying the
offset keeps the wall-clock time and the exact instant both recoverable, and keeps the
timestamp consistent with the date it is filed under.

Time of day is preserved because it is free once the timestamp is there, and it is the only
way to see that someone trains mornings, or that two sets an hour apart were not one session.

### Today

Today is simply the most recent key in `recent_training`, named by `local_date`. It is not duplicated
into a separate field — with dated buckets there is exactly one place any set can live, and a
second copy would be a second thing to keep in sync.

### Resolving the offset

Records carry `createdUtcOffsetSeconds` — the offset captured on device at write time,
in seconds east of UTC. It is optional, because records written before the field existed
have none, so readers need a fallback:

```python
def offset_seconds(row):
    if row.get("createdUtcOffsetSeconds") is not None:
        return int(row["createdUtcOffsetSeconds"])       # exact, captured on device
    tz = ZoneInfo(row.get("createdTimezone", "UTC"))     # requires tzdata in the layer
    return int(row["createdDatetime"].astimezone(tz).utcoffset().total_seconds())
```

`is not None`, never truthiness — `0` is a legal offset (UTC, London in winter).

The stored value is preferred because the derivation depends on whichever `tzdata` version
was bundled at the last `make build-layer`, and historical offset rules do get revised. The
device knew the real answer at the moment the set was logged.

Note the fallback still needs `tzdata` in the consuming service's `requirements.txt` — the
user service already does this; the checkin service does not yet.

### Why definitions travel with the payload

The thresholds live in `TrendsCalculator.IntensityBucket` on the client and could be retuned.
Sending them means the client stays the single source of truth and the prompt can never go
stale — the same reasoning as sending the set plan catalog. Putting them in a system prompt
instead would mean a threshold change requires a coordinated prompt edit, with nothing
enforcing that it happens.

Two things the definitions have to make explicit, because the words alone mislead:

**`progress` is an outcome, not an intensity.** Every other key is a band of percent-1RM.
`progress` means the estimated 1RM was exceeded — the ceiling moved. Read as a scale, a model
would slot it just above `near_max`, which is close but wrong: a progress set can land at a
*lower* percentage than near_max if the standing e1RM was stale. Nothing about the word
communicates that.

**The same vocabulary is used two ways.** In `recent_training` the keys are descriptive — what
happened. In `set_plan_catalog[*].sequence` they are prescriptive — what to aim for. A `hard`
in a plan is an instruction; a `hard` in recent_training is a measurement. The definitions say so
directly so the distinction is not left to inference.

> **Naming note.** These are payload-facing names, not storage keys. Internally the app
> persists `redline` for near-max and `pr` for progress. The payload uses the words the
> product says out loud. The serializer must map `redline → near_max` and `pr → progress`;
> do not let storage keys leak through.

### Progress sets

`e1rm_before` and `e1rm_after` appear only on progress sets, and with `current_e1rm` they are
the only load numbers in the entire payload. Together they are the strength trajectory —
everything else is shape and timing.

---

---

## Response

```jsonc
{
  "session": {
    "summary": "Squats and Bench are both due a progress set, and you have not pulled since Monday.",

    "items": [
      {
        "exercise_id": "uuid",          // MUST be one of `strength.lifts[*].id`
        "set_plan_id": "uuid",          // MUST be one of `set_plan_catalog[*].id`
        "rationale": "Last progress set landed 8 days ago at 315.",

        // Echoes, for logs and for a human reading a failed request. NOT authoritative —
        // the client resolves by id and ignores these.
        "exercise_name": "Squats",
        "set_plan_name": "Standard"
      }
    ]
  }
}
```

### Why ids, not names

Names are display strings; ids are identity. For the five fundamentals the distinction looks
academic — their names are canonical constants in the app. **Set plans are where it breaks.**
The catalog includes plans the user created, with names they chose and can edit. Two can
collide, and one can be renamed between a request and the response landing. Resolving a
returned `"Standard"` against a catalog by string is guessing; resolving a uuid is not.

Carrying `exercise_id` too costs one line and means the contract does not have to change when
this eventually covers accessories, whose names are fully user-editable.

Because the request carries the catalog and the client already holds it, **the response is
self-limiting**: an id the model invents resolves to nothing, and the client can reject the
whole response rather than silently dropping an item.

### What the response deliberately omits

**No target weights or reps.** The client computes those from `current_e1rm` and the user's
progress rep range once a plan is chosen. Keeping arithmetic out of the model removes the
largest single source of plausible-but-wrong output.

**No ordering guarantees beyond array order.** `items` is the session in the order it should
be performed.

**No set counts.** The chosen plan's `sequence` already defines how many sets and at what
efforts; restating it would create two sources of truth that can disagree.

---

## What earns its place, and why

- **The full set plan catalog, sent by the client.** The model cannot choose a plan without
  knowing which exist and what each one's effort sequence is. Sending it from the frontend
  means the backend never stores or versions the catalog, and the user's own custom plans are
  automatically in scope. It also constrains the output for free — see "Why ids, not names".
- **`tier` and `tier_progress` per lift.** Together they show *relative balance*: deep into
  Intermediate on squats, barely into it on bench. That is the signal for what deserves
  attention, and it carries it without exposing bodyweight or biological sex.
- **Explicit local date and timezone.** Everything is stored UTC with local correction.
  Without the local day, a 9pm session in California is ambiguous.
- **Every calendar date in `recent_training`, including empty ones.** Rest days are the training rhythm. Omitting
  them would force the model to infer absence from missing keys rather than read it.
- **`user_context` verbatim.** Whatever the user typed, unedited — it is the only input that
  describes today rather than the past.

## Deliberately excluded

| Excluded | Why |
|---|---|
| `limiting_lift` | Derivable — it is whichever entry in `strength.lifts` has the lowest tier |
| `movement_type` | Implicit for a fixed set of five named lifts. Becomes necessary again if this ever covers accessories |
| `days_since_last_set` / `days_since_last_progress_set` | Present in `recent_training`, which is complete and dated. See the note below on the one case this loses |
| Non-fundamental exercises | Scoped to the five for now; accessories add payload without changing what a session should be |
| Set plan per set | **We could not send this accurately even if we wanted to.** `activeSetPlanId` is a single current value on user-properties — there is no historical record of which plan was active when a past set was logged |
| Exercise UUIDs | The five are fixed and named; UUIDs add indirection with no benefit |
| Biological sex, bodyweight | Only inputs to tier; the tier itself is sent instead |
| Weights and reps | Redundant once effort is classified |
| Daily e1RM | Only progress sets move it meaningfully; those are sent explicitly |
| Progress rep range | Targets are computed app-side from e1RM once a plan is chosen |
| Birth date | Not collected, and tier does not use it |
| Per-set timestamps | Day-level ordering is sufficient |
| Deleted sets | Excluded everywhere else; stay consistent |
| Plate config, barbell weight, weight increment | Target weights are computed locally |
| Steps, protein, bodyweight target | Unrelated to lift selection |
| Entitlement status | The client already gated the call |

---

## One thing the removals cost

Dropping `days_since_last_progress_set` is fine for anything inside the window: the record is
complete and dated, so "when did they last hit a progress set" is readable straight off the
keys. The gap is **outside** it. A lift with no sets in 30 days renders as thirty empty arrays,
and that looks identical whether the last session was 31 days ago or 300. Those are different
situations — one is a lift being neglected, the other is a lift the user has effectively
abandoned — and nothing in the payload separates them.

Not worth adding a field back for on its own, but worth knowing before the first time a
generated session confidently prescribes something untouched since last year.

## Two places I would push back

**Progress rep range is a closer call than the rest.** You are right that the model does not
need it to *compute* anything — targets are app-side. But it changes what a progress set
*is*: a 3-rep range and a 10-rep range imply different plan characters, and some catalog
entries (Cluster Sets, Rest-Pause) suit one and not the other. It is one small object. I have
left it out per your call, but it is the first thing I would add back if plan choices come
out feeling mismatched to how the user actually trains.

**`current_e1rm` for accessories, not only the big five.** You scoped it to the fundamentals.
I have written it as present on any exercise that has one, because it costs a single number
and a user who trains an accessory seriously would otherwise have no load anchor at all.
Trivial to restrict later if it proves to be noise.

---

## Open questions

1. **Window length.** 30 days shows roughly four weekly cycles, enough to read a rhythm
   rather than a snapshot. Nothing now looks back further, so the window is a hard horizon —
   see "One thing the removals cost".

2. **All exercises, or only fundamentals?** All. The payload cost is trivial and excluding
   accessories would blind the generator to a user who trains them seriously. Output should
   still be constrained to a sensible count.

3. **Prior sessions and their outcomes.** Not included, because sessions are in-memory only
   today. Once persisted, "we prescribed Standard for squats on Tuesday and they completed
   4 of 6 sets" becomes the strongest available signal about whether prescriptions land.
   Worth shaping the payload so it can slot in later.

---

## Size

Thirty dates × five lift keys is ~150 objects even before any sets, most of them empty
arrays — call it 3 KB of scaffolding. Add real sets, five lift summaries and a 16-entry plan
catalog and it lands in the 5–10 KB range. Small enough that the explicit empty days are
worth their cost: they let the model *see* the training rhythm rather than reconstruct it.
