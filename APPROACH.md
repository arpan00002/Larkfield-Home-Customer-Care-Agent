# Approach

## Problem Summary
Larkfield needs consistent decisions, human stops and visible cost across 1,200 daily emails. Data room: 53 customers, 47 unique orders (49 rows), 3 refunds, 47 shipments. The FAQ (14 days/$250) conflicts with RP-3.2 (30/45 days/$150, which supersedes it); I use RP-3.2. WH-PICK-1.4 is warehouse procedure, out of scope.

## Technical Approach
A deterministic extractor ([src/parsing.py](src/parsing.py)) feeds a policy engine ([src/policy.py](src/policy.py)) mapping decisions to RP-3.2 clauses and emitting checks, reason codes, approval role, amount and a facts-derived draft ([src/replies.py](src/replies.py)). Groq `openai/gpt-oss-120b` ([src/llm.py](src/llm.py)) fills extraction gaps only; hints are data-room-validated and cannot turn a failed check into PASS. SQLite ([src/store.py](src/store.py)) stores runs and the ledger; first decision wins and a unique `(run_id, action)` constraint records an approved action once ([src/service.py](src/service.py)). The service never refunds or sends. Evaluation: 36 cases (15 labels, 10 public, 11 negatives, 11 kinds), pass rule and runner in [eval/run_eval.py](eval/run_eval.py); the §2.6 change moved 34/36 to 36/36.

## AI Tool Usage
Built with Cadra/OpenCode: explored the data room first, then iterated on policy, eval and contract tests. Prompts like "trace every outcome to an RP-3.2 clause" and "never let a hint flip a FAIL" shaped the design; the model stays outside the decision path.

## Trade-offs & Limitations
No outbound email, payment or CRM write-back. Carrier status is a CSV snapshot, not a live API. Photos are checked by metadata only. Open policy cases are in [ARTEFACT.md](ARTEFACT.md). Median cost $0.00016/request with Groq on every ticket (2.6 s); policy-only $0.00 (63 ms).
