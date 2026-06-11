# TrainingOS

TrainingOS is a local-first running intelligence platform. Durable facts,
derived metrics, retrieval documents, and presentation views are built from a
local SQLite data store rather than live vendor queries.

The initial code defines the vendor-neutral domain language and package
boundaries. See [docs/architecture.md](docs/architecture.md) and
[docs/domain-conventions.md](docs/domain-conventions.md).

## Development

TrainingOS currently requires Python 3.12 or newer and has no runtime
dependencies.

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```
