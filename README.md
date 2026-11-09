# Larkfield Home Customer Care Agent

A public HTTP service that reads one customer-support email and returns the
Customer Care desk's decision: outcome, checks, reason codes, approval needs,
a draft reply, and a costed trace. It **proposes** actions; it never refunds,
sends, or changes anything itself.

The decision path is deterministic and policy-controlled
([`src/policy.py`](src/policy.py)). A language model is an optional extraction
assist only ([`src/llm.py`](src/llm.py)); it cannot override a policy result.

**Live service (review window):** <https://conversion-transition-president-paragraphs.trycloudflare.com>
(`GET /health` → `{"status":"ok"}`). The tunnel is tied to the running process;
`render.yaml` and the `Dockerfile` are the durable deployment path.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # then add GROQ_API_KEY (never commit .env)
python prepare.py                  # one command; builds artifacts/normalized.json
python -m uvicorn src.app:app --host 0.0.0.0 --port 8000
```

Open <http://localhost:8000/docs> for the generated OpenAPI UI. The service
reads `PORT` from the environment in production. It works with no model key at
all (`LLM_ASSIST=off`); the key only enables extraction assist.

Smoke test:

```bash
curl -s localhost:8000/health
curl -s -X POST localhost:8000/tickets \
  -H 'content-type: application/json' \
  --data @<(python -c "import json;print(json.dumps(json.load(open('data_room/inbox/public_tickets.json'))[0]))")
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | `{"status": "ok"}` |
| POST | `/tickets` | Process one ticket (body shape: `data_room/inbox/public_tickets.json`) |
| GET | `/runs/{run_id}` | Current run state, including a run waiting for a person |
| POST | `/runs/{run_id}/decision` | `{"decision":"approve"\|"reject","approver":"..."}` |
| GET | `/ledger` | JSON array of recorded actions |

`POST /tickets` is idempotent on `ticket_id`: sending the same ticket again
returns the same `run_id` and does not create a second action. The first
decision on a run wins; later decisions are audited but cannot record a second
action.

## Data preparation

The provided data room is vendored verbatim in `data_room/`. It is **never
edited**. One command reads it and writes `artifacts/normalized.json`:

```bash
python prepare.py
python prepare.py --check          # CI: fail if the artifact is stale
```

`prepare.py` hashes every source file before and after and aborts if a byte
changed. It normalises three money formats (`USD 24.00`, `$129.00`, `258.00`),
de-duplicates two order rows, indexes customers/orders/lines/shipments/refunds,
and records data-quality findings in the artifact and in
[`ARTEFACT.md`](ARTEFACT.md).

## Architecture

```
email -> deterministic extractor -> policy engine -> draft reply -> SQLite run + ledger
                    |                     |
             optional LLM hint     RP-3.2 clauses, checks, reasons
```

- [`src/parsing.py`](src/parsing.py): identifiers, product aliases, quantities,
  dates, intent and prompt-injection signals. Conservative: unknown facts
  become `UNABLE_TO_VERIFY`, never `PASS`.
- [`src/policy.py`](src/policy.py): the authority. Implements
  `refund_policy_RP-3.2.md`: 30/45-day windows, affected-line value, prior
  refunds, $150 authority, lost-in-transit and Trust & Safety escalation.
- [`src/llm.py`](src/llm.py): optional extraction assist (`LLM_ASSIST=off|auto|on`).
  Hints are validated against the data room; they cannot make a check pass.
- [`src/replies.py`](src/replies.py): templates built from the engine's facts, so
  amounts and statuses in a draft always match the decision.
- [`src/store.py`](src/store.py): SQLite runs/ledger with a unique
  `(run_id, action)` constraint.
- [`src/service.py`](src/service.py), [`src/app.py`](src/app.py): orchestration
  and the HTTP contract.

Policy documents used at runtime: **RP-3.2 v3.2**. The Help Centre FAQ
(14 days / $250) is superseded by RP-3.2's explicit replacement statement and
is not used for decisions. `WH-PICK-1.4` is internal warehouse guidance and is
out of scope.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8000` | HTTP port |
| `STORE_PATH` | `artifacts/runs.sqlite3` | SQLite run/ledger file |
| `LLM_ASSIST` | `auto` | `off`, `auto` (only ambiguous tickets), or `on` (always) |
| `GROQ_API_KEY` | — | Groq key. Set on the host or in the gitignored `.env`; never commit it. |
| `GROQ_MODEL` | — | Groq model id, e.g. `openai/gpt-oss-120b` |
| `GROQ_BASE_URL` | `https://api.groq.com/openai/v1` | Override the Groq endpoint |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | — | Generic OpenAI-compatible provider; wins over the Groq shortcut |
| `LLM_MAX_TOKENS` | `600` | Completion budget (Groq reasoning tokens count against it) |
| `LLM_REASONING_EFFORT` | `low` | Groq reasoning effort; empty omits the field |
| `LLM_JSON_MODE` | `0` | `1` adds `response_format: json_object` for non-Groq providers |
| `PRICE_INPUT_PER_M` | `0.10` | Cost assumption, USD per 1M input tokens |
| `PRICE_OUTPUT_PER_M` | `0.40` | Cost assumption, USD per 1M output tokens |
| `DAMAGE_EXCEPTION_ENABLED` | `1` | Eval flag for the RP-3.2 §2.6 before/after |

The running service uses **Groq `openai/gpt-oss-120b`** over its
OpenAI-compatible endpoint. `src/llm.py` reads `.env` if present but never
`opencode.json`, so the coding agent's build-time token cannot be used.
`.env` is gitignored; on Render set `GROQ_API_KEY` in the dashboard.

The provider does not return a billed amount, so `trace.cost_usd` is computed
from the returned `usage` token counts and the rates above ($0.15 / $0.60 per
1M for the configured model). The rates are an explicit assumption, not an
invoice.

## Added fields

The contract's models are in `data_room/schema/response_schema.py`. The service
returns every required field and these documented additions:

- `RunState.status`: `COMPLETED`, `AWAITING_APPROVAL`, `ESCALATED`, `APPROVED`,
  or `REJECTED`.
- `RunState.decisions[]`: audit of every decision call (`decision`, `approver`,
  `recorded_at`). Only the first one changes the ledger.
- `RunState.ledger[]`: the run's ledger entries (same shape as `GET /ledger`).
- `RunState.created_at`, `RunState.decided_at`.
- `RunState.duplicate_of_run_id`: set when an earlier live refund action covers
  the same order/line.
- `Trace.latency_ms`: wall-clock processing time.
- `Trace.assist_mode`: `off`, `auto`, or `on`.

`GET /ledger` returns a JSON array of `LedgerEntry` objects (the contract's
fields only).

## Evaluation

```bash
python eval/build_cases.py                 # regenerate eval/cases.json
python eval/run_eval.py --variant after \
  --change "Enabled RP-3.2 2.6 photo-backed 31-45 day damage exception"
python -m unittest discover -s tests -v    # HTTP contract tests
```

36 cases: the 15 senior-agent labels, 10 public regressions, and 11 new
negatives across 11 kinds (prompt injection, out-of-window, already-refunded,
customer mismatch, order-not-found, carrier-unavailable, over-authority,
duplicate request, damage-without-photo, partial-lines, lost-in-transit).

**Pass rule:** a case passes only if outcome, `human_approval_required`,
`approver_role`, expected reason codes, expected refund amount, and expected
ledger action/status all match; `no_refund` cases have no REFUND entry;
duplicate cases set `duplicate_of_run_id` and record no second REFUND; and all
contract invariants hold (valid outcomes/check results, `UNABLE_TO_VERIFY`
outcome has an `UNABLE_TO_VERIFY` check, PEND/ESCALATE have a human and role,
and any ledger amount appears in the draft reply). The rule is implemented in
[`eval/run_eval.py`](eval/run_eval.py).

Committed results:

- [`eval/results/2026-11-09T16-59-39Z_before.json`](eval/results/2026-11-09T16-59-39Z_before.json):
  **34/36 (94.4%)** — §2.6 photo exception disabled.
- [`eval/results/2026-11-09T16-59-42Z_after.json`](eval/results/2026-11-09T16-59-42Z_after.json):
  **36/36 (100%)** — §2.6 photo exception enabled.
- [`eval/results/2026-11-09T16-57-16.721174Z_groq_on.json`](eval/results/2026-11-09T16-57-16.721174Z_groq_on.json):
  **36/36 (100%)** with `LLM_ASSIST=on` on Groq — proves the model cannot
  change a policy outcome.

The change fixed `T001` (40-day photo-backed damage should PEND at $210, not
REJECT) and `P01` (40-day photo-backed faulty throw should PROCEED at $64).

## Cost and latency

Measured from the trace and wall clock; see [`ARTEFACT.md`](ARTEFACT.md) for
the three-line method and numbers.

- Deterministic decision path (evaluation, `LLM_ASSIST=off`): 0 model calls,
  median **$0.00000000/request**, median **62.67 ms** (p95 68.51 ms).
- Full 36-case run with `LLM_ASSIST=on` on Groq: 36 calls, 12,528 input +
  6,555 output tokens, **$0.0058122 total**, median **$0.00016192/request**,
  median **2,646.66 ms** (p95 7,081.15 ms).
- In the deployed configuration (`LLM_ASSIST=on`) every response carries
  `trace.model_calls[]` with `model: openai/gpt-oss-120b` and its token counts.
  If the provider call fails (bad key, rate limit, timeout), the trace still
  names the configured model with `input_tokens: 0, output_tokens: 0` and
  `cost_usd: 0.0`, and the deterministic decision is unaffected.
- In `auto` mode the model is called only when extraction is ambiguous; the
  policy result is identical with the model off.

## Deployment

- **Render:** `render.yaml` is a ready blueprint. Build:
  `pip install -r requirements.txt && python prepare.py`; start:
  `uvicorn src.app:app --host 0.0.0.0 --port $PORT`.
- **Docker:** `docker build -t larkfield-agent . && docker run -p 8000:8000 larkfield-agent`.
- **Current public URL:** <https://conversion-transition-president-paragraphs.trycloudflare.com>
  (Cloudflare quick tunnel to the local process for the review window).

## What is not built

- No outbound email sending, payment execution, or CRM write-back: by design,
  the service only proposes.
- No real carrier API: the service uses the exported `shipments.csv` snapshot.
- No image-content analysis: RP-3.2 §2.6 says a supplied image attachment is
  sufficient, so the service checks attachment metadata only.
- No Vercel configuration; Render and Docker are provided.
- No authentication: the contract specifies a public service.
