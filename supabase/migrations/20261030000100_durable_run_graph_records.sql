-- Versioned, workspace-scoped run graph metadata. Large bodies stay in private
-- artifact storage; payloads here contain bounded metadata and artifact refs.
create table public.eventlens_run_records (
    id uuid primary key,
    contract_version text not null check (contract_version = 'eventlens.run_record.v1'),
    owner_id text not null check (length(owner_id) between 1 and 200),
    workspace_id text not null check (length(workspace_id) between 1 and 200),
    run_id uuid not null,
    session_id uuid not null,
    kind text not null check (kind in (
        'transition', 'artifact', 'company_context', 'evidence_bundle',
        'external_agent_report', 'decision_packet', 'qa_result', 'outcome', 'postmortem'
    )),
    revision integer not null check (revision > 0),
    created_at timestamptz not null,
    status text not null check (length(status) between 1 and 80),
    checksum text not null check (checksum ~ '^[a-f0-9]{64}$'),
    artifact jsonb,
    metadata jsonb not null default '{}'::jsonb,
    constraint eventlens_run_records_revision_unique unique (workspace_id, run_id, kind, revision),
    constraint eventlens_run_records_metadata_bounded check (octet_length(metadata::text) <= 32768),
    constraint eventlens_run_records_artifact_required check (kind <> 'artifact' or artifact is not null)
);

create index eventlens_run_records_scope_idx
    on public.eventlens_run_records (owner_id, workspace_id, run_id, kind, revision);

create or replace function public.eventlens_run_records_append_only()
returns trigger language plpgsql as $$
begin
    raise exception 'durable run records are immutable; append a new revision';
end;
$$;

create trigger eventlens_run_records_append_only
before update or delete on public.eventlens_run_records
for each row execute function public.eventlens_run_records_append_only();

alter table public.eventlens_run_records enable row level security;
revoke all on table public.eventlens_run_records from public, anon, authenticated;
grant select, insert on table public.eventlens_run_records to service_role;
