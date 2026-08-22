-- Two roles, on purpose.
--
--   pms_owner  owns the schema and runs migrations
--   pms_app    is what the API connects as: it can read and write rows, but it
--              cannot alter a table, and it cannot drop a row-level security
--              policy. It must never be given BYPASSRLS or SUPERUSER — that
--              single attribute would undo every isolation guarantee below.
--
-- Run once as a superuser:  psql -f scripts/bootstrap_db.sql

\set owner_password  'owner-password-change-me'
\set app_password    'app-password-change-me'

CREATE ROLE pms_owner LOGIN PASSWORD :'owner_password' NOBYPASSRLS;
CREATE ROLE pms_app   LOGIN PASSWORD :'app_password'   NOBYPASSRLS NOCREATEDB NOCREATEROLE NOSUPERUSER;

CREATE DATABASE pms      OWNER pms_owner;
CREATE DATABASE pms_test OWNER pms_owner;

\connect pms
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO pms_app;
ALTER SCHEMA public OWNER TO pms_owner;
-- Rights on tables created later by the owner.
ALTER DEFAULT PRIVILEGES FOR ROLE pms_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO pms_app;
ALTER DEFAULT PRIVILEGES FOR ROLE pms_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO pms_app;
-- The application never creates objects.
REVOKE CREATE ON SCHEMA public FROM pms_app;

\connect pms_test
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO pms_app;
ALTER SCHEMA public OWNER TO pms_owner;
ALTER DEFAULT PRIVILEGES FOR ROLE pms_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO pms_app;
ALTER DEFAULT PRIVILEGES FOR ROLE pms_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO pms_app;
REVOKE CREATE ON SCHEMA public FROM pms_app;
