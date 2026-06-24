#!/usr/bin/env sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"

database_path=${TRAININGOS_DB_PATH:-./var/trainingos.sqlite3}
grafana_port=${TRAININGOS_GRAFANA_PORT:-3000}
admin_user=${TRAININGOS_GRAFANA_ADMIN_USER:-admin}
admin_password=${TRAININGOS_GRAFANA_ADMIN_PASSWORD:-admin}
runtime_dir=${TRAININGOS_GRAFANA_RUNTIME_DIR:-/private/tmp/trainingos-grafana-runtime}
detach=${TRAININGOS_GRAFANA_DETACH:-0}

case "$database_path" in
    /*) database_mount_path=$database_path ;;
    *) database_mount_path=$repo_root/$database_path ;;
esac

if [ ! -f "$database_mount_path" ]; then
    printf '%s\n' "TrainingOS database not found at $database_mount_path" >&2
    printf '%s\n' "Create it first with: TRAININGOS_DB_PATH=$database_path PYTHONPATH=src python3 -m trainingos.storage" >&2
    exit 2
fi

provisioning_dir=$runtime_dir/provisioning
dashboards_dir=$runtime_dir/dashboards
runtime_database_path=$runtime_dir/trainingos.sqlite3

rm -rf "$provisioning_dir" "$dashboards_dir"
mkdir -p "$provisioning_dir" "$dashboards_dir"
cp -R "$repo_root/grafana/provisioning/." "$provisioning_dir/"
cp -R "$repo_root/grafana/dashboards/." "$dashboards_dir/"
mkdir -p "$provisioning_dir/alerting" "$provisioning_dir/plugins"
cp "$database_mount_path" "$runtime_database_path"

export TRAININGOS_GRAFANA_PROVISIONING_DIR=$provisioning_dir
export TRAININGOS_GRAFANA_DASHBOARDS_DIR=$dashboards_dir
export TRAININGOS_GRAFANA_DB_PATH=$runtime_database_path
export TRAININGOS_GRAFANA_PORT=$grafana_port
export TRAININGOS_GRAFANA_ADMIN_USER=$admin_user
export TRAININGOS_GRAFANA_ADMIN_PASSWORD=$admin_password

if [ "$detach" = "1" ]; then
    compose_detach_flag=-d
    docker_detach_flag=--detach
else
    compose_detach_flag=
    docker_detach_flag=
fi

if docker compose version >/dev/null 2>&1; then
    exec docker compose up --force-recreate $compose_detach_flag grafana
fi

if command -v docker-compose >/dev/null 2>&1; then
    exec docker-compose up --force-recreate $compose_detach_flag grafana
fi

docker rm -f trainingos-grafana >/dev/null 2>&1 || true
exec docker run --rm \
    --name trainingos-grafana \
    $docker_detach_flag \
    --publish "$grafana_port:3000" \
    --env GF_INSTALL_PLUGINS=frser-sqlite-datasource \
    --env GF_SECURITY_ADMIN_USER="$admin_user" \
    --env GF_SECURITY_ADMIN_PASSWORD="$admin_password" \
    --env GF_USERS_DEFAULT_THEME=dark \
    --env GF_PANELS_DISABLE_SANITIZE_HTML=true \
    --volume "$provisioning_dir:/etc/grafana/provisioning:ro" \
    --volume "$dashboards_dir:/var/lib/grafana/dashboards:ro" \
    --volume "$runtime_database_path:/var/lib/trainingos/trainingos.sqlite3:ro" \
    grafana/grafana-oss:11.5.2
