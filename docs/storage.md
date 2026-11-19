# Durable storage

EventLens requires each deployed product instance to use its owner's Supabase
project as the durable source of truth. SQLite is local-only for demos and
tests; it is key-free and does not provide shared or deployed persistence.
Configure the server process with `EVENTLENS_MEMORY_BACKEND=supabase`, the project's
`EVENTLENS_SUPABASE_URL`, and a server-only `EVENTLENS_SUPABASE_SECRET_KEY`.
The secret key is never sent to browser code. Apply checked-in migrations to
the user's own project; this repository contains no shared project credentials.

The versioned repository contract `eventlens.run_record.v1` stores
workspace-scoped immutable revisions for transitions, artifact index entries,
company context, evidence bundles, external-agent reports, decision packets,
QA results, outcomes, and postmortems. Each row carries its owner, workspace,
run and session IDs, status, revision, checksum, bounded metadata, and optional
artifact reference. A new revision is appended; existing revisions cannot be
updated or deleted. Reads require the workspace and run IDs.

Raw graph checkpoints, prompts, and large evidence or report bodies are not
stored in these records. Store permitted bodies in checksum-addressed private
artifact storage and persist only the storage key, media type, size, and
checksum. In the Phase 3 implementation that artifact store is the
server-owned local directory. Supabase therefore owns the complete typed run
graph, but a different machine also needs the private artifact directory to
open bundle/report bodies. A shared private object-store adapter is deferred;
the application does not create a writable public Supabase bucket. Supabase RLS
is enabled on the run-record table with no `anon` or
`authenticated` policies or grants; backend access uses the server-only secret.

Run local adapter contract and migration checks without Docker or remote
writes:

```bash
.venv/bin/python -m pytest -q tests/memory/test_run_records.py tests/persistence/test_memory_schema.py
```
