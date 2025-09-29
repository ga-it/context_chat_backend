"""Runtime adjustments for the R2R backend.

We extend the upstream adapter with optional deep-context retrieval while
keeping the original module untouched for easier rebases.
"""
from __future__ import annotations

import os
from functools import wraps
from typing import Any, Sequence

from . import r2r as _r2r_mod


def _parse_override() -> int | None:
    raw = os.getenv("R2R_SEARCH_LIMIT", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _build_payload(
    user_id: str,
    query: str,
    limit: int,
    scope_type: str | None,
    scope_list: Sequence[str] | None,
    collection_filters: Sequence[str] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"query": query, "top_k": limit}
    filters = list(collection_filters or [])
    if filters:
        payload["filters"] = {"collection_ids": {"$overlap": filters}}
    if scope_type and scope_list:
        payload["scope"] = {"type": scope_type, "ids": list(scope_list)}
    payload["search_settings"] = {
        "limit": limit,
        "include_metadatas": True,
        "include_scores": True,
    }
    return payload


_original_init = _r2r_mod.R2rBackend.__init__
_original_rag = _r2r_mod.R2rBackend.rag


@wraps(_original_init)
def _patched_init(self: _r2r_mod.R2rBackend, *args, **kwargs):
    _original_init(self, *args, **kwargs)
    self._search_limit_override = _parse_override()


@wraps(_original_rag)
def _patched_rag(
    self: _r2r_mod.R2rBackend,
    *,
    user_id: str,
    query: str,
    ctx_limit: int,
    scope_type: str | None = None,
    scope_list: Sequence[str] | None = None,
) -> dict[str, Any]:
    override = getattr(self, "_search_limit_override", None)
    requested_limit = max(1, int(ctx_limit or 0))
    primary_limit = max(requested_limit, override) if isinstance(override, int) else requested_limit

    collection_filters = self._collection_filters([user_id])
    payload = _build_payload(user_id, query, primary_limit, scope_type, scope_list, collection_filters)
    _r2r_mod.logger.debug(
        "R2R search payload: %s",
        _r2r_mod.json.dumps(payload, sort_keys=True),
    )

    try:
        resp = self._request(
            "POST",
            "retrieval/rag",
            action="/v3/retrieval/rag",
            json=payload,
        )
    except _r2r_mod.httpx.HTTPError as exc:
        fallback_limit = None
        if isinstance(override, int) and override > requested_limit:
            fallback_limit = requested_limit
        if fallback_limit:
            _r2r_mod.logger.warning(
                "rag request failed with override; retrying",
                extra={
                    "error": str(exc),
                    "limit": primary_limit,
                    "fallback_limit": fallback_limit,
                },
            )
            try:
                resp = self._request(
                    "POST",
                    "retrieval/rag",
                    action="/v3/retrieval/rag",
                    json=_build_payload(user_id, query, fallback_limit, scope_type, scope_list, collection_filters),
                )
            except _r2r_mod.httpx.HTTPError as exc2:
                _r2r_mod.logger.warning(
                    "rag request failed after fallback",
                    extra={"error": str(exc2)},
                )
                return {"answer": None, "hits": []}
        else:
            _r2r_mod.logger.warning("rag request failed", extra={"error": str(exc)})
            return {"answer": None, "hits": []}

    rag_results = resp.get("results", {})
    answer = None
    if isinstance(rag_results, dict):
        answer = rag_results.get("generated_answer") or rag_results.get("answer")
    try:
        prefer = os.getenv("R2R_USE_GENERATED_ANSWER", "true").lower() in {"1", "true", "yes"}
    except Exception:
        prefer = True
    if not prefer:
        answer = None

    results = rag_results.get("search_results") if isinstance(rag_results, dict) else {}
    if isinstance(results, list):
        hits_src: Sequence[Any] = results
    elif isinstance(results, dict):
        hits_src = results.get("chunk_search_results") or []
    else:
        hits_src = []

    hits: list[dict[str, Any]] = []
    for hit in hits_src:
        if isinstance(hit, str):
            hits.append({"page_content": hit, "metadata": {}})
        else:
            hits.append(
                {
                    "page_content": hit.get("text") or hit.get("content", ""),
                    "metadata": hit.get("metadata", {}),
                }
            )

    _r2r_mod.logger.debug(
        "R2R rag results",
        extra={
            "has_answer": bool(answer),
            "hits": hits,
        },
    )
    return {"answer": answer, "hits": hits}


def _apply_overrides():
    _r2r_mod.R2rBackend.__init__ = _patched_init  # type: ignore[assignment]
    _r2r_mod.R2rBackend.rag = _patched_rag  # type: ignore[assignment]


_apply_overrides()

__all__ = ["_apply_overrides"]
