# Domain conventions

## Identifiers and provenance

Every durable entity has `RecordMetadata` containing:

- a stable local `record_id`
- an IANA `timezone`
- zero or more `SourceReference` values
- `created_at` and `updated_at` audit timestamps
- optional `Provenance`

`SourceReference` records the replaceable source name, its external ID, the
UTC sync time, and an optional opaque raw-data reference. Local user-created
records do not need an external ID. Reconciled records can retain multiple
source references.

`Provenance` identifies whether a record was observed, imported, entered,
computed, or model-interpreted. Computed records also identify a versioned
method and the local evidence records used as inputs.

## Time

- All `datetime` values must be timezone-aware.
- Domain models normalize instants to UTC at construction.
- `RecordMetadata.timezone` stores the IANA timezone needed to recover local
  calendar meaning, including daylight-saving rules.
- Calendar-only concepts such as health days and training-block boundaries use
  `date`, interpreted in the record's IANA timezone.
- Naive datetimes and fixed-offset strings masquerading as timezone names are
  rejected.

## Units

Numeric measurements use `Measurement` and an explicit `Unit`; bare numbers
are not used for quantities whose unit can vary. Canonical storage units are:

| Dimension | Canonical unit |
| --- | --- |
| Distance | metre |
| Duration | second |
| Speed | metre per second |
| Pace | second per kilometre |
| Heart rate | beat per minute |
| Temperature | degree Celsius |
| Elevation | metre |
| Ratio and percentage | ratio, percent |

Adapters are responsible for converting source values into canonical units.
The original unit remains available in retained raw source data.

## Model interpretation

Observed facts, deterministic computations, and model interpretation are
different provenance kinds. AI providers may explain stored metrics but never
originate numeric training facts.
