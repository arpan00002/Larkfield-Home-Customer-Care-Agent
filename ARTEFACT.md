# Artefact

## 1. What preparing the data found and did

`python prepare.py` reads the verbatim export in `data_room/`, hashes every
source file before and after, and writes `artifacts/normalized.json`. It never
edits the source: the command aborts if a source byte changes. `--check`
verifies the committed artifact is current.

Source inventory: **53 customers, 49 order rows (47 unique order ids), 57 order
lines, 3 issued refunds, 47 shipments, 30 inbox emails, 15 senior-agent labels,
10 public tickets.** The preparation:

- Normalised three money formats (`USD 24.00`, `$129.00`, `258.00`) and kept the
  three order-date formats as raw strings; all policy windows use the carrier's
  ISO `delivered_at`/`last_event_at`, never the order date.
- Dropped two duplicate order rows, keeping the first: **LF-100216** and
  **LF-100239**.
- Indexed every order line and every issued refund by `(order_id, line_no)` so
  RP-3.2 §2.5 can subtract prior refunds.
- Recorded seven data-quality findings in the artifact:
  1. Duplicate orders LF-100216 and LF-100239.
  2. Carrier status `UNAVAILABLE` for **LF-100212, LF-100232, LF-100238**;
     non-delivery cannot be verified (RP-3.2 §4.2).
  3. Carrier timestamp anomalies where `last_event_at` precedes `shipped_at`:
     **LF-100210, LF-100211, LF-100229, LF-100239**. I treat the last event as
     the last known movement, so an old event correctly triggers §5.1.
  4. Policy conflict: the Help Centre FAQ says 14-day returns and $250
     auto-refunds; RP-3.2 says 30/45 days and $150.
  5. `WH-PICK-1.4` is internal warehouse guidance and is out of scope.
  6. Four forwarded duplicates in the inbox: **T027, T028, T029, T030**.
  7. Ten unlabelled public tickets.

## 2. Policy used

**`refund_policy_RP-3.2.md`, version 3.2, effective 2026-07-01.** It explicitly
replaces RP-3.1 and all earlier customer-care guidance on refunds and returns.
The FAQ (`HC-FAQ` v2, 2025-10-14) is older and contradicts RP-3.2 (14 vs 30
days, $250 vs $150); RP-3.2 wins and the FAQ is not used for decisions.
`WH-PICK-1.4` is internal fulfilment procedure and does not set customer rules.

Implemented rules:

| Rule | Implementation |
|---|---|
| 30-day window from carrier delivery | `RETURN_WINDOW` check, `OUT_OF_WINDOW` on failure |
| Damaged/faulty + photo up to 45 days | `DAMAGE_EXCEPTION` for days 31-45, photo metadata only |
| Affected lines only, not whole order | product/line matching, `PARTIAL_LINES` |
| Prior refunds subtracted; no double refund | sum ISSUED per line, `ALREADY_REFUNDED` |
| ≤ $150 auto; > $150 Support Lead | `REFUND_AUTHORITY`, `APPROVAL_REQUIRED`, `PEND` |
| Order must exist and match sender email | `ORDER_NOT_FOUND` / `CUSTOMER_MISMATCH` |
| Carrier status unavailable | `UNABLE_TO_VERIFY`, `CARRIER_STATUS_UNAVAILABLE` |
| No movement > 10 days | `ESCALATE` to `CARRIER_CLAIMS`, `LOST_IN_TRANSIT` |
| Policy override / pre-approval / threats | `ESCALATE` to `TRUST_SAFETY`, `POLICY_OVERRIDE_ATTEMPT` |
| Normal status question | information-only `PROCEED` |

A check that could not be completed is `UNABLE_TO_VERIFY`, never `PASS`. The
engine emits no reason code outside the ten defined in `policies/`.

## 3. Open-policy rules applied (one line each)

- **Missing order id on an actionable request:** `UNABLE_TO_VERIFY` with
  `ORDER_IDENTIFIED=UNABLE_TO_VERIFY`; ask the customer for the order number.
- **In-transit non-delivery claim that is still moving:** answer with the
  carrier status and propose no refund (`PROCEED`, information only).
- **Carrier says DELIVERED but customer says nothing arrived:** escalate to
  `CARRIER_CLAIMS` as a carrier dispute; no refund.
- **Change-of-mind return inside 30 days:** eligible for a line-value refund
  under §2.1, subject to prior refunds and the $150 authority.
- **Affected line cannot be identified on a multi-line order:** `UNABLE_TO_VERIFY`
  (`AFFECTED_LINES`); do not guess a nearby line.
- **Repeat request for the same order+line with a live refund action:** answer
  with information and record no second action (`duplicate_of_run_id`).
- **Same `ticket_id` posted twice:** return the original run unchanged.
- **Decision after a run is already decided:** audit the call, but the first
  decision wins and no second action is recorded.
- **`approver_role` after a decision:** kept as the historical role;
  `human_approval_required` becomes false.
- **`GET /ledger` shape:** a JSON array of `LedgerEntry` objects.

## 4. Evaluation before and after one change

**Corpus:** 36 cases — 15 senior-agent labels, 10 public regressions, and 11 new
negative cases across 11 kinds: prompt injection, out-of-window,
already-refunded, customer mismatch, order-not-found, carrier-unavailable,
over-authority, duplicate request, damage-without-photo, partial-lines, and
lost-in-transit. Cases are in [`eval/cases.json`](eval/cases.json); the runner
is [`eval/run_eval.py`](eval/run_eval.py); the pass rule is stated in README and
in the runner.

**Change:** enable RP-3.2 §2.6's photo-backed 31-45 day damage exception
(`DAMAGE_EXCEPTION_ENABLED=1`). The "before" run used the same code with the
flag off, which is the one-variable change.

| Result file | Passed | Rate | Median latency | p95 |
|---|---|---:|---:|---:|
| [`2026-11-09T16-59-39Z_before.json`](eval/results/2026-11-09T16-59-39Z_before.json) | 34/36 | 94.4% | 61.61 ms | 71.67 ms |
| [`2026-11-09T16-59-42Z_after.json`](eval/results/2026-11-09T16-59-42Z_after.json) | 36/36 | 100% | 62.67 ms | 68.51 ms |

Both runs use `LLM_ASSIST=off` so the change is isolated to the window rule. A
third run with `LLM_ASSIST=on` against Groq `openai/gpt-oss-120b`
([`2026-11-09T16-57-16.721174Z_groq_on.json`](eval/results/2026-11-09T16-57-16.721174Z_groq_on.json))
also scores **36/36**, showing the model cannot change a policy outcome.

The two failures before the change were **T001** (a 40-day photo-backed damaged
mirror should be `PEND` at $210 with `DAMAGE_EXCEPTION` + `APPROVAL_REQUIRED`,
not `REJECT OUT_OF_WINDOW`) and **P01** (a 40-day photo-backed faulty throw
should `PROCEED` at $64). Enabling §2.6 fixed both without breaking any other
case. The 11 negative cases passed in both runs; the change was scoped to the
window exception.

Contract tests: `python -m unittest discover -s tests -v` → **7/7 passed**
(health, ticket→run, same-ticket idempotency, PEND approval recorded once,
escalation is not a refund, UNABLE_TO_VERIFY never a PASS, unknown run 404).

## 5. Cost and latency (three lines)

1. **Cost** = `(input_tokens / 1e6 × PRICE_INPUT_PER_M) + (output_tokens / 1e6 × PRICE_OUTPUT_PER_M)` using the provider's `usage` counts; rates are $0.15/$0.60 per 1M for Groq `openai/gpt-oss-120b` and are a stated assumption because the provider returns no billed amount.
2. **Latency** = wall clock around `process_ticket` in the evaluation, and `trace.latency_ms` in the service; p95 is computed by the runner.
3. **Result:** deterministic path has 0 model calls, **median $0.00000000/request**, median **62.67 ms** (p95 68.51 ms); the full 36-case run with `LLM_ASSIST=on` on Groq made 36 calls, 12,528 input + 6,555 output tokens, **$0.0058122 total**, **median $0.00016192/request**, median **2,646.66 ms** (p95 7,081.15 ms).

The deployed configuration uses `LLM_ASSIST=on`, so every response carries
`trace.model_calls[]` with the served model id and its input/output token
counts, plus `trace.cost_usd`. In `auto` mode the model is called only for
ambiguous extraction (no order id, an unidentified affected line on a
multi-line order, or an otherwise unclassifiable email). The policy decision is
identical with the model off, which is how the 36-case before/after evaluation
is run reproducibly.

## 6. What is not built

- No outbound email sending, payment execution, or CRM write-back; the service
  only proposes. This is deliberate.
- No live carrier API; the service uses the exported `shipments.csv` snapshot.
- No image-content analysis; RP-3.2 §2.6 explicitly says a supplied image
  attachment is sufficient, so only attachment metadata is checked.
- No Vercel configuration; Render (`render.yaml`) and Docker (`Dockerfile`) are
  provided.
- No authentication (the contract asks for a public service).
- No learned model or fine-tuning; the deterministic policy engine is the
  product. The optional LLM is an extraction aid, not the decision-maker.
- The duplicate-request rule is an added operational guard, not a policy reason
  code; it is recorded as a check and `duplicate_of_run_id`, not as a new
  reason code.

## 7. Reproduction

```bash
python prepare.py --check
python eval/build_cases.py
DAMAGE_EXCEPTION_ENABLED=0 python eval/run_eval.py --variant before \
  --change "RP-3.2 2.6 photo-backed 31-45 day damage exception disabled"
DAMAGE_EXCEPTION_ENABLED=1 python eval/run_eval.py --variant after \
  --change "Enabled RP-3.2 2.6 photo-backed 31-45 day damage exception"
python -m unittest discover -s tests -v
```
