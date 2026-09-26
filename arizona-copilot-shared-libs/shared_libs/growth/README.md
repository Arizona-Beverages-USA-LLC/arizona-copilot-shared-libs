# shared_libs.growth — "Where We're Winning" shared core

Every Arizona agent that produces a 360 carries a standard **Where We're
Winning** section: a systematic census of positives (growth, new business,
like-for-like, relative wins, momentum, profit, recovery, sell-through) found by
a deterministic scan with statistical guardrails and graded evidence. This
package is the part that is **identical everywhere**; each agent owns the part
that must differ.

| Shared here | Owned by each agent |
|---|---|
| `stats` — growth, log-ratio test with over-dispersion, empirical-Bayes shrinkage, Benjamini–Hochberg q-values, LMDI-I, PVM with new/discontinued bars, SKU families | the **scanner**: its SQL, data definitions (scope filters, units), slicings, floors, and which classes its data can measure |
| `model` — `Finding`, coverage `Ledger`, the nine-class taxonomy, grade vocabulary | the **prompt section**, written in the agent's own units and template (VIP section discipline, S&M ten-section 360, IRI lenses, Kroger playbooks) |
| `grading` — FDR filter, qualitative GRADE-style grades, dedupe, ranking | the on/off flag `ENABLE_GROWTH_SCAN` (default on; off = the agent's prior instruction exactly) |
| `contract` — `PROMPT_CONTRACT` (shared vocabulary, ~15 lines) and `build_result` (shared output keys) | |

## Grades (qualitative)
Start at Strong; drop one level per failed check (not confirmed by a second data
layer · held in one window only · q > 0.05, or −2 if q > 0.10 · each caution flag),
floored at Emerging. A single-source agent is usually capped at **Moderate**
because only the Master Sales Analyst can confirm across layers. Internal
weights (1.0 / 0.6 / 0.3) rank findings; they are never shown as probabilities.

## Research basis
Master Sales Analyst repo: `docs/research/where-were-winning.md` (industry
methods per data source, verification of claims) and `docs/GROWTH_SCAN.md`
(method, defaults and their justification).

## Vendoring
Additive: copy `shared_libs/growth/` into `<agent_repo>/shared_libs/growth/`.
It depends on nothing else in `shared_libs` (standard library only), so it does
not require re-syncing the rest of an agent's vendored tree.
