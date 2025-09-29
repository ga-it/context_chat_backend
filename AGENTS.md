# Agent Playbook

This fork of Nextcloud’s Context Chat Backend (CCBE) is the control plane for a pluggable Retrieval-Augmented Generation stack. Our primary extension is the **R2R backend** that can be toggled via environment variables without disturbing the upstream “builtin” implementation. Use this document as the quick-reference for any further work.

## Repositories & Data Locations
- `/opt/context_chat_backend` – this project. Keep diffs versus upstream tight; prefer adapter hooks and self-contained modules (e.g. `context_chat_backend/backends/r2r.py`).
- `/opt/context_chat` – Nextcloud client mirror (read-only reference).
- `/opt/R2R` – SciPhi R2R server mirror (read-only reference).
- Persistent runtime state lives under `/data/context_chat_backend` (CCBE) and `/data/r2r` (R2R).

## Development Principles
- Preserve the original CCBE API surface. All routing (`controller.py`) should remain upstream-compatible, with backend selection driven solely by `RAG_BACKEND`.
- Push R2R-specific logic into the backend adapter or new helper modules. When upstream files must change, keep edits minimal and well-commented.
- Honour reproducibility: small, focused commits; run `pre-commit run --files …`, `ruff`, `pyright`, and `pytest` on touched components before shipping.
- If unexpected changes appear in the tree, stop and confirm direction with the owner.

## R2R Backend Expectations
- CCBE chooses the backend at startup. `RAG_BACKEND=builtin` restores the original PGVector flow; `RAG_BACKEND=r2r` activates our adapter.
- Critical environment toggles:
  - `R2R_BASE_URL`, `R2R_API_KEY`, `R2R_API_TOKEN` – auth and routing.
  - `R2R_HTTP_TIMEOUT`, `R2R_MAX_INFLIGHT_UPSERTS`, `R2R_HEALTH_MAX_RTT_MS` – throughput safeguards.
  - `R2R_SEARCH_LIMIT` – hard cap on chunks returned per `/v3/retrieval/rag`. When set (>0) it overrides `ctxLimit` from the client and injects a matching `search_settings.limit`, ensuring long-form sources (e.g. books) stay reachable without modifying Nextcloud or the R2R repo.
  - `R2R_USE_GENERATED_ANSWER` – opt-out of R2R LLM completions if local fallback is preferred.
- The adapter logs every external call with a runnable curl and honours retry semantics (`RetryableBackendBusy` → HTTP 503 + `cc-retry`).

## Documentation Duties
- Update `docs/ccbe_r2r_mapping.md`, `docs/endpoints.md`, `R2R-Integration.md`, and this file whenever behaviour or configuration changes.
- Reference material:
  - `PRD.md` – product context and goals.
  - `R2RAPIEndpointsSummary.txt` → look-ups into `r2rdocs.txt` for detailed API semantics.

## Testing & Validation
- Use the docker-compose stack (`/opt/ccbe-r2r-docker-compose.yml`) for integrated validation with the mounted configs (`/opt/ga_r2r.toml` → `/data/r2r/docker/user_configs/ga/ga_r2r.toml`).
- Tail CCBE logs at `/data/context_chat_backend/logs/ccb.log` to confirm R2R answers are flowing and that chunk counts match expectations.
- For ingestion health, use the helper scripts in `context_chat_backend/scripts/` (e.g. `prune_r2r_upsert_cache.py`).

Stay disciplined about separation: the CCBE fork defines the adapter surface; Nextcloud client and R2R upstream should remain untouched unless explicitly required.
