-- EventLens Milestone 1: durable research memory.
--
-- The application uses a backend-only Supabase secret for this milestone.
-- RLS is enabled on every table and this migration intentionally creates no
-- policies, so anon/authenticated clients cannot access the research ledger.

create extension if not exists pgcrypto with schema extensions;

-- Storage is optional for the backend-only MVP.  Some hosted Postgres
-- projects do not expose Supabase's managed storage schema; keep the core
-- ledger deployable there and use the content-addressed artifact reference
-- without requiring an object bucket.
do $$
begin
  if to_regclass('storage.buckets') is not null then
    execute $sql$
      insert into storage.buckets (id, name, public)
      values ('eventlens-evidence', 'eventlens-evidence', false)
      on conflict (id) do update set public = false
    $sql$;
  end if;
end
$$;

create or replace function public.eventlens_set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  -- timestamptz is stored as an absolute instant; do not convert it to a
  -- timezone-less timestamp before assignment.
  new.updated_at = statement_timestamp();
  return new;
end;
$$;

create table public.companies (
  id uuid primary key default gen_random_uuid(),
  ticker text not null,
  exchange text not null,
  market text not null check (market in ('US', 'HK', 'OTHER')),
  legal_name text not null,
  cik text,
  lei text,
  primary_currency text not null check (primary_currency ~ '^[A-Z]{3}$'),
  active boolean not null default true,
  schema_version text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint companies_ticker_not_blank check (btrim(ticker) <> ''),
  constraint companies_exchange_not_blank check (btrim(exchange) <> ''),
  constraint companies_legal_name_not_blank check (btrim(legal_name) <> ''),
  constraint companies_schema_version_not_blank check (btrim(schema_version) <> ''),
  constraint companies_exchange_ticker_unique unique (exchange, ticker)
);

create index companies_ticker_idx on public.companies (ticker);

create table public.sources (
  id uuid primary key default gen_random_uuid(),
  provider text not null,
  external_source_id text,
  source_type text not null check (
    source_type in (
      'sec_filing', 'issuer_release', 'issuer_transcript', 'market_data',
      'news', 'analyst_research', 'fixture', 'other'
    )
  ),
  title text not null,
  canonical_url text,
  published_at timestamptz,
  available_at timestamptz not null,
  retrieved_at timestamptz not null,
  content_sha256 text not null check (content_sha256 ~ '^[a-f0-9]{64}$'),
  artifact_reference text not null,
  size_bytes bigint not null check (size_bytes >= 0),
  media_type text not null,
  data_kind text not null check (data_kind in ('real', 'synthetic')),
  provider_version text,
  metadata jsonb not null default '{}'::jsonb check (jsonb_typeof(metadata) = 'object'),
  schema_version text not null,
  created_at timestamptz not null default now(),
  constraint sources_provider_not_blank check (btrim(provider) <> ''),
  constraint sources_title_not_blank check (btrim(title) <> ''),
  constraint sources_artifact_reference_not_blank check (btrim(artifact_reference) <> ''),
  constraint sources_artifact_reference_is_content_addressed check (
    artifact_reference = 'supabase-storage://eventlens-evidence/sha256/' || content_sha256
    or artifact_reference like 'local://%'
  ),
  constraint sources_media_type_not_blank check (btrim(media_type) <> ''),
  constraint sources_schema_version_not_blank check (btrim(schema_version) <> ''),
  constraint sources_retrieved_after_available check (retrieved_at >= available_at)
);

create unique index sources_provider_external_id_uidx
  on public.sources (provider, external_source_id)
  where external_source_id is not null;
create index sources_content_sha256_idx on public.sources (content_sha256);
create index sources_available_at_idx on public.sources (available_at);

create table public.earnings_events (
  id uuid primary key default gen_random_uuid(),
  company_id uuid not null references public.companies (id) on delete restrict,
  external_event_id text not null,
  fiscal_year integer not null check (fiscal_year between 1900 and 2200),
  fiscal_quarter integer not null check (fiscal_quarter between 1 and 4),
  fiscal_period_end date,
  event_date date not null,
  timing text not null check (
    timing in ('before_market_open', 'after_market_close', 'during_market_hours', 'unknown')
  ),
  release_at timestamptz not null,
  available_at timestamptz not null,
  timing_confirmed boolean not null default false,
  data_kind text not null check (data_kind in ('real', 'synthetic')),
  schema_version text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint earnings_events_external_id_not_blank check (btrim(external_event_id) <> ''),
  constraint earnings_events_schema_version_not_blank check (btrim(schema_version) <> ''),
  constraint earnings_events_external_kind_unique unique (company_id, external_event_id, data_kind),
  constraint earnings_events_period_kind_unique unique (
    company_id, fiscal_year, fiscal_quarter, data_kind
  )
);

create index earnings_events_company_event_date_idx
  on public.earnings_events (company_id, event_date desc);
create index earnings_events_release_at_idx on public.earnings_events (release_at);

create table public.event_sources (
  event_id uuid not null references public.earnings_events (id) on delete cascade,
  source_id uuid not null references public.sources (id) on delete restrict,
  relationship text not null check (
    relationship in ('event_timing', 'primary_filing', 'earnings_release', 'transcript', 'market_data', 'supplemental')
  ),
  is_primary boolean not null default false,
  created_at timestamptz not null default now(),
  primary key (event_id, source_id, relationship)
);

create index event_sources_source_idx on public.event_sources (source_id);

create table public.event_metrics (
  id uuid primary key default gen_random_uuid(),
  event_id uuid not null references public.earnings_events (id) on delete restrict,
  source_id uuid references public.sources (id) on delete restrict,
  metric_key text not null,
  metric_version integer not null default 1 check (metric_version > 0),
  value_numeric numeric,
  value_text text,
  value_json jsonb,
  unit text,
  period_label text,
  observed_at timestamptz,
  available_at timestamptz not null,
  calculation_version text not null,
  provenance_sha256 text not null check (provenance_sha256 ~ '^[a-f0-9]{64}$'),
  data_kind text not null check (data_kind in ('real', 'synthetic')),
  schema_version text not null,
  created_at timestamptz not null default now(),
  constraint event_metrics_key_not_blank check (btrim(metric_key) <> ''),
  constraint event_metrics_calculation_version_not_blank check (btrim(calculation_version) <> ''),
  constraint event_metrics_schema_version_not_blank check (btrim(schema_version) <> ''),
  constraint event_metrics_exactly_one_value check (
    num_nonnulls(value_numeric, value_text, value_json) = 1
  ),
  constraint event_metrics_unique_revision unique (event_id, metric_key, metric_version)
);

create index event_metrics_event_available_idx
  on public.event_metrics (event_id, available_at);
create index event_metrics_source_idx on public.event_metrics (source_id);

create table public.market_setups (
  id uuid primary key default gen_random_uuid(),
  event_id uuid not null references public.earnings_events (id) on delete restrict,
  setup_version integer not null default 1 check (setup_version > 0),
  as_of_at timestamptz not null,
  information_cutoff_at timestamptz not null,
  available_at timestamptz not null,
  payload jsonb not null check (jsonb_typeof(payload) = 'object'),
  payload_sha256 text not null check (payload_sha256 ~ '^[a-f0-9]{64}$'),
  calculation_version text not null,
  data_kind text not null check (data_kind in ('real', 'synthetic')),
  schema_version text not null,
  created_at timestamptz not null default now(),
  constraint market_setups_as_of_before_cutoff check (as_of_at <= information_cutoff_at),
  constraint market_setups_available_before_cutoff check (available_at <= information_cutoff_at),
  constraint market_setups_calculation_version_not_blank check (btrim(calculation_version) <> ''),
  constraint market_setups_schema_version_not_blank check (btrim(schema_version) <> ''),
  constraint market_setups_event_version_unique unique (event_id, setup_version)
);

create index market_setups_event_cutoff_idx
  on public.market_setups (event_id, information_cutoff_at desc);

create table public.market_setup_sources (
  market_setup_id uuid not null references public.market_setups (id) on delete cascade,
  source_id uuid not null references public.sources (id) on delete restrict,
  created_at timestamptz not null default now(),
  primary key (market_setup_id, source_id)
);

create index market_setup_sources_source_idx on public.market_setup_sources (source_id);

create table public.market_reactions (
  id uuid primary key default gen_random_uuid(),
  event_id uuid not null references public.earnings_events (id) on delete restrict,
  reaction_version integer not null default 1 check (reaction_version > 0),
  reference_session date not null,
  first_post_event_session date not null,
  fifth_post_event_session date,
  available_at timestamptz not null,
  payload jsonb not null check (jsonb_typeof(payload) = 'object'),
  payload_sha256 text not null check (payload_sha256 ~ '^[a-f0-9]{64}$'),
  calculation_version text not null,
  data_kind text not null check (data_kind in ('real', 'synthetic')),
  schema_version text not null,
  created_at timestamptz not null default now(),
  constraint market_reactions_sessions_ordered check (
    reference_session < first_post_event_session
    and (fifth_post_event_session is null or fifth_post_event_session >= first_post_event_session)
  ),
  constraint market_reactions_calculation_version_not_blank check (btrim(calculation_version) <> ''),
  constraint market_reactions_schema_version_not_blank check (btrim(schema_version) <> ''),
  constraint market_reactions_event_version_unique unique (event_id, reaction_version)
);

create index market_reactions_event_idx on public.market_reactions (event_id);

create table public.market_reaction_sources (
  market_reaction_id uuid not null references public.market_reactions (id) on delete cascade,
  source_id uuid not null references public.sources (id) on delete restrict,
  created_at timestamptz not null default now(),
  primary key (market_reaction_id, source_id)
);

create index market_reaction_sources_source_idx on public.market_reaction_sources (source_id);

create table public.research_sessions (
  id uuid primary key default gen_random_uuid(),
  session_key text not null unique,
  company_id uuid not null references public.companies (id) on delete restrict,
  event_id uuid not null references public.earnings_events (id) on delete restrict,
  run_mode text not null check (run_mode in ('replay', 'live')),
  data_kind text not null check (data_kind in ('real', 'synthetic')),
  phase text not null check (
    phase in (
      'created', 'evidence_frozen', 'awaiting_hypothesis', 'hypothesis_locked',
      'analysis_complete', 'awaiting_approval', 'approved', 'post_event', 'completed', 'failed'
    )
  ),
  information_cutoff_at timestamptz not null,
  replay_as_of_at timestamptz,
  actual_started_at timestamptz not null default now(),
  last_transition_at timestamptz not null default now(),
  compact_state jsonb not null default '{}'::jsonb check (jsonb_typeof(compact_state) = 'object'),
  state_sha256 text not null check (state_sha256 ~ '^[a-f0-9]{64}$'),
  workflow_version text not null,
  schema_version text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint research_sessions_key_not_blank check (btrim(session_key) <> ''),
  constraint research_sessions_workflow_version_not_blank check (btrim(workflow_version) <> ''),
  constraint research_sessions_schema_version_not_blank check (btrim(schema_version) <> ''),
  constraint research_sessions_replay_clock_explicit check (
    (run_mode = 'replay' and replay_as_of_at is not null and information_cutoff_at <= replay_as_of_at)
    or (run_mode = 'live' and replay_as_of_at is null and information_cutoff_at <= actual_started_at)
  )
);

create index research_sessions_event_started_idx
  on public.research_sessions (event_id, actual_started_at desc);
create index research_sessions_phase_idx on public.research_sessions (phase);

create table public.hypotheses (
  id uuid primary key default gen_random_uuid(),
  research_session_id uuid not null references public.research_sessions (id) on delete restrict,
  version integer not null check (version > 0),
  status text not null check (status in ('draft', 'locked')),
  author_mode text not null check (author_mode in ('analyst', 'model_draft_analyst_approved')),
  run_mode text not null check (run_mode in ('replay', 'live')),
  content jsonb not null check (jsonb_typeof(content) = 'object'),
  content_sha256 text not null check (content_sha256 ~ '^[a-f0-9]{64}$'),
  evidence_cutoff_at timestamptz not null,
  replay_as_of_at timestamptz,
  actual_created_at timestamptz not null default now(),
  approved_at timestamptz,
  approved_by text,
  prompt_version text,
  schema_version text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint hypotheses_schema_version_not_blank check (btrim(schema_version) <> ''),
  constraint hypotheses_clock_explicit check (
    (run_mode = 'replay' and replay_as_of_at is not null and evidence_cutoff_at <= replay_as_of_at)
    or (run_mode = 'live' and replay_as_of_at is null and evidence_cutoff_at <= actual_created_at)
  ),
  constraint hypotheses_lock_approval_complete check (
    (status = 'draft' and approved_at is null and approved_by is null)
    or (
      status = 'locked' and approved_at is not null
      and approved_by is not null and btrim(approved_by) <> ''
    )
  ),
  constraint hypotheses_approval_after_creation check (
    approved_at is null or approved_at >= actual_created_at
  ),
  constraint hypotheses_session_version_unique unique (research_session_id, version)
);

create index hypotheses_session_status_idx
  on public.hypotheses (research_session_id, status, version desc);

create table public.pre_event_snapshots (
  id uuid primary key default gen_random_uuid(),
  research_session_id uuid not null references public.research_sessions (id) on delete restrict,
  event_id uuid not null references public.earnings_events (id) on delete restrict,
  version integer not null check (version > 0),
  status text not null check (status in ('draft', 'finalized')),
  run_mode text not null check (run_mode in ('replay', 'live')),
  data_kind text not null check (data_kind in ('real', 'synthetic')),
  information_cutoff_at timestamptz not null,
  replay_as_of_at timestamptz,
  payload jsonb not null check (jsonb_typeof(payload) = 'object'),
  content_sha256 text not null check (content_sha256 ~ '^[a-f0-9]{64}$'),
  calculation_version text not null,
  workflow_version text not null,
  prompt_version text,
  schema_version text not null,
  actual_created_at timestamptz not null default now(),
  finalized_at timestamptz,
  approved_at timestamptz,
  approved_by text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint pre_event_snapshots_versions_not_blank check (
    btrim(calculation_version) <> '' and btrim(workflow_version) <> '' and btrim(schema_version) <> ''
  ),
  constraint pre_event_snapshots_clock_explicit check (
    (run_mode = 'replay' and replay_as_of_at is not null and information_cutoff_at <= replay_as_of_at)
    or (run_mode = 'live' and replay_as_of_at is null)
  ),
  constraint pre_event_snapshots_finalization_complete check (
    (status = 'draft' and finalized_at is null and approved_at is null and approved_by is null)
    or (
      status = 'finalized' and finalized_at is not null and approved_at is not null
      and approved_by is not null and btrim(approved_by) <> ''
      and finalized_at >= actual_created_at and approved_at >= finalized_at
    )
  ),
  constraint pre_event_snapshots_session_version_unique unique (research_session_id, version)
);

create index pre_event_snapshots_event_cutoff_idx
  on public.pre_event_snapshots (event_id, information_cutoff_at desc);
create index pre_event_snapshots_session_status_idx
  on public.pre_event_snapshots (research_session_id, status, version desc);

create table public.pre_event_snapshot_evidence (
  snapshot_id uuid not null references public.pre_event_snapshots (id) on delete cascade,
  source_id uuid not null references public.sources (id) on delete restrict,
  passage_id text not null,
  relationship text not null check (
    relationship in ('supports', 'contradicts', 'context', 'market_data', 'excluded')
  ),
  inclusion_reason text not null,
  available_at timestamptz not null,
  created_at timestamptz not null default now(),
  constraint snapshot_evidence_passage_not_blank check (btrim(passage_id) <> ''),
  constraint snapshot_evidence_reason_not_blank check (btrim(inclusion_reason) <> ''),
  primary key (snapshot_id, source_id, passage_id)
);

create index pre_event_snapshot_evidence_source_idx
  on public.pre_event_snapshot_evidence (source_id);

create table public.pre_event_snapshot_metrics (
  snapshot_id uuid not null references public.pre_event_snapshots (id) on delete cascade,
  event_metric_id uuid not null references public.event_metrics (id) on delete restrict,
  created_at timestamptz not null default now(),
  primary key (snapshot_id, event_metric_id)
);

create index pre_event_snapshot_metrics_metric_idx
  on public.pre_event_snapshot_metrics (event_metric_id);

create table public.hypothesis_assessments (
  id uuid primary key default gen_random_uuid(),
  hypothesis_id uuid not null references public.hypotheses (id) on delete restrict,
  research_session_id uuid not null references public.research_sessions (id) on delete restrict,
  snapshot_id uuid not null references public.pre_event_snapshots (id) on delete restrict,
  version integer not null check (version > 0),
  status text not null check (status in ('draft', 'finalized')),
  conclusion text not null check (conclusion in ('supported', 'weakened', 'rejected', 'unresolved')),
  run_mode text not null check (run_mode in ('replay', 'live')),
  payload jsonb not null check (jsonb_typeof(payload) = 'object'),
  content_sha256 text not null check (content_sha256 ~ '^[a-f0-9]{64}$'),
  prompt_version text not null,
  model_provider text not null,
  model_name text not null,
  schema_version text not null,
  knowledge_cutoff_at timestamptz not null,
  replay_as_of_at timestamptz,
  actual_created_at timestamptz not null default now(),
  finalized_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint hypothesis_assessments_versions_not_blank check (
    btrim(prompt_version) <> '' and btrim(model_provider) <> ''
    and btrim(model_name) <> '' and btrim(schema_version) <> ''
  ),
  constraint hypothesis_assessments_clock_explicit check (
    (run_mode = 'replay' and replay_as_of_at is not null and knowledge_cutoff_at <= replay_as_of_at)
    or (run_mode = 'live' and replay_as_of_at is null and knowledge_cutoff_at <= actual_created_at)
  ),
  constraint hypothesis_assessments_finalization_complete check (
    (status = 'draft' and finalized_at is null)
    or (status = 'finalized' and finalized_at is not null and finalized_at >= actual_created_at)
  ),
  constraint hypothesis_assessments_hypothesis_version_unique unique (hypothesis_id, version)
);

create index hypothesis_assessments_snapshot_idx
  on public.hypothesis_assessments (snapshot_id, status);

create table public.hypothesis_assessment_evidence (
  hypothesis_assessment_id uuid not null
    references public.hypothesis_assessments (id) on delete cascade,
  source_id uuid not null references public.sources (id) on delete restrict,
  passage_id text not null,
  stance text not null check (stance in ('supports', 'contradicts', 'context')),
  created_at timestamptz not null default now(),
  constraint hypothesis_assessment_evidence_passage_not_blank check (btrim(passage_id) <> ''),
  primary key (hypothesis_assessment_id, source_id, passage_id)
);

create index hypothesis_assessment_evidence_source_idx
  on public.hypothesis_assessment_evidence (source_id);

create table public.event_context_cards (
  id uuid primary key default gen_random_uuid(),
  event_id uuid not null references public.earnings_events (id) on delete restrict,
  research_session_id uuid not null references public.research_sessions (id) on delete restrict,
  snapshot_id uuid not null references public.pre_event_snapshots (id) on delete restrict,
  hypothesis_id uuid not null references public.hypotheses (id) on delete restrict,
  hypothesis_assessment_id uuid not null
    references public.hypothesis_assessments (id) on delete restrict,
  market_setup_id uuid not null references public.market_setups (id) on delete restrict,
  market_reaction_id uuid not null references public.market_reactions (id) on delete restrict,
  version integer not null check (version > 0),
  status text not null check (status in ('draft', 'approved')),
  data_kind text not null check (data_kind in ('real', 'synthetic')),
  run_mode text not null check (run_mode in ('replay', 'live')),
  knowledge_cutoff_at timestamptz not null,
  replay_as_of_at timestamptz,
  recommendation_action text check (
    recommendation_action is null
    or recommendation_action in ('BUY', 'DONT_BUY', 'TRIM', 'SELL')
  ),
  recommendation_price_floor numeric check (
    recommendation_price_floor is null or recommendation_price_floor >= 0
  ),
  recommendation_price_ceiling numeric check (
    recommendation_price_ceiling is null or recommendation_price_ceiling >= 0
  ),
  payload jsonb not null check (jsonb_typeof(payload) = 'object'),
  content_sha256 text not null check (content_sha256 ~ '^[a-f0-9]{64}$'),
  prompt_version text not null,
  model_provider text not null,
  model_name text not null,
  schema_version text not null,
  actual_created_at timestamptz not null default now(),
  approved_at timestamptz,
  approved_by text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint event_context_cards_versions_not_blank check (
    btrim(prompt_version) <> '' and btrim(model_provider) <> ''
    and btrim(model_name) <> '' and btrim(schema_version) <> ''
  ),
  constraint event_context_cards_price_range check (
    recommendation_price_floor is null or recommendation_price_ceiling is null
    or recommendation_price_floor <= recommendation_price_ceiling
  ),
  constraint event_context_cards_clock_explicit check (
    (run_mode = 'replay' and replay_as_of_at is not null and knowledge_cutoff_at <= replay_as_of_at)
    or (run_mode = 'live' and replay_as_of_at is null and knowledge_cutoff_at <= actual_created_at)
  ),
  constraint event_context_cards_approval_complete check (
    (status = 'draft' and approved_at is null and approved_by is null)
    or (
      status = 'approved' and approved_at is not null
      and approved_by is not null and btrim(approved_by) <> ''
    )
  ),
  constraint event_context_cards_approved_recommendation_required check (
    status = 'draft' or recommendation_action is not null
  ),
  constraint event_context_cards_approval_after_creation check (
    approved_at is null or approved_at >= actual_created_at
  ),
  constraint event_context_cards_event_version_unique unique (event_id, version)
);

create index event_context_cards_event_status_idx
  on public.event_context_cards (event_id, status, version desc);

create table public.context_card_evidence (
  context_card_id uuid not null references public.event_context_cards (id) on delete cascade,
  source_id uuid not null references public.sources (id) on delete restrict,
  passage_id text not null,
  relationship text not null check (
    relationship in ('supports', 'contradicts', 'context', 'market_data')
  ),
  created_at timestamptz not null default now(),
  constraint context_card_evidence_passage_not_blank check (btrim(passage_id) <> ''),
  primary key (context_card_id, source_id, passage_id)
);

create index context_card_evidence_source_idx on public.context_card_evidence (source_id);

-- Mutable rows receive database-authored update timestamps.
create trigger companies_set_updated_at
before update on public.companies
for each row execute function public.eventlens_set_updated_at();
create trigger earnings_events_set_updated_at
before update on public.earnings_events
for each row execute function public.eventlens_set_updated_at();
create trigger research_sessions_set_updated_at
before update on public.research_sessions
for each row execute function public.eventlens_set_updated_at();
create trigger hypotheses_set_updated_at
before update on public.hypotheses
for each row execute function public.eventlens_set_updated_at();
create trigger pre_event_snapshots_set_updated_at
before update on public.pre_event_snapshots
for each row execute function public.eventlens_set_updated_at();
create trigger hypothesis_assessments_set_updated_at
before update on public.hypothesis_assessments
for each row execute function public.eventlens_set_updated_at();
create trigger event_context_cards_set_updated_at
before update on public.event_context_cards
for each row execute function public.eventlens_set_updated_at();

create or replace function public.eventlens_protect_locked_hypothesis()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if old.status = 'locked' then
    raise exception 'locked hypothesis % is immutable', old.id;
  end if;
  if tg_op = 'UPDATE' and new.status = 'locked' then
    if new.content is distinct from old.content
      or new.content_sha256 is distinct from old.content_sha256
      or new.research_session_id is distinct from old.research_session_id
      or new.version is distinct from old.version
      or new.author_mode is distinct from old.author_mode
      or new.run_mode is distinct from old.run_mode
      or new.evidence_cutoff_at is distinct from old.evidence_cutoff_at
      or new.replay_as_of_at is distinct from old.replay_as_of_at
      or new.actual_created_at is distinct from old.actual_created_at
      or new.prompt_version is distinct from old.prompt_version
      or new.schema_version is distinct from old.schema_version
    then
      raise exception 'locking hypothesis % cannot rewrite its reviewed content or identity', old.id;
    end if;
    if not exists (
      select 1 from public.research_sessions session
      where session.id = new.research_session_id
        and session.information_cutoff_at = new.evidence_cutoff_at
        and session.run_mode = new.run_mode
        and session.replay_as_of_at is not distinct from new.replay_as_of_at
    ) then
      raise exception 'hypothesis % cutoff is inconsistent with its session', old.id;
    end if;
  end if;
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end;
$$;

create trigger hypotheses_protect_locked
before update or delete on public.hypotheses
for each row execute function public.eventlens_protect_locked_hypothesis();

create or replace function public.eventlens_protect_finalized_snapshot()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if old.status = 'finalized' then
    raise exception 'finalized snapshot % is immutable', old.id;
  end if;
  if tg_op = 'UPDATE' and new.status = 'finalized' then
    if new.payload is distinct from old.payload
      or new.content_sha256 is distinct from old.content_sha256
      or new.research_session_id is distinct from old.research_session_id
      or new.event_id is distinct from old.event_id
      or new.version is distinct from old.version
      or new.run_mode is distinct from old.run_mode
      or new.data_kind is distinct from old.data_kind
      or new.information_cutoff_at is distinct from old.information_cutoff_at
      or new.replay_as_of_at is distinct from old.replay_as_of_at
      or new.calculation_version is distinct from old.calculation_version
      or new.workflow_version is distinct from old.workflow_version
      or new.prompt_version is distinct from old.prompt_version
      or new.schema_version is distinct from old.schema_version
      or new.actual_created_at is distinct from old.actual_created_at
    then
      raise exception 'finalizing snapshot % cannot rewrite its reviewed content or identity', old.id;
    end if;
    if not exists (
      select 1 from public.pre_event_snapshot_evidence where snapshot_id = old.id
    ) then
      raise exception 'snapshot % requires evidence before finalization', old.id;
    end if;
    if not exists (
      select 1 from public.pre_event_snapshot_metrics where snapshot_id = old.id
    ) then
      raise exception 'snapshot % requires canonical metrics before finalization', old.id;
    end if;
    if not exists (
      select 1
      from public.research_sessions session
      join public.earnings_events event on event.id = session.event_id
      where session.id = new.research_session_id
        and session.event_id = new.event_id
        and event.company_id = session.company_id
        and event.data_kind = new.data_kind
        and session.run_mode = new.run_mode
        and session.data_kind = new.data_kind
        and session.information_cutoff_at = new.information_cutoff_at
        and session.replay_as_of_at is not distinct from new.replay_as_of_at
    ) then
      raise exception 'snapshot % is inconsistent with its research session', old.id;
    end if;
    if exists (
      select 1
      from public.pre_event_snapshot_evidence link
      join public.sources source on source.id = link.source_id
      where link.snapshot_id = old.id
        and (
          link.available_at <> source.available_at
          or source.available_at > new.information_cutoff_at
          or source.data_kind <> new.data_kind
        )
    ) then
      raise exception 'snapshot % contains evidence unavailable at its cutoff', old.id;
    end if;
    if exists (
      select 1
      from public.pre_event_snapshot_metrics link
      join public.event_metrics metric on metric.id = link.event_metric_id
      where link.snapshot_id = old.id
        and (
          metric.event_id <> new.event_id
          or metric.available_at > new.information_cutoff_at
          or metric.data_kind <> new.data_kind
        )
    ) then
      raise exception 'snapshot % contains unavailable or cross-event metrics', old.id;
    end if;
  end if;
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end;
$$;

create trigger pre_event_snapshots_protect_finalized
before update or delete on public.pre_event_snapshots
for each row execute function public.eventlens_protect_finalized_snapshot();

create or replace function public.eventlens_protect_snapshot_links()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  target_snapshot_id uuid;
  target_status text;
begin
  if tg_op = 'DELETE' then
    target_snapshot_id := old.snapshot_id;
  else
    target_snapshot_id := new.snapshot_id;
  end if;
  select status into target_status
  from public.pre_event_snapshots
  where id = target_snapshot_id;
  if target_status = 'finalized' then
    raise exception 'evidence links for finalized snapshot % are immutable', target_snapshot_id;
  end if;
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end;
$$;

create trigger pre_event_snapshot_evidence_protect_finalized
before insert or update or delete on public.pre_event_snapshot_evidence
for each row execute function public.eventlens_protect_snapshot_links();
create trigger pre_event_snapshot_metrics_protect_finalized
before insert or update or delete on public.pre_event_snapshot_metrics
for each row execute function public.eventlens_protect_snapshot_links();

create or replace function public.eventlens_protect_finalized_assessment()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if old.status = 'finalized' then
    raise exception 'finalized hypothesis assessment % is immutable', old.id;
  end if;
  if tg_op = 'UPDATE' and new.status = 'finalized' then
    if new.payload is distinct from old.payload
      or new.content_sha256 is distinct from old.content_sha256
      or new.hypothesis_id is distinct from old.hypothesis_id
      or new.research_session_id is distinct from old.research_session_id
      or new.snapshot_id is distinct from old.snapshot_id
      or new.version is distinct from old.version
      or new.conclusion is distinct from old.conclusion
      or new.run_mode is distinct from old.run_mode
      or new.prompt_version is distinct from old.prompt_version
      or new.model_provider is distinct from old.model_provider
      or new.model_name is distinct from old.model_name
      or new.schema_version is distinct from old.schema_version
      or new.knowledge_cutoff_at is distinct from old.knowledge_cutoff_at
      or new.replay_as_of_at is distinct from old.replay_as_of_at
      or new.actual_created_at is distinct from old.actual_created_at
    then
      raise exception 'finalizing assessment % cannot rewrite its reviewed content or identity', old.id;
    end if;
    if not exists (
      select 1
      from public.hypothesis_assessment_evidence
      where hypothesis_assessment_id = old.id
    ) then
      raise exception 'hypothesis assessment % requires evidence before finalization', old.id;
    end if;
    if not exists (
      select 1
      from public.hypotheses hypothesis
      join public.pre_event_snapshots snapshot
        on snapshot.research_session_id = hypothesis.research_session_id
      where hypothesis.id = new.hypothesis_id
        and hypothesis.research_session_id = new.research_session_id
        and hypothesis.status = 'locked'
        and snapshot.id = new.snapshot_id
        and snapshot.status = 'finalized'
        and snapshot.information_cutoff_at = new.knowledge_cutoff_at
        and snapshot.run_mode = new.run_mode
        and snapshot.replay_as_of_at is not distinct from new.replay_as_of_at
    ) then
      raise exception 'assessment % requires a locked hypothesis and matching finalized snapshot', old.id;
    end if;
    if exists (
      select 1
      from public.hypothesis_assessment_evidence link
      join public.sources source on source.id = link.source_id
      where link.hypothesis_assessment_id = old.id
        and (
          source.available_at > new.knowledge_cutoff_at
          or source.data_kind <> (
            select data_kind from public.research_sessions where id = new.research_session_id
          )
        )
    ) then
      raise exception 'assessment % contains evidence unavailable at its cutoff', old.id;
    end if;
  end if;
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end;
$$;

create trigger hypothesis_assessments_protect_finalized
before update or delete on public.hypothesis_assessments
for each row execute function public.eventlens_protect_finalized_assessment();

create or replace function public.eventlens_protect_assessment_links()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  target_assessment_id uuid;
  target_status text;
begin
  if tg_op = 'DELETE' then
    target_assessment_id := old.hypothesis_assessment_id;
  else
    target_assessment_id := new.hypothesis_assessment_id;
  end if;
  select status into target_status
  from public.hypothesis_assessments
  where id = target_assessment_id;
  if target_status = 'finalized' then
    raise exception 'evidence links for finalized assessment % are immutable', target_assessment_id;
  end if;
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end;
$$;

create trigger hypothesis_assessment_evidence_protect_finalized
before insert or update or delete on public.hypothesis_assessment_evidence
for each row execute function public.eventlens_protect_assessment_links();

create or replace function public.eventlens_protect_approved_card()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if old.status = 'approved' then
    raise exception 'approved context card % is immutable', old.id;
  end if;
  if tg_op = 'UPDATE' and new.status = 'approved' then
    if new.payload is distinct from old.payload
      or new.content_sha256 is distinct from old.content_sha256
      or new.event_id is distinct from old.event_id
      or new.research_session_id is distinct from old.research_session_id
      or new.snapshot_id is distinct from old.snapshot_id
      or new.hypothesis_id is distinct from old.hypothesis_id
      or new.hypothesis_assessment_id is distinct from old.hypothesis_assessment_id
      or new.market_setup_id is distinct from old.market_setup_id
      or new.market_reaction_id is distinct from old.market_reaction_id
      or new.version is distinct from old.version
      or new.data_kind is distinct from old.data_kind
      or new.run_mode is distinct from old.run_mode
      or new.knowledge_cutoff_at is distinct from old.knowledge_cutoff_at
      or new.replay_as_of_at is distinct from old.replay_as_of_at
      or new.recommendation_action is distinct from old.recommendation_action
      or new.recommendation_price_floor is distinct from old.recommendation_price_floor
      or new.recommendation_price_ceiling is distinct from old.recommendation_price_ceiling
      or new.prompt_version is distinct from old.prompt_version
      or new.model_provider is distinct from old.model_provider
      or new.model_name is distinct from old.model_name
      or new.schema_version is distinct from old.schema_version
      or new.actual_created_at is distinct from old.actual_created_at
    then
      raise exception 'approving context card % cannot rewrite its reviewed content or identity', old.id;
    end if;
    if not exists (
      select 1 from public.context_card_evidence where context_card_id = old.id
    ) then
      raise exception 'context card % requires evidence before approval', old.id;
    end if;
    if not exists (
      select 1
      from public.pre_event_snapshots snapshot
      join public.hypotheses hypothesis
        on hypothesis.research_session_id = snapshot.research_session_id
      join public.hypothesis_assessments assessment
        on assessment.hypothesis_id = hypothesis.id and assessment.snapshot_id = snapshot.id
      join public.market_setups setup on setup.event_id = snapshot.event_id
      join public.market_reactions reaction on reaction.event_id = snapshot.event_id
      where snapshot.id = new.snapshot_id
        and snapshot.status = 'finalized'
        and hypothesis.id = new.hypothesis_id
        and hypothesis.status = 'locked'
        and assessment.id = new.hypothesis_assessment_id
        and assessment.status = 'finalized'
        and setup.id = new.market_setup_id
        and setup.data_kind = new.data_kind
        and setup.available_at <= new.knowledge_cutoff_at
        and reaction.id = new.market_reaction_id
        and reaction.data_kind = new.data_kind
        and reaction.available_at <= new.knowledge_cutoff_at
        and snapshot.event_id = new.event_id
        and snapshot.research_session_id = new.research_session_id
        and snapshot.run_mode = new.run_mode
        and snapshot.data_kind = new.data_kind
        and assessment.knowledge_cutoff_at <= new.knowledge_cutoff_at
    ) then
      raise exception 'context card % has inconsistent or non-final object references', old.id;
    end if;
    if exists (
      select 1
      from public.context_card_evidence link
      join public.sources source on source.id = link.source_id
      where link.context_card_id = old.id
        and (
          source.available_at > new.knowledge_cutoff_at
          or source.data_kind <> new.data_kind
        )
    ) then
      raise exception 'context card % contains evidence unavailable at its cutoff', old.id;
    end if;
  end if;
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end;
$$;

create trigger event_context_cards_protect_approved
before update or delete on public.event_context_cards
for each row execute function public.eventlens_protect_approved_card();

create or replace function public.eventlens_protect_card_links()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  target_card_id uuid;
  target_status text;
begin
  if tg_op = 'DELETE' then
    target_card_id := old.context_card_id;
  else
    target_card_id := new.context_card_id;
  end if;
  select status into target_status
  from public.event_context_cards
  where id = target_card_id;
  if target_status = 'approved' then
    raise exception 'evidence links for approved context card % are immutable', target_card_id;
  end if;
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end;
$$;

create trigger context_card_evidence_protect_approved
before insert or update or delete on public.context_card_evidence
for each row execute function public.eventlens_protect_card_links();

create or replace function public.eventlens_require_initial_draft()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.status <> 'draft' then
    raise exception '% must be inserted as a draft and finalized in a separate transaction', tg_table_name;
  end if;
  return new;
end;
$$;

create trigger hypotheses_require_initial_draft
before insert on public.hypotheses
for each row execute function public.eventlens_require_initial_draft();
create trigger pre_event_snapshots_require_initial_draft
before insert on public.pre_event_snapshots
for each row execute function public.eventlens_require_initial_draft();
create trigger hypothesis_assessments_require_initial_draft
before insert on public.hypothesis_assessments
for each row execute function public.eventlens_require_initial_draft();
create trigger event_context_cards_require_initial_draft
before insert on public.event_context_cards
for each row execute function public.eventlens_require_initial_draft();

create or replace function public.eventlens_reject_update_delete()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception '% rows are append-only; insert a new version instead', tg_table_name;
end;
$$;

-- These rows are transitive inputs to immutable snapshots/cards. Corrections
-- are new versioned rows, never in-place edits.
create trigger sources_append_only
before update or delete on public.sources
for each row execute function public.eventlens_reject_update_delete();
create trigger event_sources_append_only
before update or delete on public.event_sources
for each row execute function public.eventlens_reject_update_delete();
create trigger event_metrics_append_only
before update or delete on public.event_metrics
for each row execute function public.eventlens_reject_update_delete();
create trigger market_setups_append_only
before update or delete on public.market_setups
for each row execute function public.eventlens_reject_update_delete();
create trigger market_reactions_append_only
before update or delete on public.market_reactions
for each row execute function public.eventlens_reject_update_delete();

create or replace function public.eventlens_protect_session_identity()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.session_key is distinct from old.session_key
    or new.company_id is distinct from old.company_id
    or new.event_id is distinct from old.event_id
    or new.run_mode is distinct from old.run_mode
    or new.data_kind is distinct from old.data_kind
    or new.information_cutoff_at is distinct from old.information_cutoff_at
    or new.replay_as_of_at is distinct from old.replay_as_of_at
    or new.actual_started_at is distinct from old.actual_started_at
    or new.workflow_version is distinct from old.workflow_version
    or new.schema_version is distinct from old.schema_version
  then
    raise exception 'research session identity and clock fields are immutable';
  end if;
  return new;
end;
$$;

create trigger research_sessions_protect_identity
before update on public.research_sessions
for each row execute function public.eventlens_protect_session_identity();

create or replace function public.eventlens_validate_session_lineage()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if not exists (
    select 1
    from public.earnings_events event
    where event.id = new.event_id
      and event.company_id = new.company_id
      and event.data_kind = new.data_kind
  ) then
    raise exception 'research session company/event/data-kind lineage is inconsistent';
  end if;
  return new;
end;
$$;

create trigger research_sessions_validate_lineage
before insert on public.research_sessions
for each row execute function public.eventlens_validate_session_lineage();

create or replace function public.eventlens_protect_event_truth()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if exists (
    select 1 from public.pre_event_snapshots
    where event_id = old.id and status = 'finalized'
  ) or exists (
    select 1 from public.event_context_cards
    where event_id = old.id and status = 'approved'
  ) then
    raise exception 'earnings event % is referenced by finalized research memory', old.id;
  end if;
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end;
$$;

create trigger earnings_events_protect_referenced_truth
before update or delete on public.earnings_events
for each row execute function public.eventlens_protect_event_truth();

create or replace function public.eventlens_protect_market_source_links()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  object_id uuid;
  is_setup boolean;
begin
  is_setup := tg_table_name = 'market_setup_sources';
  if is_setup then
    object_id := case when tg_op = 'DELETE' then old.market_setup_id else new.market_setup_id end;
    if exists (
      select 1 from public.event_context_cards
      where market_setup_id = object_id and status = 'approved'
    ) then
      raise exception 'sources for market setup % are frozen by an approved card', object_id;
    end if;
  else
    object_id := case when tg_op = 'DELETE' then old.market_reaction_id else new.market_reaction_id end;
    if exists (
      select 1 from public.event_context_cards
      where market_reaction_id = object_id and status = 'approved'
    ) then
      raise exception 'sources for market reaction % are frozen by an approved card', object_id;
    end if;
  end if;
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end;
$$;

create trigger market_setup_sources_protect_approved
before insert or update or delete on public.market_setup_sources
for each row execute function public.eventlens_protect_market_source_links();
create trigger market_reaction_sources_protect_approved
before insert or update or delete on public.market_reaction_sources
for each row execute function public.eventlens_protect_market_source_links();

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
      'companies', 'sources', 'earnings_events', 'event_sources', 'event_metrics',
      'market_setups', 'market_setup_sources', 'market_reactions', 'market_reaction_sources',
      'research_sessions', 'hypotheses', 'pre_event_snapshots',
      'pre_event_snapshot_evidence', 'pre_event_snapshot_metrics',
      'hypothesis_assessments', 'hypothesis_assessment_evidence',
      'event_context_cards', 'context_card_evidence'
    )
  order by stats.relname;
$$;

revoke all on function public.eventlens_relation_sizes() from public, anon, authenticated;
grant execute on function public.eventlens_relation_sizes() to service_role;

create or replace function public.eventlens_evidence_storage_size()
returns table (object_count bigint, content_bytes bigint)
language sql
security definer
set search_path = pg_catalog, public
as $$
  select count(*)::bigint, coalesce(sum(object.size_bytes), 0)::bigint
  from (
    select content_sha256, max(size_bytes)::bigint as size_bytes
    from public.sources
    group by content_sha256
  ) object;
$$;

revoke all on function public.eventlens_evidence_storage_size()
  from public, anon, authenticated;
grant execute on function public.eventlens_evidence_storage_size() to service_role;

-- No policies are declared in Milestone 1. The server-only secret bypasses RLS;
-- public API roles see no rows and cannot mutate the ledger.
alter table public.companies enable row level security;
alter table public.sources enable row level security;
alter table public.earnings_events enable row level security;
alter table public.event_sources enable row level security;
alter table public.event_metrics enable row level security;
alter table public.market_setups enable row level security;
alter table public.market_setup_sources enable row level security;
alter table public.market_reactions enable row level security;
alter table public.market_reaction_sources enable row level security;
alter table public.research_sessions enable row level security;
alter table public.hypotheses enable row level security;
alter table public.pre_event_snapshots enable row level security;
alter table public.pre_event_snapshot_evidence enable row level security;
alter table public.pre_event_snapshot_metrics enable row level security;
alter table public.hypothesis_assessments enable row level security;
alter table public.hypothesis_assessment_evidence enable row level security;
alter table public.event_context_cards enable row level security;
alter table public.context_card_evidence enable row level security;

comment on table public.research_sessions is
  'Compact resumable session state. replay_as_of_at is simulated time; actual_started_at is wall-clock time.';
comment on table public.pre_event_snapshots is
  'Versioned evidence contract. Drafts are mutable; finalized rows and their links are immutable.';
comment on table public.hypothesis_assessments is
  'Independent evidence-linked support/weaken/reject/unresolved assessment of the locked initial hypothesis.';
comment on table public.event_context_cards is
  'Per-event memory synthesized from independently persisted event, setup, reaction, snapshot, and hypothesis objects.';
