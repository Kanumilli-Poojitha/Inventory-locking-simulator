#!/usr/bin/env bash
PG_USER="user"
PG_DB="inventory_db"

while true; do
  echo "--- Active Locks at $(date) ---"
  docker exec -it $(docker ps --filter "ancestor=postgres:15" --format "{{.ID}}" | head -n 1) \
    psql -U $PG_USER -d $PG_DB -c "SELECT pid, relation::regclass, locktype, mode, granted FROM pg_locks WHERE pid IN (SELECT pid FROM pg_stat_activity WHERE datname = '$PG_DB');"
  sleep 2
done
