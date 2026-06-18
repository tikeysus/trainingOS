# Local dashboard

TrainingOS ships a local Grafana dashboard backed by the SQLite database. The
dashboard reads only persisted local tables and reporting views. It does not
call Garmin, Strava, weather services, or AI providers.

## Prepare the database

Apply migrations to the database you want Grafana to read:

```sh
mkdir -p var
export TRAININGOS_DB_PATH="$PWD/var/trainingos.sqlite3"
PYTHONPATH=src python3 -m trainingos.storage
```

Run ingestion and metric/retrieval generation before opening the dashboard.
Expensive calculations are expected to be persisted before request time.

## Run Grafana

The Grafana launcher provisions:

- the `frser-sqlite-datasource` Grafana datasource plugin.
- a read-only SQLite datasource at `/var/lib/trainingos/trainingos.sqlite3`.
- the TrainingOS dashboard from `grafana/dashboards`.

```sh
export TRAININGOS_DB_PATH="$PWD/var/trainingos.sqlite3"
sh scripts/run-grafana.sh
```

The launcher uses `docker compose up grafana` when the Docker Compose plugin is
available, falls back to standalone `docker-compose`, and otherwise runs the
same Grafana container with `docker run`.
Before starting Grafana, it copies the provisioned dashboard files and a
read-only snapshot of the configured SQLite database into
`/private/tmp/trainingos-grafana-runtime`. This avoids macOS container-runtime
permission failures when the project lives under `~/Documents`.

The dashboard includes an `Ask Local Coach` link to `http://localhost:8765`.
Start the local coach UI separately with:

```sh
export TRAININGOS_DB_PATH="$PWD/var/trainingos.sqlite3"
PYTHONPATH=src python3 -m trainingos.coach_web
```

Grafana still does not call the AI provider. The linked coach UI owns the local
Ollama request after it retrieves bounded evidence from SQLite.

Grafana listens on `http://localhost:3000` by default. Set
`TRAININGOS_GRAFANA_PORT` to use a different host port.

The default local admin credentials are `admin` / `admin`. Override them with
`TRAININGOS_GRAFANA_ADMIN_USER` and `TRAININGOS_GRAFANA_ADMIN_PASSWORD` for
local use. Do not commit credentials or local database files.

## Dashboard data model

Panels query `dashboard_*` reporting views instead of raw source payloads or
vendor-specific tables. The views expose:

- weekly training volume, duration, long run, pace, gaps, and formula versions.
- derived metric time series with units, quality, method versions, caveats, and
  evidence ids.
- current versus prior training block comparison.
- retrieval-document evidence, including caveats and stale status.
- context annotations for notes, races, and training blocks.
- race projection status as explicitly unavailable until projection evidence is
  persisted.

Missing values remain missing. The dashboard does not convert absent recovery,
weather, projection, or prior-block data to zero.
