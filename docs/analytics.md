# Analytics formulas

TrainingOS analytics are deterministic local computations. They read normalized
SQLite records and write computed evidence before dashboard or coach requests.
LLMs may explain these values, but they do not originate numeric metrics.

## Weekly summary v1

Method: `weekly_summary` `1.0.0`.

- Window: Monday through Sunday in the requested IANA timezone.
- Inputs: normalized `run` activities whose local start date falls in the
  week.
- Outputs: run count, total duration in seconds, total distance in metres,
  longest run in metres, average pace in seconds per kilometre, and data gaps.
- Exclusions: non-run activities.
- Missing distance: total distance uses only measured distances and the summary
  records `one_or_more_runs_missing_distance`; distance-dependent metric
  evidence is not emitted unless all runs in the week have distance.

## Metric formulas v1

- `weekly_run_count` `1.0.0`: count of run activities in the week, unit
  `count`.
- `weekly_duration` `1.0.0`: sum of run durations, unit `s`.
- `weekly_distance` `1.0.0`: sum of run distances, unit `m`; emitted only when
  every run has distance.
- `weekly_long_run` `1.0.0`: maximum run distance in the week, unit `m`;
  emitted only when every run has distance.
- `weekly_average_pace` `1.0.0`: total duration divided by total kilometres,
  unit `s/km`; emitted only when every run has distance and total distance is
  positive.
- `training_load_trend` `1.0.0`: current weekly distance divided by average
  distance from up to four prior complete weeks, unit `ratio`. Weeks with
  missing distance are excluded; a caveat is added until four complete prior
  weeks are available.
- `marathon_pace_volume` `1.0.0`: total lap distance within plus or minus 5%
  of 3:10 marathon pace, unit `m`. The v1 target pace is 11,400 seconds over
  42,195 metres. This metric requires lap distance and duration.
- `aerobic_efficiency` `1.0.0`: mean of activity speed in metres per second
  divided by average sampled heart rate in bpm, unit `ratio`. Quality is the
  share of weekly runs with usable distance, duration, and heart-rate samples.
- `heart_rate_drift` `1.0.0`: percent change from first-half to second-half
  average heart rate for an activity, unit `%`. Requires at least four ordered
  heart-rate samples.
- `recovery_coverage` `1.0.0`: share of days in the week with at least one
  measured sleep, HRV, or resting-heart-rate metric, unit `ratio`.

## Provenance and caveats

Each computed record stores method name, method version, UTC computation time,
evidence record IDs, and caveats. Low-quality or incomplete inputs reduce
quality and add caveats instead of producing unsupported precision.

## Race projection contract

`RaceProjection` is the public contract for future projection formulas. It
requires a method version, target race ID, status, evidence record IDs, caveats,
and either a positive projected duration with non-negative uncertainty or an
explicit insufficient-data status with caveats. Projection formulas must use
stored local evidence and persist their method version before presentation.
