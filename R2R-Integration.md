# Context Chat Backend — R2R Integration Guide

This document captures what we learned integrating the Context Chat Backend (CCBE) with an external R2R retrieval/RAG service and making it work reliably in Nextcloud Context Chat.

For operations and troubleshooting, see the support guide: [support.md](support.md)

## Overview

- CCBE supports two RAG modes controlled by `RAG_BACKEND`:
  - `builtin`: local vector DB + local LLM
  - `r2r`: external R2R service for retrieval and (optionally) generation
- With `r2r`, CCBE calls R2R’s `/v3/retrieval/rag` endpoint to get:
  - `generated_answer`: final LLM answer from R2R
  - `search_results`: chunk hits used to build source references shown in the UI

Key files:

- `context_chat_backend/backends/r2r.py`: R2R HTTP client
- `context_chat_backend/controller.py`: FastAPI app and routes
- `context_chat_backend/chain/query_proc.py`: prompt pruning and token budgeting

## API Usage and Payload Shapes

R2R endpoints used:

- Health/status: `GET /v3/system/status`
- RAG: `POST /v3/retrieval/rag` (primary)
- Search (internal checks): `POST /v3/retrieval/search` (hash/title lookup)

RAG request (minimal):

```json
{
  "query": "...",
  "top_k": 20,
  "filters": {"collection_ids": {"$overlap": ["<userId>"]}}
}
```

RAG response (relevant fields):

```json
{
  "results": {
    "generated_answer": "...",
    "search_results": {
      "chunk_search_results": [
        {
          "text": "...",
          "metadata": {
            "title": "...",
            "source": "files__default:8059480",
            "filename": "files__default:8059480",
            "provider": "files__default",
            "modified": "1718822863",
            "sha256": "..."
          }
        }
      ]
    }
  }
}
```

We tolerate both list and nested forms for hits:

- `results.search_results.chunk_search_results` (preferred)
- Or `results.search_results` as a list

## Controller Flow

Route: `POST /query`

1. When `RAG_BACKEND=r2r` and `useContext=true`, CCBE calls `R2rBackend.rag()` (R2R `/retrieval/rag`).
2. If `generated_answer` exists, CCBE returns it directly to the client.
3. Else it falls back to local LLM prompting: builds a context from hits and invokes the configured local LLM.
4. In both cases, CCBE extracts and returns source references for the UI.

Route: `POST /docSearch`

1. Uses `R2rBackend.search()` (backed by `/retrieval/rag`) to get chunk hits.
2. Emits a list of `{ sourceId, title }` in the format Nextcloud expects.

## Source ID Normalization (Nextcloud expectations)

Nextcloud’s Context Chat expects provider-style identifiers in the UI and linking layer. We normalize R2R metadata into a canonical form:

- Prefer `metadata.source`, then `metadata.filename`, then any `source_id`/`sourceId`.
- Normalize to `"<provider>: <id>"` with a space after `:` (e.g., `files__default: 8059480`).
- If the provider looks like `files_default` (single underscore), convert to double underscore on first segment: `files__default`.
- Deduplicate by `(sourceId, title)` for `/docSearch` and by `sourceId` for `/query` sources.

Implementation references:

- `/query` source building: `context_chat_backend/controller.py`
- `/docSearch` results shaping: `context_chat_backend/controller.py`

## Key Changes and Fixes

1. Use R2R’s generated answer:
   - Added `R2rBackend.rag()` which returns `{ answer, hits }` by calling `/v3/retrieval/rag`.
   - Updated `/query` to prefer `generated_answer` from R2R, falling back to local LLM only if the answer is missing.

2. Token counting fallback:
   - Some local LLMs (e.g., Nextcloud TextToText) don’t implement `get_num_tokens`.
   - `get_pruned_query()` now uses a safe heuristic fallback (~4 chars/token) to avoid crashes before “invoking llm”.

3. FastAPI startup crash (ExceptionMiddleware):
   - Starlette only accepts exception handlers for subclasses of `Exception`.
   - Python 3.11’s `BaseExceptionGroup` derives from `BaseException`, so unconditional registration caused an `AssertionError` at startup.
   - We now conditionally register the handler only when allowed; otherwise we skip it.

4. Resilient uploads for transient R2R errors:
   - When `POST /loadSources` is running with `RAG_BACKEND=r2r`, per-source uploads now handle transient R2R failures (HTTP 5xx, 408, 429, or network timeouts) without failing the entire batch.
   - The response includes `{"loaded_sources": [...], "sources_to_retry": [...]}` so the client can retry only the affected items.
   - This keeps scans progressing even if the Hatchet queue is briefly backpressured or the API momentarily spikes.

5. Orchestration toggle (bypass Hatchet when needed):
   - `R2R_RUN_WITH_ORCHESTRATION` (default `true`) controls whether CCBE asks R2R to enqueue ingestion through Hatchet or ingest directly.
   - Set `R2R_RUN_WITH_ORCHESTRATION=false` to bypass Hatchet temporarily if you see step-start delays or scheduler errors; switch back to `true` when the queue is healthy.

## Configuration

Environment variables for R2R:

- `RAG_BACKEND=r2r`
- `R2R_BASE_URL=http://<host>:<port>` (default `http://127.0.0.1:7272`)
- `R2R_API_KEY=<key>` (sends `X-API-Key`)
- `R2R_API_TOKEN=<token>` (sends `Authorization: Bearer …`)
- `R2R_HTTP_TIMEOUT=<seconds>` (default `300`)

### Backpressure and Timeouts

- CCBE performs a light queue health pre-check before heavy R2R operations when `RAG_BACKEND=r2r`. If the backlog is high (messages_ready per consumer above threshold) or an absolute cap is exceeded, CCBE returns `503` with header `cc-retry: true` instead of timing out.
- Configure via environment variables in the CCBE container:
  - `QUEUE_HEALTH_URL` (e.g., `http://<r2r-host>:15673`) plus optional `QUEUE_HEALTH_USER`, `QUEUE_HEALTH_PASSWORD` (RabbitMQ mgmt API)
  - `QUEUE_BACKLOG_PER_CONSUMER_OK` (default `20`) and `QUEUE_BACKLOG_ABSOLUTE_OK` (default `0` disables)
  - `QUEUE_MAX_WAIT_SECONDS` (default `0`): how long to wait for the queue to drain before returning 503
  - `QUEUE_HEALTH_POLL_INTERVAL` (default `2.0`), `QUEUE_HEALTH_HTTP_TIMEOUT` (default `3.0`)
  - Optional: `QUEUE_HEALTH_FAIL_CLOSED=true` to treat health endpoint errors as busy (force `503`)

When backpressure triggers, CCBE’s controller translates this into a retryable `503` for `/loadSources` so the Nextcloud scanner automatically retries later.

Caller-side saturation (no upstream diffs):

- In addition to RabbitMQ/engine health, CCBE now uses lightweight, caller-side signals to avoid overloading R2R’s synchronous request path:
  - `R2R_MAX_INFLIGHT_UPSERTS`: maximum concurrent R2R document creates from CCBE before returning a retryable `503`.
  - `R2R_HEALTH_MAX_RTT_MS`: if the EWMA of recent R2R request latencies exceeds this value, CCBE returns a retryable `503` for new ingestions.
  - `R2R_MAX_WAIT_SECONDS` (or `QUEUE_MAX_WAIT_SECONDS`): optional grace period to wait for conditions to improve before responding.
  
These signals live entirely in the R2R backend adapter (`backends/r2r.py`) and are exposed to the app via a generic `RetryableBackendBusy` exception that is mapped to `503 cc-retry` centrally in `main.py`.

Excluded extensions (graceful skip):

- R2R may be configured to reject certain file types via its user config (for example, `ga_r2r.toml`, typically mounted at `/data/r2r/docker/user_configs/ga/ga_r2r.toml` inside the R2R container).
- When R2R rejects a file due to such rules (e.g., returning an error like "File size exceeds maximum of 0 bytes for extension 'xlsx'."), CCBE now treats this as a benign "scan ok" and skips the file without failing the batch. The response still includes a loaded source identifier so the client advances to the next file.
- Optional: you can proactively mirror these exclusions in CCBE via `R2R_EXCLUDE_EXTS` (comma-separated extensions like `.xls,.xlsx`), which avoids uploading excluded files in the first place.

Other environment variables commonly used by CCBE:

- `CC_CONFIG_PATH` — path to CCBE config file
- `NEXTCLOUD_URL`, `APP_SECRET` — for AppAPI status reporting
- `APP_HOST`, `APP_PORT` — used by optional startup tests

## Logging and Diagnostics

CCBE logs notable milestones with a request correlation id (`X-Request-ID`):

- `http request` and `http response`
- `R2R request` (as a runnable curl) and `R2R request completed`
- `R2R response body` (for debugging)
- `/query` flow:
  - `received query request`
  - `backend search hits`
  - `context retrieved` (when falling back to local LLM)
  - `invoking llm` and `llm output`
  - `query response` (shows `sources` and truncated `output`)
- `/docSearch` flow:
  - `docSearch hits` (raw hits)
  - `docSearch map` (normalization details)
  - `docSearch results` (final payload to client)

Tip: If the log shows `context retrieved` but not `invoking llm`, the issue is likely in prompt pruning/token counting (now mitigated with the fallback).

For R2R load management and caching, look for:
- `Seeding export progress` and `Seeded upsert cache from export` during cache seeding
- `Upsert skip windows` on each upsert attempt (shows configured windows)
- `Quick-skip all ...` when a file is skipped entirely within the “skip-all” window
- `Quick-skip meta ...` when only metadata/dedup checks are skipped for unchanged content

## Troubleshooting

- Startup AssertionError in `ExceptionMiddleware`:
  - Caused by registering a handler for `BaseExceptionGroup` (not a subclass of `Exception` in Py3.11).
  - Fixed: conditional registration to avoid the assertion.

- Short LLM answers while on R2R:
  - Cause: CCBE was still invoking the local Nextcloud LLM (which uses conservative defaults) instead of R2R’s generated answer.
  - Fix: Use `R2rBackend.rag()`; prefer R2R `generated_answer` and only fall back to local LLM if it’s missing.

- “No documents retrieved” errors:
  - Ensure documents are indexed and in collections mapped to the querying `userId`.
  - Filters: `filters.collection_ids.$overlap` must include the user’s collection name/ID.

- R2R rejects a file due to excluded type/size:
  - CCBE logs an info message and returns a benign identifier, effectively treating the file as "skipped" so the client proceeds.
  - To keep behavior aligned with R2R, set `R2R_EXCLUDE_EXTS` in CCBE to match the extensions excluded in `ga_r2r.toml`.

- Transient 5xx during ingestion with Hatchet warning like “THE TIME TO START THE STEP RUN IS TOO LONG…”:
  - This originates in Hatchet (axe icon). It means step scheduling is delayed, often due to temporary CPU/IO pressure or queue backpressure.
  - Mitigations (no code changes): tune worker counts and RabbitMQ QoS as in the Performance Tuning section; ensure `ingestion_concurrency_limit` and Unstructured worker count are set sensibly for your machine.
  - CCBE will now return `sources_to_retry` for those items so the client can retry without failing the whole batch.

## Maintenance Scripts

- Prune failed ingestions from R2R upsert cache
  - Path: `context_chat_backend/scripts/prune_r2r_upsert_cache.py`
  - Purpose: remove entries from the first‑pass upsert cache (at `/data/context_chat_backend/persistent_storage/r2r_upsert_cache.json`) when their R2R `ingestion_status` is `failed` (or the document no longer exists). This lets CCBE re‑upload them on the next scan.
  - Usage:
    - `python3 context_chat_backend/scripts/prune_r2r_upsert_cache.py --dry-run`
    - Env (optional): `R2R_BASE_URL`, `R2R_API_KEY` and/or `R2R_API_TOKEN`, `R2R_UPSERT_CACHE_PATH`
    - Example: `R2R_BASE_URL=http://192.168.0.86:7272 python3 context_chat_backend/scripts/prune_r2r_upsert_cache.py`

- `context retrieved` but 500 before LLM:
  - Previously due to missing `get_num_tokens`; now handled via heuristic fallback.

- Frequent 503 with `cc-retry: true` on `/loadSources`:
  - Indicates backpressure gating is active. Check RabbitMQ backlog and R2R worker health.
  - Tune `QUEUE_BACKLOG_PER_CONSUMER_OK` and `QUEUE_MAX_WAIT_SECONDS`.

- Cache not being used (no `Quick-skip` logs):
  - Ensure `R2R_SKIP_UPSERT_ALL_WITHIN_SECS` and/or `R2R_SKIP_UPSERT_META_WITHIN_SECS` are set in the CCBE container env.
  - Verify `/app/persistent_storage/r2r_upsert_cache.json` exists and contains entries keyed by sha256.
  - Confirm the computed sha256 in CCBE matches the one in the cache (file content changed → no skip).

## Upsert Caching & Skip Windows

Rationale: Re-running large or interrupted scans can waste time and compute if unchanged files are re-checked or re-ingested. CCBE adds a lightweight cache and two time-based skip windows to short‑circuit work when safe.

- Cache location: `/app/persistent_storage/r2r_upsert_cache.json` (bind‑mounted to your data dir). Keys are document `sha256` with values `{ ts, doc_id, filename }`.
- Skip windows (set one or both):
  - `R2R_SKIP_UPSERT_ALL_WITHIN_SECS`: if the same sha (and thus same content) was seen within this window, CCBE skips all R2R calls for that file and returns success. Logged as `Quick-skip all ...`.
  - `R2R_SKIP_UPSERT_META_WITHIN_SECS`: beyond the above but within this window, CCBE bypasses metadata/dedup checks for unchanged content. Logged as `Quick-skip meta ...`.

Behavior:
- The cache is updated on successful creates and when existing documents are reused. On skip, CCBE refreshes the timestamp to extend the window.
- Skips apply per file content (sha256); if content changes, CCBE resumes normal checks.

Seeding the cache (fast path):
- Use the R2R CSV export to seed entries rapidly:
  - Inside the CCBE container:
    - `python3 -c "import json; from context_chat_backend.backends.r2r import R2rBackend; b=R2rBackend(); print(json.dumps(b.seed_upsert_cache_from_export(), indent=2))"`
  - The export posts to `/v3/documents/export` with `Accept: text/csv`, requests `id,title,metadata`, and parses `metadata` JSON to read `sha256` and `filename/source/title`.
  - For huge datasets, you can add `max_rows=100000` and `flush_every=10000` to see progress and persist incrementally.

Verification:
- Enable DEBUG logs (default for `ccb.r2r`), tail the CCBE log, and look for `Quick-skip ...` lines while scanning.
- Confirm the cache file exists and grows after seeding and during scans.

## Operations

- Seed cache (full export, streamed):
  - `docker exec -it ccbe-r2r python3 -c "import json; from context_chat_backend.backends.r2r import R2rBackend; b=R2rBackend(); print(json.dumps(b.seed_upsert_cache_from_export(flush_every=10000), indent=2))"`

- Seed cache in chunks (progress + limit runtime):
  - `docker exec -it ccbe-r2r python3 -c "import json; from context_chat_backend.backends.r2r import R2rBackend; b=R2rBackend(); print(json.dumps(b.seed_upsert_cache_from_export(max_rows=100000, flush_every=10000), indent=2))"`

- Watch progress and quick-skip activity:
  - `tail -f /data/context_chat_backend/logs/ccb.log | egrep 'Seeding export progress|Quick-skip|Upsert skip windows'`

- Verify cache file on disk:
  - `docker exec -it ccbe-r2r sh -lc 'ls -lh /app/persistent_storage/r2r_upsert_cache.json; wc -c /app/persistent_storage/r2r_upsert_cache.json'`

- Enable or tune skip windows (edit `/opt/ccbe-r2r.env` and restart CCBE):
  - `R2R_SKIP_UPSERT_ALL_WITHIN_SECS=172800`
  - `R2R_SKIP_UPSERT_META_WITHIN_SECS=345600`
  - Restart: `docker compose -f /opt/ccbe-r2r-docker-compose.yml up -d --build context_chat_backend`

- Backpressure configuration (RabbitMQ mgmt):
  - `QUEUE_HEALTH_URL=http://<r2r-host>:15673`
  - `QUEUE_HEALTH_USER=user`
  - `QUEUE_HEALTH_PASSWORD=password`
  - Optional: `QUEUE_BACKLOG_PER_CONSUMER_OK=20`, `QUEUE_MAX_WAIT_SECONDS=120`, `QUEUE_HEALTH_FAIL_CLOSED=true`
  - Verify from CCBE: `docker exec -it ccbe-r2r curl -u user:password http://<r2r-host>:15673/api/overview`

- Verify CCBE health (signed request):
  - `export APP_ID=context_chat_backend APP_VERSION=4.4.1 APP_SECRET=12345`
  - `AUTH=$(printf 'ncadmin:%s' "$APP_SECRET" | base64 -w0)`
  - `curl -v --http1.1 http://127.0.0.1:10034/enabled -H "EX-APP-ID: $APP_ID" -H "EX-APP-VERSION: $APP_VERSION" -H "OCS-APIRequest: true" -H "AUTHORIZATION-APP-API: $AUTH"`

## Validation Steps

1. Set `RAG_BACKEND=r2r` and R2R credentials/URL.
2. Start CCBE and confirm health checks pass (`/v3/system/status`).
3. Hit `POST /docSearch` with a known query and `userId`; verify `sourceId` formatting like `files__default: 123`.
4. Hit `POST /query` with the same query:
   - If R2R returns `generated_answer`, it should appear as the final answer in Nextcloud with the same `sources` you see in logs.
   - If not, CCBE will prompt the local LLM (watch for `invoking llm`).

## Notes

- We preserve R2R’s flexibility by tolerating different `results` shapes.
- We avoid leaking secrets in logs by masking `Authorization` and `X-API-Key` in the emitted curl.
- Source normalization specifically targets Context Chat expectations (provider name normalization and a space after `:`).

## Performance Tuning (no-drop under heavy ingestion)

To keep the R2R API responsive while large Hatchet queues drain, we adjust deployment configs only (no upstream code changes):

- API server (uvicorn workers and caps)
  - `r2r_docker_compose.yaml` adds uvicorn flags using env defaults: `--workers`, `--limit-concurrency`, `--timeout-keep-alive`, and `--backlog`.
  - `r2r.env` defines: `R2R_API_WORKERS=4`, `R2R_API_LIMIT_CONCURRENCY=256`, `R2R_API_KEEPALIVE=15`, `R2R_API_BACKLOG=2048`.
- Hatchet / RabbitMQ backpressure
  - `/data/r2r/hatchetconfig/server.yaml`: set `msgQueue.rabbitmq.qos: 25` to avoid hoarding large prefetches.
- Concurrency limits
  - `ga_r2r.toml`: `[orchestration] ingestion_concurrency_limit = 4`; `[embedding] concurrent_request_limit = 6`; `[completion] concurrent_request_limit = 3`.
  - `r2r.env`: `UNSTRUCTURED_NUM_WORKERS=4`.
- Logging reduction
  - `r2r.env`: `R2R_LOG_LEVEL=INFO` to reduce I/O during bursts.

These keep ingestion flowing (no drops) and make the dashboard and API on port 7272 responsive during large scans. Switching `RAG_BACKEND` back to `builtin` restores upstream behavior at any time.

## Horizontal Scaling: Unstructured (multi-host)

Goal: spread CPU-heavy document parsing across multiple machines while keeping R2R state centralized. We scale the stateless Unstructured service and point R2R at a small load balancer.

Approach (recommended): keep the existing R2R stack on your primary host, deploy an additional Unstructured service on a second host (e.g., `gazasrv17`), and front both with a lightweight TCP HTTP load balancer.

1) Deploy Unstructured on the second server

- On `gazasrv17`, run an Unstructured container matching the version you use on the primary host. Example `docker-compose.yml`:

  ```yaml
  services:
    unstructured:
      image: ragtoriches/unst-prod
      environment:
        - UNSTRUCTURED_NUM_WORKERS=12   # tune per CPU cores
      healthcheck:
        test: ["CMD", "curl", "-f", "http://localhost:7275/health"]
        interval: 10s
        timeout: 5s
        retries: 5
      ports:
        - "7275:7275"
      restart: unless-stopped
  ```

- Validate: `curl http://<gazasrv17-ip>:7275/health` returns `ok`.

2) Add a tiny load balancer on the primary host (sidecar service)

- Add an HAProxy (or NGINX) sidecar to the existing R2R compose and point it at both backends:

  `haproxy.cfg`:

  ```
  global
    maxconn 4096
  defaults
    mode http
    timeout connect 5s
    timeout client  120s
    timeout server  120s
  frontend fe_unstructured
    bind :7277
    default_backend be_unstructured
  backend be_unstructured
    balance leastconn
    option httpchk GET /health
    server local      unstructured:7275           check inter 2000 fall 3 rise 2 weight 1
    server gazasrv17  <gazasrv17-ip>:7275         check inter 2000 fall 3 rise 2 weight 1
  ```

- Compose sidecar (example snippet to add alongside your `r2r` service):

  ```yaml
  services:
    unstructured-lb:
      image: haproxy:2.8
      volumes:
        - /opt/haproxy-unstructured.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro
      ports:
        - "7277:7277"   # optional; not required if only R2R consumes it internally
      restart: unless-stopped
  ```

3) Point R2R at the load balancer

- Update R2R env to use the LB: `UNSTRUCTURED_SERVICE_URL=http://unstructured-lb:7277`
- Recreate the `r2r` container to pick up the env change.

4) Tuning after scale-out

- `ga_r2r.toml` → `[orchestration] ingestion_concurrency_limit`: increase gradually (e.g., +4 at a time) to utilize the added capacity.
- `UNSTRUCTURED_NUM_WORKERS` on both hosts: set per CPU cores and memory pressure; start with 8–12.
- Keep `embedding.concurrent_request_limit` aligned with your LLM/embedding throughput.

5) Validation checklist

- `curl http://unstructured-lb:7277/health` from inside the `r2r` container returns `ok`.
- HAProxy stats (optional) show both backends up and serving.
- Hatchet dashboard shows steady step starts; R2R `/v3/retrieval/rag` latency stays stable under load.

6) Rollback

- Point `UNSTRUCTURED_SERVICE_URL` back to `http://unstructured:7275` and stop the LB. No code changes required.

Alternative: Docker Swarm

- If you prefer a single compose across hosts, move to Docker Swarm with an overlay network and scale `unstructured` to multiple replicas. The Swarm VIP for `unstructured:7275` will load-balance across nodes automatically. This is heavier to stand up but eliminates the LB sidecar.
