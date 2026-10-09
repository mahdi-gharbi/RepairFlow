#!/bin/sh
# Creates one database + role per component. Runs once, on an empty data volume.
set -eu

create() {
  db="$1"; user="$2"; pass="$3"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
CREATE ROLE "$user" LOGIN PASSWORD '$pass';
CREATE DATABASE "$db" OWNER "$user";
REVOKE ALL ON DATABASE "$db" FROM PUBLIC;
SQL
}

create repairflow repairflow "$RF_DB_PASSWORD"
create n8n n8n "$N8N_DB_PASSWORD"
create tickets tickets "$TICKETS_DB_PASSWORD"
