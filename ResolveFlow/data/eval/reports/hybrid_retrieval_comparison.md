# ResolveFlow Hybrid Retrieval Comparison

All values below come from deterministic local ChromaDB/BM25 runs on the
internal synthetic golden set. They are not production traffic metrics.

## Iterations

| Run | Query set | Hit@1 | Hit@3 | Hit@5 | MRR | P95 ms | Outcome |
|---|---:|---:|---:|---:|---:|---:|---|
| Baseline: vector candidates + in-candidate lexical score | 140 | 0.7857 | 0.9214 | 0.9643 | 0.8571 | 21.86 | Baseline |
| Round 1: independent vector/BM25 + equal RRF | 140 | 0.3857 | 0.7214 | 0.9143 | 0.5767 | 22.15 | Rejected |
| Round 2: reliability-weighted RRF | 140 | 0.8429 | 0.9357 | 0.9929 | 0.8957 | 25.59 | Passed core gates |
| Final on original queries with expanded corpus | 140 | 0.8071 | 0.9357 | 0.9571 | 0.8673 | 24.47 | No Hit@1/Hit@3/MRR regression |
| Final including robustness set | 160 | 0.8302 | 0.9434 | 0.9623 | 0.8831 | 23.82 | Passed all gates |

Final index build time was 1556.57 ms and the first post-build warmup query was
18.51 ms. Timed evaluation queries were not served from the MCP TTL cache.

The final robustness subset contains seven exact-identifier queries with
Hit@3=1.0 and one identifier-only no-match query with no-match accuracy=1.0.

## Failure analysis and response

Round 1 failed because `all-MiniLM-L6-v2` ranked many Chinese support queries
poorly, while BM25 usually placed the labelled document first. Equal RRF then
promoted documents that were mediocre in both channels above the strong BM25
hit. The failure was general to Chinese tokenization/model fit, not a handful of
query IDs.

Round 2 retained independent recall and RRF but calibrated route reliability:
English queries use equal weights, Chinese queries give more weight to the
Chinese unigram/bigram BM25 route, and opaque identifiers give the lexical
route the highest weight. This does not add raw BM25 and vector scores or
hard-code evaluation queries.

The final run added exact identifiers, mixed language, 7-vs-70, negation,
long-document chunk precision and a no-match case. Nine of 159 positive-query
cases remain outside Top-3; they are listed with vector/BM25/RRF diagnostics in
`latest_rag.json`. The required metrics already pass, so no third tuning round
was applied to avoid overfitting the internal set.

## Gates

- Hit@3 >= 0.90: passed (0.9434)
- MRR >= 0.80: passed (0.8831)
- Exact-identifier Hit@3 >= 0.95: passed (1.0)
- Original-query Hit@1/Hit@3/MRR no regression: passed
- P95 <= 1.5 x baseline (32.79 ms): passed (23.82 ms)
- Unit tests: passed (27/27)
