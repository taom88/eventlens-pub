-- Forward-only passage integrity for evidence links created after v1 receipts.

alter table public.source_passages
add constraint source_passages_text_sha256_matches_text
check (
  text_sha256 = encode(extensions.digest(convert_to("text", 'UTF8'), 'sha256'), 'hex')
) not valid;

alter table public.pre_event_snapshot_evidence
add constraint pre_event_snapshot_evidence_passage_fk
foreign key (source_id, passage_id)
references public.source_passages (source_id, passage_id)
not valid;

alter table public.hypothesis_assessment_evidence
add constraint hypothesis_assessment_evidence_passage_fk
foreign key (source_id, passage_id)
references public.source_passages (source_id, passage_id)
not valid;

alter table public.context_card_evidence
add constraint context_card_evidence_passage_fk
foreign key (source_id, passage_id)
references public.source_passages (source_id, passage_id)
not valid;

create or replace function public.eventlens_evidence_storage_size()
returns table (object_count bigint, content_bytes bigint)
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  if to_regclass('storage.objects') is null then
    return query select 0::bigint, 0::bigint;
  end if;
  return query execute $sql$
    select
      count(*)::bigint,
      coalesce(sum((metadata->>'size')::bigint), 0)::bigint
    from storage.objects
    where bucket_id = 'eventlens-evidence'
      and name like 'sha256/%'
  $sql$;
end;
$$;

revoke all on function public.eventlens_evidence_storage_size()
  from public, anon, authenticated;
grant execute on function public.eventlens_evidence_storage_size() to service_role;
