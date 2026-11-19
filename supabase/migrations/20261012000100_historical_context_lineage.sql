-- Persist the exact checksum-addressed prior-card manifest used by each
-- hypothesis, pre-event snapshot, and resulting context card.  Empty values
-- preserve compatibility with artifacts created before this migration.

alter table public.hypotheses
  add column if not exists historical_context_manifest jsonb not null default '[]'::jsonb,
  add column if not exists historical_context_manifest_sha256 text;

alter table public.pre_event_snapshots
  add column if not exists historical_context_manifest jsonb not null default '[]'::jsonb,
  add column if not exists historical_context_manifest_sha256 text;

alter table public.event_context_cards
  add column if not exists historical_context_manifest jsonb not null default '[]'::jsonb,
  add column if not exists historical_context_manifest_sha256 text;

alter table public.hypotheses
  add constraint hypotheses_historical_context_manifest_valid check (
    jsonb_typeof(historical_context_manifest) = 'array'
    and (
      (jsonb_array_length(historical_context_manifest) = 0 and historical_context_manifest_sha256 is null)
      or (
        jsonb_array_length(historical_context_manifest) > 0
        and historical_context_manifest_sha256 ~ '^[a-f0-9]{64}$'
      )
    )
  );

alter table public.pre_event_snapshots
  add constraint pre_event_snapshots_historical_context_manifest_valid check (
    jsonb_typeof(historical_context_manifest) = 'array'
    and (
      (jsonb_array_length(historical_context_manifest) = 0 and historical_context_manifest_sha256 is null)
      or (
        jsonb_array_length(historical_context_manifest) > 0
        and historical_context_manifest_sha256 ~ '^[a-f0-9]{64}$'
      )
    )
  );

alter table public.event_context_cards
  add constraint event_context_cards_historical_context_manifest_valid check (
    jsonb_typeof(historical_context_manifest) = 'array'
    and (
      (jsonb_array_length(historical_context_manifest) = 0 and historical_context_manifest_sha256 is null)
      or (
        jsonb_array_length(historical_context_manifest) > 0
        and historical_context_manifest_sha256 ~ '^[a-f0-9]{64}$'
      )
    )
  );

create index if not exists hypotheses_historical_context_manifest_sha_idx
  on public.hypotheses (historical_context_manifest_sha256)
  where historical_context_manifest_sha256 is not null;

create index if not exists pre_event_snapshots_historical_context_manifest_sha_idx
  on public.pre_event_snapshots (historical_context_manifest_sha256)
  where historical_context_manifest_sha256 is not null;

create index if not exists event_context_cards_historical_context_manifest_sha_idx
  on public.event_context_cards (historical_context_manifest_sha256)
  where historical_context_manifest_sha256 is not null;
