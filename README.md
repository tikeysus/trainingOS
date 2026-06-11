# TrainingOS

TrainingOS is a local-first running intelligence platform. Durable facts,
derived metrics, retrieval documents, and presentation views are built from a
local SQLite data store rather than live vendor queries.

The initial code defines the vendor-neutral domain language, package
boundaries, and local SQLite foundation. See
[docs/architecture.md](docs/architecture.md),
[docs/domain-conventions.md](docs/domain-conventions.md), and
[docs/storage.md](docs/storage.md).

## Development

TrainingOS currently requires Python 3.12 or newer and has no runtime
dependencies.

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

Set `TRAININGOS_DB_PATH` to override the default database at
`~/.local/share/trainingos/trainingos.sqlite3`.

Initialize or update the configured database with:

```sh
PYTHONPATH=src python3 -m trainingos.storage
```
