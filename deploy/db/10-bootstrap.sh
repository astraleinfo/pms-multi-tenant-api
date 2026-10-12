#!/bin/sh
# First start of the db container in docker-compose.yml: runs
# scripts/bootstrap_db.sql with the passwords from .env instead of the
# placeholders the file sets for local development.

: "${PMS_OWNER_PASSWORD:?set PMS_OWNER_PASSWORD in .env}"
: "${PMS_APP_PASSWORD:?set PMS_APP_PASSWORD in .env}"

# The file's own \set lines would override -v, so they go.
sed '/^\\set /d' /bootstrap/bootstrap_db.sql \
  | psql -v ON_ERROR_STOP=1 \
         --username "${POSTGRES_USER:-postgres}" --dbname postgres \
         -v owner_password="$PMS_OWNER_PASSWORD" \
         -v app_password="$PMS_APP_PASSWORD"
