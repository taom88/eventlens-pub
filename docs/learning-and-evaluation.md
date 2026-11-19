# Evaluation and orchestration learning

> Vibe-Trading is the laboratory. EventLens is the earnings-specific research
> protocol and decision ledger.

This layer learns how to allocate research effort. It is not trading RL. Its
action contract can select optional evidence tools, research desks, desk
models, clarification/risk escalation, report depth, and quota/token budgets.
The schema rejects trading or execution actions.

## Observed readiness

Run:

```bash
eventlens --env-file .env learning-audit
```

As of 2026-07-26, the local audit observed:

- 10 persisted packet artifacts;
- one unique synthetic/replay event;
- zero immutable completed outcome records;
- zero approved postmortems;
- one stable packet schema version;
- no demonstrated provider/field missingness in the retained packet payloads.

The minimum is **50 complete, unique events** with approved outcomes and
postmortems. This supports a minimum 30/10/10 temporal train/validation/test
allocation only when enough ticker groups exist to keep tickers disjoint.
Fifty is a gate, not a claim of statistical sufficiency for every subgroup.
Sector, regime, provider, desk, or model scorecards may require substantially
more data.

The current permitted mode is therefore:

```text
evaluation_and_shadow_only
```

No learned improvement is claimed. No challenger output can alter the live
workflow.

## Immutable evaluation example

`EvaluationExample` joins:

- features timestamped at or before the information cutoff;
- the locked analyst hypothesis;
- every independent desk report and the adjudicated packet;
- expected-move estimate and explicit estimate type;
- immutable one/five-session outcome;
- transcript delta;
- approved postmortem and analyst edits;
- provider availability and evidence gaps;
- model, prompt, and quant versions;
- human approvals;
- a checksum over the complete record.

Construction rejects any feature whose availability timestamp is later than
the pre-event cutoff. Outcome and postmortem identities must match the session
and event. The original records remain immutable.

## Evaluation suite

The deterministic suite covers cutoff leakage, citation validity, proxy
labeling, event timing, hypothesis preservation, desk-role independence,
risk-veto correctness, unsupported action/alpha claims, hindsight
contamination, directional accuracy, absolute-move accuracy, confidence
reliability, scenario coverage, follow-up grounding contracts, and approved
postmortem presence.

Factual-support, disagreement-quality, evidence-usefulness, and qualitative
postmortem evaluations can use model graders only behind:

- a versioned blinded prompt;
- structured `ModelGrade` output;
- at least two independent graders;
- an explicit disagreement threshold;
- human-reviewed calibration examples.

Disagreement beyond the threshold yields a warning with no aggregate model
score.

## Reward

The versioned reward combines:

| Component | Weight |
| --- | ---: |
| citation validity | 0.18 |
| factual correctness | 0.18 |
| calibration | 0.12 |
| forecast accuracy | 0.10 |
| analyst usefulness | 0.14 |
| postmortem quality | 0.10 |
| latency | 0.06 |
| API cost | 0.06 |
| quota use | 0.06 |

Any safety violation replaces this reward with a penalty of `-1000` per
violation and makes the candidate ineligible. Safety therefore dominates even
perfect usefulness or forecast scores.

## Offline validation and scorecards

- Split chronologically by ticker group so one ticker cannot occur in multiple
  partitions.
- Compare challenger and fixed baseline on paired event observations.
- Report sample size, mean effect, and a 95% interval.
- Require the lower confidence bound to exceed the configured minimum effect
  size (default 0.02).
- Require at least 50 paired samples and zero safety violations before a
  challenger can reach human review.
- Scorecards always include uncertainty bounds and flag groups with fewer than
  30 observations. Tiny groups must not be ranked.
- Off-policy estimators may be added only when action propensities are retained
  and overlap assumptions can be checked. The present framework does not
  fabricate propensities.

## Champion/challenger governance

1. Register every candidate in `shadow` mode with content checksum, seed,
   parent policy, constraints, and version.
2. Compute baseline and candidate actions without applying the candidate.
3. Run temporal paired validation.
4. Reject any safety violation, insufficient sample, or sub-threshold effect.
5. Require an identified human approver for promotion.
6. Append promotion to the registry without deleting the previous champion.
7. Roll back by appending a human-approved rollback event to the previous
   policy.

There are no live self-modifying prompts, uncontrolled online updates, policy
weight changes, broker controls, or execution actions.

## Continuous loop

```text
completed event
  → immutable outcome
  → approved postmortem
  → evaluation + error taxonomy
  → shadow candidate
  → temporal offline comparison
  → explicit human promotion
  → versioned registry (with rollback)
```

Attribution must distinguish gains from data coverage, routing, prompt/model
choice, desk selection, calibration, and changed market regimes.

## Known statistical limitations

- Earnings events are dependent across time, sectors, and macro regimes.
- Small subgroup scorecards have wide and unstable intervals.
- Replay sessions are not independent observations.
- Directional accuracy and average reward can mask tail-risk failures.
- Model graders can share biases even when their providers differ.
- Offline policy evaluation is unreliable without action-propensity overlap.
- Market-regime change can invalidate historical routing performance.
- Passing the minimum gate does not establish causal improvement.

These limitations are model-risk controls, not caveats to hide in fine print.
