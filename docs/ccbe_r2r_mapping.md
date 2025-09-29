# CCBE to R2R Action Mapping

```mermaid
graph TD
    LS[PUT /loadSources] --> EC[ensure_collections]
    EC -->|GET /v3/collections| RC1[(list collections)]
    EC -->|POST /v3/collections| RC2[(create collection)]
    LS --> UD[upsert_document]
    UD -->|POST /v3/documents| RU[(upload document)]
    UD -->|PUT /v3/documents/{id}/metadata| UM[(update metadata)]

    UA[POST /updateAccess] --> UAa[update_access]
    UAa -->|POST/DELETE /v3/collections/{cid}/documents/{doc}| RA[(update collection membership)]

    UAD[POST /updateAccessDeclarative] --> UAd[decl_update_access]
    UAd -->|GET /v3/documents/{id}/collections| RL[(list document collections)]
    UAd -->|POST/DELETE /v3/collections/{cid}/documents/{doc}| RA

    DS[POST /deleteSources] --> DD[delete_document] -->|DELETE /v3/documents/{id}| RDel[(delete document)]

    CID[POST /countIndexedDocuments] --> LD[list_documents] -->|GET /v3/documents| RD[(list documents)]

    Q[POST /query] --> SR[search] -->|POST /v3/retrieval/rag| RS[(rag)]
    DOCS[POST /docSearch] --> SR
```

## Endpoint narratives

- **`PUT /loadSources`** first ensures per-user collections then uploads each document. The controller resolves user IDs and metadata before calling `ensure_collections` and `upsert_document`【F:context_chat_backend/controller.py†L523-L590】. These backend calls translate to listing or creating collections【F:context_chat_backend/backends/r2r.py†L528-L566】 and posting or updating documents with server-side hash checks and metadata updates while using a custom ingestion mode【F:context_chat_backend/backends/r2r.py†L699-L913】.
- **`POST /updateAccessDeclarative`** synchronizes document membership for a set of users. CCBE invokes `decl_update_access`【F:context_chat_backend/controller.py†L334-L356】 which lists existing document collections and issues POST/DELETE requests to adjust membership【F:context_chat_backend/backends/r2r.py†L1177-L1201】.
- **`POST /updateAccess`** grants or revokes access for users. The controller delegates to `update_access`【F:context_chat_backend/controller.py†L369-L399】 which maps to R2R collection membership operations【F:context_chat_backend/backends/r2r.py†L1152-L1175】.
- **`POST /deleteSources`** removes documents by ID. CCBE calls `delete_document` for each identifier【F:context_chat_backend/controller.py†L443-L467】 which issues `DELETE /v3/documents/{id}` in R2R【F:context_chat_backend/backends/r2r.py†L1120-L1138】.
- **`POST /countIndexedDocuments`** reports document counts. When using R2R, it simply lists documents and returns the length【F:context_chat_backend/controller.py†L511-L517】【F:context_chat_backend/backends/r2r.py†L613-L620】.
- **`POST /query`** and **`POST /docSearch`** forward search requests to R2R. Both endpoints call `search` on the backend【F:context_chat_backend/controller.py†L737-L743】【F:context_chat_backend/controller.py†L775-L781】 which translates into `POST /v3/retrieval/rag` filtered by the user's resolved R2R collection ID via `_collection_filters`【F:context_chat_backend/backends/r2r.py†L1205-L1315】.

## References

- R2R document upsert performs a server-side hash comparison by issuing `POST /v3/retrieval/search` with a wildcard query (`*`), `search_mode` set to `basic`, and `search_settings` containing a metadata hash filter and limit to avoid re-uploading unchanged files, updating metadata in place when hashes match and skipping documents that are still ingesting【F:context_chat_backend/backends/r2r.py†L699-L865】【F:context_chat_backend/backends/r2r.py†L622-L712】.
- Access control modifications operate through collection-document membership changes【F:context_chat_backend/backends/r2r.py†L1152-L1201】.
- **Search depth override**: CCBE now reads `R2R_SEARCH_LIMIT` (default unset) to decide how many chunks to request from `/v3/retrieval/rag`. When set (>0) the limit takes precedence over `ctxLimit`, allowing very large documents (for example, full-length books) to supply deep context without changing the Nextcloud client.
