# E0 MVP-401 research-evidence evaluation

**Status:** PASS

- Suite: `mvp401-research-evidence-e0-v1` (`75adcaaf4de89dd0f3371e20b4c59271cc461a6117c1011dc23d81453a6479c8`)
- Evaluator: `eventlens-e0-research-evidence-v1`
- Evaluator source: `846e3d5acb8ae84d546940c13619da6d5021381d97d65f2b647a5f6e990505fd`
- Production boundary: `0000000000000000000000000000000000000000`
- Execution base: `0000000000000000000000000000000000000000`
- Evaluated at: `2026-11-08T06:20:32Z`

## Result

17/17 cases matched their independent labels; critical failures: 0.

| Case | Expected | Observed | Reason codes |
|---|---|---|---|
| `bad_citation` | blocked | blocked | `unresolved_citation` |
| `complete_projection` | pass | pass | `all_components_projected`, `citations_resolved`, `network_calls_zero`, `projection_deterministic` |
| `history_authorization_late` | blocked | blocked | `history_authorization_late` |
| `history_authorization_missing` | blocked | blocked | `history_authorization_missing` |
| `history_authorization_ticker_mismatch` | blocked | blocked | `history_authorization_ticker_mismatch` |
| `history_draft_card` | blocked | blocked | `history_card_not_approved` |
| `history_exception_with_three_cards` | blocked | blocked | `history_exception_with_three_cards` |
| `history_future_card` | blocked | blocked | `history_future_event` |
| `history_three_approved` | pass | pass | `three_approved_history_valid` |
| `historical_checksum_mutation` | blocked | blocked | `historical_checksum_mismatch` |
| `identity_cutoff_mismatch` | blocked | blocked | `company_context_identity_mismatch` |
| `identity_ticker_mismatch` | blocked | blocked | `company_context_identity_mismatch` |
| `late_evidence` | blocked | blocked | `late_evidence` |
| `legacy_v1` | pass | pass | `legacy_checksum_verified`, `legacy_v1_rejected_for_synthesis` |
| `mutated_bundle_checksum` | blocked | blocked | `bundle_checksum_mismatch` |
| `newly_covered_valid` | pass | pass | `newly_covered_exception_valid` |
| `rights_degraded` | pass | pass | `headline_only_not_content_scored`, `summary_only_citations_resolve` |

## Execution

- Command: `.venv/bin/python -m pytest -q tests/evals/test_research_evidence_e0.py -k 'not retained'`
- Runtime: 1.63 seconds
- Provider calls: 0
- Model calls: 0
- Cache state: not_used
- Reported cost: $0.00

## Limitations

- E0 uses compact synthetic, rights-safe fixtures and does not make a live-provider claim.
- The result covers MVP-401 bundle, projection, compatibility, citation, cutoff, identity, rights, and history gates only.
- Later synthesis, policy, packet QA, journey, and public-release evaluations remain assigned to E1 through E4.

Decision 0011 is supported for the bounded MVP-401 projection and history contract. This result does not accept later synthesis, live collection, packet QA, or external-ready journey work.

Content checksum: `4fc3622669569636d7492ea4ca94e24760aaad2e99e36b99c84a541e2e75bbae`
