# Architecture

TrainingOS follows this data flow:

```text
sources -> nightly sync -> SQLite -> derived metrics -> retrieval -> UI/coach
```

## Package boundaries

| Package | Owns | May depend on |
| --- | --- | --- |
| `domain` | Vendor-neutral entities, identifiers, units, timestamps, provenance | Python standard library only |
| `storage` | SQLite connections, schema, and ordered migrations | Python standard library only |
| `ingestion` | Source adapters, raw payload retention, sync cursors | `domain` |
| `normalization` | Conversion from source records to canonical entities | `domain`, adapter contracts from `ingestion` |
| `analytics` | Deterministic, versioned metric calculations | `domain` |
| `retrieval` | Local evidence documents and retrieval contracts | `domain` |
| `providers` | LLM and embedding adapter contracts/implementations | `domain`, `retrieval` |
| `presentation` | Dashboard and coach-facing application services | `domain`, `analytics`, `retrieval`, provider contracts |

Dependencies flow down this table. The domain package must not import from any
other TrainingOS package. Source SDK types stop at ingestion and normalization
boundaries. Analytics must not call providers, and presentation must not call
source services for stored facts.

SQLite persistence will implement repositories around domain entities rather
than becoming part of those entities. This keeps schema and adapter concerns
replaceable while preserving a stable domain language.

The `storage` package owns SQLite mechanics and may not shape domain models.
Feature packages can depend on its connection and future repository contracts,
but the `domain` package remains independent of persistence.

## Source adapter boundary

Garmin is the first intended rich-data adapter, but it is not represented in
the domain model. An adapter translates Garmin identifiers and payloads into:

- `SourceReference` for source identity and sync audit data
- canonical activity, health, weather, and context entities
- retained raw data references

Manual FIT and future source adapters use the same boundary. A canonical
record may have multiple source references when records are reconciled.

## Durable data rules

- Raw source data is retained when practical.
- Normalized records carry stable local identifiers and source references.
- Derived records carry a versioned method and evidence references.
- Expensive analytics are computed during sync and persisted.
- Queries for stored facts remain local and do not require a source or model
  provider.
