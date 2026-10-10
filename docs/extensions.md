# Optional extensions (M6): decision record

The plan says each extension must justify its gain with a concrete case. None does yet; this records why and what
evidence would change the decision.

| Extension | Status | Why not now | What would justify it |
|---|---|---|---|
| Hybrid retrieval (embeddings + pgvector + rank fusion) | not built | Lexical retrieval already reaches Recall@3 = 1.0 on both dev and test (MRR@3 0.90 / 0.85). Error codes and the fixed category expansion bridge French reports to English manuals. There is no failing case to fix, so a comparison would only show "no gain" at the cost of an embedding model (no local inference here; Groq exposes no embedding model) | Reports without error codes whose wording does not overlap the manuals (paraphrases, other languages), *measured* as missed relevant sections on a reviewed dataset |
| OpenTelemetry tracing | not built | A correlation id is propagated through n8n, the API, the worker and the ticket service and logged as JSON by each (`tests/workflow::test_correlation_id_reaches_the_ticket_service`). That answers "which calls belong to this incident". It is **not** distributed tracing: no spans, no timing tree | A latency question the logs cannot answer (e.g. where the p95 dispatch time goes), or more services than one person can follow by grepping a correlation id |
| Real external connector in a sandbox | not built | All demo actions must stay local (plan §2). The two local connectors already show the two guarantee classes that matter (idempotent by-key API vs SMTP) | A provider with a documented sandbox whose idempotency/consistency differs from the simulator's, to test reconciliation against eventual consistency |
