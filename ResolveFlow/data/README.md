# ResolveFlow Data Assets

All project data is UTF-8 JSON or JSONL. External dialogue corpora are not committed here. Their access and license status live in `sources/registry.yaml`.

| Directory | Contents | Use |
| --- | --- | --- |
| `raw/` | Empty boundary for approved external downloads | Never commit raw corpora by default |
| `processed/` | Reproducible intermediate samples | Cold-start and stress testing |
| `golden/` | Synthetic, human-review-pending Chinese gold data | General, Technical, Billing, Escalation, and cross-Agent regression evaluation |
| `knowledge/` | ResolveFlow-owned demo policy documents | Explicit RAG import only |
| `sources/` | Source registry and intent mapping | Traceability and license review |
| `splits/` | Stable dev/holdout ID manifests | Prevent prompt and rule leakage |
| `eval/` | RAG queries, accepted baseline, timestamped reports | Independent retrieval and end-to-end evaluation |

## Required JSONL fields

Every sample contains `id`, `source`, `source_license`, `language`, `text` or `turns`, `domain`, `expected_intent`, `expected_group`, `expected_primary_agent`, `expected_supporting_agents`, `entities`, `should_use_knowledge`, `should_escalate`, `must_include`, `must_not_include`, `review_status`, `reviewer`, `created_at`, and `synthetic`.

`billing_dialogs_zh.jsonl` additionally contains `turn_expectations`, aligned with `turns`. A turn expectation can set `memory_keywords`; the evaluator checks that a later response retains those earlier-topic terms.

## Rebuild and validate

Run `python scripts/generate_multi_agent_data.py` from the `ResolveFlow` directory to recreate all MVP data files and their stable `dev`/`holdout` manifests. `generate_billing_data.py` remains as a compatible alias. The generator produces demonstration data only; every Chinese gold row remains `pending_human_review` until an assigned reviewer approves it.

Run `python scripts/validate_data_assets.py` for the complete static gate: JSON shape, duplicate IDs, labels, sensitive identifiers, registered sources, stable split coverage, and holdout/template leakage. Run `python -m unittest discover -s tests -v` for the automated regression checks.

## Knowledge import

All `knowledge/*_policy_v1.json` files are deliberately marked as ResolveFlow demonstration policies. They are not automatically loaded into ChromaDB. Run `python scripts/run_rag_eval.py` to import them into an isolated local ChromaDB and verify top-3 retrieval before using any policy file in a demonstration environment.
