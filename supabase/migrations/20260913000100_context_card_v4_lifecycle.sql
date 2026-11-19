-- Forward-only Context Card v4 lifecycle.  Existing v1-v3 rows remain valid
-- and are not rewritten; their draft/approved states retain their semantics.

alter table public.event_context_cards
  drop constraint if exists event_context_cards_status_check,
  drop constraint if exists event_context_cards_approval_complete,
  drop constraint if exists event_context_cards_approved_recommendation_required;

alter table public.event_context_cards
  add column if not exists decision_trace jsonb,
  add column if not exists reviewed_at timestamptz,
  add column if not exists reviewed_by text,
  add column if not exists stale_at timestamptz,
  add constraint event_context_cards_status_v4_check
    check (
      (schema_version ~ '^eventlens[.]context_card[.]v4'
        and status in ('draft', 'blocked', 'reviewed', 'approved', 'stale'))
      or (schema_version !~ '^eventlens[.]context_card[.]v4'
        and status in ('draft', 'approved'))
    ),
  add constraint event_context_cards_v4_lifecycle_complete
    check (
      (schema_version !~ '^eventlens[.]context_card[.]v4'
        and ((status = 'draft' and approved_at is null and approved_by is null)
          or (status = 'approved' and approved_at is not null
            and approved_by is not null and btrim(approved_by) <> '')))
      or (schema_version ~ '^eventlens[.]context_card[.]v4'
        and ((status = 'draft'
          and reviewed_at is null and reviewed_by is null
          and approved_at is null and approved_by is null and stale_at is null)
        or (status = 'blocked' and approved_at is null and approved_by is null and stale_at is null)
        or (status = 'reviewed' and reviewed_at is not null and btrim(reviewed_by) <> ''
          and approved_at is null and approved_by is null and stale_at is null)
        or (status = 'approved' and approved_at is not null and btrim(approved_by) <> '')
        or (status = 'stale' and stale_at is not null)))
    ),
  add constraint event_context_cards_v4_decision_trace_required
    check (
      (schema_version !~ '^eventlens[.]context_card[.]v4'
        and (status = 'draft' or recommendation_action is not null))
      or (schema_version ~ '^eventlens[.]context_card[.]v4'
        and (status <> 'approved'
          or recommendation_action is null
          or (decision_trace is not null and jsonb_typeof(decision_trace) = 'object'
              and decision_trace <> '{}'::jsonb)))
    );

create table if not exists public.context_card_lifecycle_audit (
  id bigint generated always as identity primary key,
  context_card_id uuid not null references public.event_context_cards (id) on delete restrict,
  from_status text not null,
  to_status text not null,
  actor text not null,
  occurred_at timestamptz not null default now(),
  constraint context_card_lifecycle_audit_statuses_check
    check (from_status in ('draft', 'blocked', 'reviewed', 'approved', 'stale')
       and to_status in ('draft', 'blocked', 'reviewed', 'approved', 'stale')
       and from_status <> to_status)
);

create index if not exists context_card_lifecycle_audit_card_idx
  on public.context_card_lifecycle_audit (context_card_id, occurred_at, id);

alter table public.context_card_lifecycle_audit enable row level security;

create or replace function public.eventlens_protect_context_card_v4_lifecycle()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  content_unchanged boolean;
begin
  -- Existing v1-v3 rows retain their original trigger and constraint semantics.
  if tg_op = 'DELETE' and old.schema_version !~ '^eventlens[.]context_card[.]v4' then
    return old;
  end if;
  if tg_op = 'UPDATE'
     and old.schema_version !~ '^eventlens[.]context_card[.]v4'
     and new.schema_version !~ '^eventlens[.]context_card[.]v4' then
    return new;
  end if;

  if tg_op = 'DELETE' and old.status in ('reviewed', 'approved', 'stale') then
    raise exception 'terminal context card % is immutable', old.id;
  end if;
  if tg_op = 'DELETE' then
    return old;
  end if;

  content_unchanged :=
    (to_jsonb(new) - array['status', 'reviewed_at', 'reviewed_by', 'approved_at',
                             'approved_by', 'stale_at', 'updated_at'])
    is not distinct from
    (to_jsonb(old) - array['status', 'reviewed_at', 'reviewed_by', 'approved_at',
                             'approved_by', 'stale_at', 'updated_at']);

  if old.status in ('reviewed', 'approved', 'stale') and not content_unchanged then
    raise exception 'terminal context card % cannot rewrite reviewed content or identity', old.id;
  end if;
  if old.status = 'stale' and new.status <> 'stale' then
    raise exception 'stale context card % is immutable', old.id;
  end if;
  if old.status = 'reviewed' and new.status not in ('reviewed', 'approved', 'stale') then
    raise exception 'reviewed context card % cannot return to a mutable state', old.id;
  end if;
  if old.status = 'approved' and not (
    new.status = 'approved'
    or (new.status = 'stale' and new.schema_version ~ '^eventlens[.]context_card[.]v4')
  ) then
    raise exception 'approved context card % is immutable except v4 staleness', old.id;
  end if;
  if new.status in ('reviewed', 'approved', 'stale') and not content_unchanged then
    raise exception 'transitioning context card % cannot rewrite reviewed content or identity', old.id;
  end if;
  return new;
end;
$$;

-- Keep the original trigger for legacy rows.  It is made a no-op for v4 rows
-- below, so the v4 lifecycle trigger can permit an approved -> stale transition.
create or replace function public.eventlens_protect_approved_card()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if tg_op = 'DELETE' and old.schema_version ~ '^eventlens[.]context_card[.]v4' then
    return old;
  end if;
  if tg_op = 'UPDATE' and (old.schema_version ~ '^eventlens[.]context_card[.]v4'
     or new.schema_version ~ '^eventlens[.]context_card[.]v4') then
    return new;
  end if;
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
        and (source.available_at > new.knowledge_cutoff_at or source.data_kind <> new.data_kind)
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

create trigger event_context_cards_protect_v4_lifecycle
before update or delete on public.event_context_cards
for each row execute function public.eventlens_protect_context_card_v4_lifecycle();

create or replace function public.eventlens_protect_card_links()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  target_card_id uuid;
  target_status text;
  target_schema_version text;
begin
  if tg_op = 'DELETE' then
    target_card_id := old.context_card_id;
  else
    target_card_id := new.context_card_id;
  end if;
  select status, schema_version into target_status, target_schema_version
  from public.event_context_cards
  where id = target_card_id;
  if target_status = 'approved'
     or (target_schema_version ~ '^eventlens[.]context_card[.]v4'
         and target_status in ('reviewed', 'stale')) then
    raise exception 'evidence links for terminal context card % are immutable', target_card_id;
  end if;
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end;
$$;

create or replace function public.eventlens_audit_context_card_lifecycle()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if old.status is distinct from new.status then
    insert into public.context_card_lifecycle_audit
      (context_card_id, from_status, to_status, actor)
    values
      (new.id, old.status, new.status,
       coalesce(nullif(new.approved_by, ''), nullif(new.reviewed_by, ''), current_user));
  end if;
  return new;
end;
$$;

create trigger event_context_cards_audit_v4_lifecycle
after update on public.event_context_cards
for each row execute function public.eventlens_audit_context_card_lifecycle();

create or replace function public.eventlens_reject_context_card_lifecycle_audit_mutation()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception 'context card lifecycle audit is append-only';
end;
$$;

create trigger context_card_lifecycle_audit_append_only
before update or delete on public.context_card_lifecycle_audit
for each row execute function public.eventlens_reject_context_card_lifecycle_audit_mutation();

grant select, insert on table public.context_card_lifecycle_audit to service_role;
grant usage, select on sequence public.context_card_lifecycle_audit_id_seq to service_role;
