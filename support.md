# Context Chat Backend — IT Support Guide

This guide equips IT support to operate, troubleshoot, and tune a Nextcloud Context Chat deployment using a pluggable retrieval backend. It stays close to upstream behavior and documents the knobs added by the R2R backend.

See also: `README.md` (architecture), `R2R-Integration.md` (deep dive), and comments in `context_chat_backend/backends/r2r.py`.

---

## 1) Conceptual Overview

### Embeddings
- Numerical vectors representing text (and optionally images). Similar content → nearby vectors.
- Used to search semantically rather than exact keyword match.

### RAG (Retrieval-Augmented Generation)
- Combine retrieval + LLM: find relevant chunks first, then have an LLM answer using those chunks as context. Improves factuality and reduces hallucinations.

### Graph / Graph‑RAG
- Build a knowledge graph (entities and relations) from documents. Retrieval can expand from a hit to related nodes/edges for richer context.
- R2R supports graph‑enhanced retrieval internally; CCBE consumes the normalized results via HTTP.

### Roles in this deployment
- Nextcloud: hosts the user UI and authentication. The Context Chat app calls the backend via AppAPI.
- Context Chat Client (Nextcloud app): chat UI and document actions; unmodified in this deployment (`/opt/context_chat`, for reference only).
- Context Chat Backend (CCBE): FastAPI service implementing the Nextcloud endpoints. Selects a retrieval backend by environment variable.
- Builtin backend: upstream default. Local vector DB + local LLM; no external dependency.
- R2R backend: external retrieval/graph service (SciPhi R2R). CCBE forwards ingestion, search and (optionally) answer generation to R2R.
- Hatchet (in R2R): R2R’s job orchestration/queuing for ingestion pipelines. Can be bypassed from CCBE if needed.

---

## 2) Architecture at a Glance

User → Nextcloud Context Chat (PHP app) → CCBE (Python) → selected RAG backend

- Selection: `RAG_BACKEND=builtin` (upstream behavior) or `RAG_BACKEND=r2r` (external R2R).
- API contract: the CCBE HTTP endpoints and response shapes remain the same for both backends.

Key paths (host):
- CCBE repo: `/opt/context_chat_backend`
- CC client repo (read‑only reference): `/opt/context_chat`
- R2R repo (read‑only reference): `/opt/R2R`
- CCBE persistent data: `/data/context_chat_backend/persistent_storage`
- CCBE logs (mounted into container): `/data/context_chat_backend/logs/ccb.log`
- R2R config (source): `/opt/ga_r2r.toml` → copied to `/data/r2r/docker/user_configs/ga/ga_r2r.toml` and mounted into the R2R container.

Containers and compose (host):
- CCBE compose: `/opt/ccbe-r2r-docker-compose.yml` (container name: `ccbe-r2r`)
- R2R compose: `/opt/r2r_docker_compose.yaml` (and env in `/opt/r2r.env`)

---

## 3) Day‑0/1 Operations

### Start/Stop CCBE
- Build + start: `docker compose -f /opt/ccbe-r2r-docker-compose.yml up -d --build`
- Logs (follow): `docker logs -f ccbe-r2r`
- Config file mounted as: `/app/config.yaml` inside the container (from `/opt/context_chat_backend/config.gpu.yaml` by default in compose).

### Select/Change Backend
- Env var in CCBE: `RAG_BACKEND=builtin` or `RAG_BACKEND=r2r`
  - File: `/opt/ccbe-r2r.env`
  - Apply change: `docker compose -f /opt/ccbe-r2r-docker-compose.yml up -d` (recreates container)
- Builtin path remains upstream‑compatible. Switching back to `builtin` restores the original behavior.

### R2R Basics
- Health: `curl -sS http://<r2r-host>:7272/v3/system/status`
- R2R config: `/data/r2r/docker/user_configs/ga/ga_r2r.toml` (container) ↔ `/opt/ga_r2r.toml` (authoritative source on host)
- Env file for R2R: `/opt/r2r.env` (API server, concurrency, services)

### Logs
- CCBE JSON log file (container): `/app/logs/ccb.log` → host: `/data/context_chat_backend/logs/ccb.log`
- CCBE also logs to stderr; use `docker logs ccbe-r2r` during live debugging.
- Correlation id: `X-Request-ID` is attached to every request/response and propagated to R2R calls (helps stitch log lines across systems).

---

## 4) What CCBE Endpoints Do (unchanged for users)

- `PUT /loadSources`: upload files for ingestion. With R2R, CCBE streams each file to R2R with per‑user collection filtering.
- `POST /docSearch`: retrieve relevant document references for a query. With R2R, this wraps R2R retrieval results.
- `POST /query`: get an answer + sources. With R2R, CCBE prefers R2R’s `generated_answer` when present, else falls back to the local LLM.
- Access management: `/updateAccess`, `/updateAccessDecl`, `/deleteSources`, `/deleteUser`, etc. CCBE maps these to R2R collections when using R2R.

Note: Most routes are AppAPI‑protected. Exercise them from the Nextcloud app, or use signed requests if calling directly.

---

## 5) Configuration Knobs

### CCBE (container `ccbe-r2r`)
- `RAG_BACKEND`: `builtin` (default) or `r2r`.
- `CC_CONFIG_PATH`: path to config inside container (compose mounts `/app/config.yaml`).
- `NEXTCLOUD_URL`, `APP_SECRET`: AppAPI status reporting.
- `APP_HOST`, `APP_PORT`: bind address (defaults are fine for container networking).
- `R2R_BASE_URL`: e.g. `http://r2r:7272`.
- `R2R_API_KEY` / `R2R_API_TOKEN`: auth for R2R.
- `R2R_HTTP_TIMEOUT`: seconds (defaults to 300 for long queries/uploads).
- `R2R_RUN_WITH_ORCHESTRATION`: `true` to enqueue in Hatchet, `false` to ingest directly (useful while troubleshooting Hatchet backlog).
- `R2R_USE_GENERATED_ANSWER`: `true` to prefer R2R’s final answer; set `false` to force CCBE’s local LLM.
- `R2R_EXCLUDE_EXTS`: comma‑separated extensions to skip before upload (mirror R2R exclusions, e.g. `.xls,.xlsx,.png`).

Config file (`/opt/context_chat_backend/config.gpu.yaml` → mounted as `/app/config.yaml`):
- `embedding_chunk_size`: default 1000 chars.
- `doc_parser_worker_limit`: number of in‑process document parser workers (affects builtin mode only).
- `uvicorn_workers`: CCBE worker processes.

### R2R (see `/opt/r2r_docker_compose.yaml` and `/opt/r2r.env`)
- Concurrency (examples; actual values depend on hardware):
  - `UNSTRUCTURED_NUM_WORKERS`: CPU workers for document parsing.
  - `R2R_API_WORKERS`, `R2R_API_LIMIT_CONCURRENCY`, `R2R_API_KEEPALIVE`, `R2R_API_BACKLOG`: API server throughput.
  - `ga_r2r.toml`:
    - `[orchestration] ingestion_concurrency_limit`
    - `[embedding] concurrent_request_limit`
    - `[completion] concurrent_request_limit`
  - Hatchet/RabbitMQ QoS: `/data/r2r/hatchetconfig/server.yaml` → `msgQueue.rabbitmq.qos`
- File type/size rules: `ga_r2r.toml` `[app.max_upload_size_by_type]`. Keep CCBE `R2R_EXCLUDE_EXTS` aligned to avoid unnecessary uploads.

---

## 6) Performance Tuning Cheatsheet

Goal: keep user queries responsive while scans and large ingestions run.

- Start conservative; increase gradually while watching CPU, RAM, and queue depth.
- CCBE
  - `uvicorn_workers`: 2–4 for busy sites.
  - Keep CCBE logging at `INFO` in production (JSON file log is already enabled).
- R2R
  - Raise `R2R_API_WORKERS` and `R2R_API_LIMIT_CONCURRENCY` as CPU allows.
  - Balance `ingestion_concurrency_limit` vs. `UNSTRUCTURED_NUM_WORKERS`.
  - If Hatchet delays step starts, temporarily set `R2R_RUN_WITH_ORCHESTRATION=false` in CCBE to ingest directly; revert when the queue is healthy.
- Exclusions
  - Set `R2R_EXCLUDE_EXTS` in CCBE to match blocked types in `ga_r2r.toml` to reduce load and noise.

Validation under load:
- `curl -sS http://<r2r-host>:7272/v3/system/status` stays fast.
- CCBE `/docSearch` latency is stable; logs show `R2R request completed` times within expectation.

---

## 7) Troubleshooting

Symptom → checks → likely fix:

- Upload fails quickly with “disallowed extension” or 413
  - Check R2R logs and `ga_r2r.toml` limits. Align CCBE `R2R_EXCLUDE_EXTS`.
  - CCBE will log and treat excluded files as skipped so scans continue.

- Long delays before ingestion starts
  - Hatchet queue under pressure. Check Hatchet dashboard/logs.
  - Reduce concurrency briefly or set `R2R_RUN_WITH_ORCHESTRATION=false` in CCBE, then restore.

- `docSearch` returns nothing but the corpus exists
  - Verify the querying user is in the right collection. With R2R, CCBE filters by `collection_ids` overlapping the `userId`.
  - Confirm documents show `ingestion_status=success` via R2R API/dashboard.

- Short/low‑quality answers while using R2R
  - Ensure `R2R_USE_GENERATED_ANSWER=true` so CCBE returns R2R’s final answer.
  - If disabled, CCBE falls back to the local LLM (which may be conservative).

- 401/403 calling R2R
  - Missing/incorrect `R2R_API_KEY`/`R2R_API_TOKEN`. Update CCBE env.

- Timeouts on large uploads/queries
  - Raise `R2R_HTTP_TIMEOUT` in CCBE; check R2R API concurrency and Unstructured workers.

- Repeated retries of the same failed document
  - Use the maintenance script to prune the first‑pass upsert cache:
    - `python3 context_chat_backend/scripts/prune_r2r_upsert_cache.py --dry-run`
    - Then run without `--dry-run` if entries need removal. See script header for envs.

Log tips:
- Use `X-Request-ID` to correlate CCBE lines:
  - `http request` → `R2R request` (curl) → `R2R request completed` → response handler.

---

## 8) Health & Validation

- R2R health: `GET /v3/system/status`.
- CCBE startup tests (when `RAG_BACKEND=r2r`): CCBE runs a small document lifecycle against itself (see `context_chat_backend/startup_tests.py`), echoed as runnable curl commands in the CCBE logs.
- Count indexed docs:
  - CCBE with R2R: `POST /countIndexedDocuments` returns a per‑provider map; in R2R mode, this is derived from `list_documents()`.

---

## 9) Rollback and Safety

- To revert to upstream behavior: set `RAG_BACKEND=builtin` in CCBE and recreate the container. No other changes required.
- All R2R‑specific logic is contained in backend code paths under `context_chat_backend/backends/`. Upstream endpoints and shapes are preserved.

---

## 10) Quick Reference

Where to look first:
- Config: `/opt/ccbe-r2r.env`, `/opt/context_chat_backend/config.gpu.yaml`, `/opt/r2r.env`, `/opt/ga_r2r.toml`
- Logs: `/data/context_chat_backend/logs/ccb.log`, `docker logs ccbe-r2r`
- Code references: `context_chat_backend/controller.py`, `context_chat_backend/backends/r2r.py`
- Docs: `README.md`, `R2R-Integration.md`

Common commands:
- Recreate CCBE: `docker compose -f /opt/ccbe-r2r-docker-compose.yml up -d --build`
- Tail CCBE logs: `docker logs -f ccbe-r2r`
- R2R health: `curl -sS http://<r2r-host>:7272/v3/system/status`

If you need more depth, open `R2R-Integration.md` for payload shapes, normalization rules, and tuning examples.

---

Maintainers: keep this document aligned with upstream changes and the adapter’s behavior. If you add new backends, prefer generic descriptions here and push specifics into backend‑local docs.

