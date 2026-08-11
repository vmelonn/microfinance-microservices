#!/bin/bash
# Creates one database per service inside the single dev Postgres container.
#
# Database-per-service is about OWNERSHIP, who may migrate a schema, who
# breaks when a column changes, not about how many Postgres processes are
# running. In the cluster these are separate StatefulSets; in compose one
# server with two databases gives the same isolation guarantees for a
# fraction of the memory.
#
# Runs only on FIRST start, when the data volume is empty. If you add a
# database here later, you must `docker compose down -v` for it to take.

set -euo pipefail

if [ -z "${POSTGRES_MULTIPLE_DATABASES:-}" ]; then
    echo "POSTGRES_MULTIPLE_DATABASES is unset, nothing to create."
    exit 0
fi

for db in $(echo "$POSTGRES_MULTIPLE_DATABASES" | tr ',' ' '); do
    echo "  creating database '$db'"
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
        CREATE DATABASE "$db";
        GRANT ALL PRIVILEGES ON DATABASE "$db" TO "$POSTGRES_USER";
EOSQL
done

echo "databases ready: $POSTGRES_MULTIPLE_DATABASES"
