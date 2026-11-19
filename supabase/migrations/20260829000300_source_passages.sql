-- Deterministic source passages for evidence links and future retrieval.

create table public.source_passages (
  id uuid primary key default gen_random_uuid(),
  source_id uuid not null references public.sources (id) on delete restrict,
  passage_id text not null,
  ordinal integer not null check (ordinal >= 0),
  text text not null,
  text_sha256 text not null check (text_sha256 ~ '^[a-f0-9]{64}$'),
  char_start integer not null check (char_start >= 0),
  char_end integer not null check (char_end >= char_start),
  token_estimate integer not null check (token_estimate >= 0),
  extraction_version text not null,
  created_at timestamptz not null default now(),
  constraint source_passages_passage_not_blank check (btrim(passage_id) <> ''),
  constraint source_passages_text_not_blank check (btrim(text) <> ''),
  constraint source_passages_extraction_version_not_blank check (btrim(extraction_version) <> ''),
  constraint source_passages_source_passage_unique unique (source_id, passage_id)
);

create index source_passages_source_ordinal_idx
  on public.source_passages (source_id, ordinal);
create index source_passages_text_sha256_idx on public.source_passages (text_sha256);

create trigger source_passages_append_only
before update or delete on public.source_passages
for each row execute function public.eventlens_reject_update_delete();

alter table public.source_passages enable row level security;

grant select, insert, update, delete on table public.source_passages to service_role;

create or replace function public.eventlens_relation_sizes()
returns table (
  relation_name text,
  row_estimate bigint,
  table_bytes bigint,
  index_bytes bigint,
  total_bytes bigint
)
language sql
security definer
set search_path = pg_catalog, public
as $$
  select
    stats.relname::text,
    stats.n_live_tup::bigint,
    pg_table_size(format('%I.%I', stats.schemaname, stats.relname)::regclass)::bigint,
    pg_indexes_size(format('%I.%I', stats.schemaname, stats.relname)::regclass)::bigint,
    pg_total_relation_size(format('%I.%I', stats.schemaname, stats.relname)::regclass)::bigint
  from pg_stat_user_tables stats
  where stats.schemaname = 'public'
    and stats.relname in (
      'companies', 'sources', 'source_passages', 'earnings_events', 'event_sources',
      'event_metrics', 'market_setups', 'market_setup_sources',
      'market_reactions', 'market_reaction_sources', 'research_sessions',
      'hypotheses', 'pre_event_snapshots', 'pre_event_snapshot_evidence',
      'pre_event_snapshot_metrics', 'hypothesis_assessments',
      'hypothesis_assessment_evidence', 'event_context_cards',
      'context_card_evidence'
    )
  order by stats.relname;
$$;

revoke all on function public.eventlens_relation_sizes()
  from public, anon, authenticated;
grant execute on function public.eventlens_relation_sizes() to service_role;
