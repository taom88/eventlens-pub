-- Allow only backend service clients to operate the Milestone 1 memory tables.
-- RLS remains enabled and no anon/authenticated policies are added.

grant usage on schema public to service_role;

grant select, insert, update, delete on table
  public.companies,
  public.sources,
  public.earnings_events,
  public.event_sources,
  public.event_metrics,
  public.market_setups,
  public.market_setup_sources,
  public.market_reactions,
  public.market_reaction_sources,
  public.research_sessions,
  public.hypotheses,
  public.pre_event_snapshots,
  public.pre_event_snapshot_evidence,
  public.pre_event_snapshot_metrics,
  public.hypothesis_assessments,
  public.hypothesis_assessment_evidence,
  public.event_context_cards,
  public.context_card_evidence
to service_role;
