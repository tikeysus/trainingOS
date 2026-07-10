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

Coach queries use Claude API. Set your API key in the environment:

```sh
export ANTHROPIC_API_KEY=sk-ant-...
```

Optionally select a specific Claude model (defaults to latest):

```sh
export CLAUDE_MODEL=claude-opus-4-1
```

The coach will also accept per-query model selection via the `/api/coach` endpoint.

Initialize or update the configured database with:

```sh
PYTHONPATH=src python3 -m trainingos.storage
```

Refresh derived metrics and coach retrieval documents after imports with:

```sh
PYTHONPATH=src python3 -m trainingos.refresh
```

Run the local Grafana dashboard with:

```sh
sh scripts/run-grafana.sh
```

Run the local coach chat UI with:

```sh
PYTHONPATH=src python3 -m trainingos.coach_web
```

Check coach database/provider status with:

```sh
curl http://localhost:8765/api/health
```

The dashboard includes an `Ask Local Coach` link to the default UI at
`http://localhost:8765`.

Import local FIT files, directories, or Garmin export zips with:

```sh
PYTHONPATH=src python3 -m trainingos.ingestion.fit_import /path/to/file-dir-or-export.zip
```

Successful manual FIT imports refresh derived metrics and retrieval documents
for the same configured database.

## Daily Garmin Sync

Set credentials in your environment and run the daily sync pipeline manually:

```sh
export TRAININGOS_GARMIN_EMAIL=you@example.com
export TRAININGOS_GARMIN_PASSWORD=yourpassword
PYTHONPATH=src python3 -m trainingos.ingestion.daily_sync
```

The pipeline runs migrations, syncs new activities from Garmin Connect,
regenerates derived metrics and retrieval documents, and copies the database
to the Grafana runtime path if available. Exit code 0 on success, 1 on failure.

### Schedule with launchd (macOS)

Create `~/Library/LaunchAgents/com.trainingos.daily-sync.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.trainingos.daily-sync</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>-m</string>
    <string>trainingos.ingestion.daily_sync</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key>
    <string>/path/to/trainingOS/src</string>
    <key>TRAININGOS_GARMIN_EMAIL</key>
    <string>you@example.com</string>
    <key>TRAININGOS_GARMIN_PASSWORD</key>
    <string>yourpassword</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>6</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/tmp/trainingos-daily-sync.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/trainingos-daily-sync.log</string>
</dict>
</plist>
```

Load it with:

```sh
launchctl load ~/Library/LaunchAgents/com.trainingos.daily-sync.plist
```

### Schedule with cron

```sh
# Run at 06:00 daily — edit with: crontab -e
0 6 * * * TRAININGOS_GARMIN_EMAIL=you@example.com TRAININGOS_GARMIN_PASSWORD=yourpassword PYTHONPATH=/path/to/trainingOS/src /usr/bin/python3 -m trainingos.ingestion.daily_sync >> /tmp/trainingos-daily-sync.log 2>&1
```
