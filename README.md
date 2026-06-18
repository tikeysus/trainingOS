# TrainingOS

TrainingOS is a local-first running intelligence platform. Durable facts,
derived metrics, retrieval documents, and presentation views are built from a
local SQLite data store rather than live vendor queries.

The initial code defines the vendor-neutral domain language, package
boundaries, and local SQLite foundation. See
[docs/architecture.md](docs/architecture.md),
[docs/domain-conventions.md](docs/domain-conventions.md),
[docs/analytics.md](docs/analytics.md),
[docs/dashboard.md](docs/dashboard.md),
[docs/coach.md](docs/coach.md), and
[docs/storage.md](docs/storage.md).

## Development

TrainingOS currently requires Python 3.12 or newer.

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

Set `TRAININGOS_DB_PATH` to override the default database at
`~/.local/share/trainingos/trainingos.sqlite3`.
Set `TRAININGOS_RAW_DATA_DIR` to override retained raw artifacts at
`~/.local/share/trainingos/raw`, and `TRAININGOS_LOCAL_TIMEZONE` to choose the
default IANA timezone for manual imports.

Local coach synthesis defaults to Ollama at `http://localhost:11434`. Override
`TRAININGOS_OLLAMA_CHAT_MODEL`, `TRAININGOS_OLLAMA_EMBEDDING_MODEL`, or
`TRAININGOS_AI_TIMEOUT_SECONDS` to change local provider behavior.

Initialize or update the configured database with:

```sh
PYTHONPATH=src python3 -m trainingos.storage
```

Run the local Grafana dashboard with:

```sh
sh scripts/run-grafana.sh
```

Run the local coach chat UI with:

```sh
PYTHONPATH=src python3 -m trainingos.coach_web
```

The dashboard includes an `Ask Local Coach` link to the default UI at
`http://localhost:8765`.

Import local FIT files with:

```sh
PYTHONPATH=src python3 -m trainingos.ingestion.fit_import /path/to/export
```
