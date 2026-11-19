# Supabase persistence for deployments

SQLite is for local demonstrations and tests. Every deployed EventLens instance
uses a Supabase project owned by that deployment's user or organization for
durable state. Each deployment configures its own project and identifiers; this
repository contains no shared project identifiers or credentials.

## Configure a user-owned project

1. Create a Supabase project under an account you control.
2. Apply the checked-in migrations using the Supabase CLI. Review each migration
   before applying it to a hosted project.
3. Set `EVENTLENS_MEMORY_BACKEND=supabase` and provide the project URL and a
   server-side credential through your deployment's secret manager or local
   ignored `.env` file. Set `EVENTLENS_MEMORY_OWNER_ID` and
   `EVENTLENS_MEMORY_WORKSPACE_ID` to stable identifiers owned by that
   deployment. Example values are in `.env.example`.
4. Keep all credentials in server-side configuration. Never place them in the
   browser, source control, exported evidence bundles, or support messages.

The application reads environment configuration through its settings layer;
do not source `.env` as shell code. The application does not provision a
project, migrate a hosted database, or select a shared deployment for you.

The checked-in Phase 3 adapter persists immutable typed run/session revisions
in Supabase. Checksum-addressed evidence, bundle, and report bodies remain in
the server-owned artifact directory; copy or mount that private directory when
moving the deployment to another machine. Supabase-only body recovery is not
claimed, and no browser or public role receives write access.

Schema changes belong in `supabase/migrations/`. Review and test migrations
against a project you own before applying them. The hosted database remains
under the project owner's control.

Official references:

- <https://supabase.com/docs/guides/getting-started/api-keys>
- <https://supabase.com/docs/guides/local-development/cli/getting-started>
- <https://supabase.com/docs/guides/deployment/database-migrations>
- <https://supabase.com/docs/guides/platform/free-project-pausing>
- <https://supabase.com/docs/guides/platform/database-size>
