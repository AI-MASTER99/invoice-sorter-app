"""
Supabase database layer — all DB queries live here.
Multi-tenant: every query is filtered by company_id (and after Phase B,
also enforced by Postgres Row-Level Security policies).

Two clients:
  - _sb_service (service-role, BYPASSRLS) — for login flow, queue worker,
    startup bootstrap, and storage operations.
  - per-request user-scoped client — built via make_user_client(jwt) in
    main.py's `authed` dep, stored in `_current_client` ContextVar for the
    request lifetime. RLS evaluates against the JWT's claims.

DAL functions resolve the client via _client() which returns the
contextvar value if set, else falls back to _sb_service. Inside an HTTP
request scope, that's the user client. Outside (worker, startup, storage),
it's service-role.

Storage operations always go through _sb_service.storage explicitly —
storage.objects has no RLS policies; tenant isolation comes from the
{company_id}/… path-prefix in app code. See docs/archive/PHASE_B_PLAN.md
for full rationale.
"""
import json as _json
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)

from supabase import Client, create_client
from supabase.client import ClientOptions

_url = os.environ.get("SUPABASE_URL", "")
_service_key = os.environ.get("SUPABASE_KEY", "")
_anon_key = os.environ.get("SUPABASE_ANON_KEY", "")

if not _url or not _service_key:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set in .env")
if not _anon_key:
    raise RuntimeError(
        "SUPABASE_ANON_KEY must be set in .env (required for per-request "
        "user-scoped clients in Phase B)"
    )

# Service-role client — bypasses RLS. Used by login, queue worker,
# startup bootstrap, and storage. Module-private; never imported directly
# by main.py (the CI lint forbids `db.sb.` references).
_sb_service: Client = create_client(_url, _service_key)

# Per-request override: set by main.py's `authed` dep, reset on response.
# Default None → _client() falls back to _sb_service.
_current_client: ContextVar[Optional[Client]] = ContextVar(
    "current_client", default=None
)


def _client() -> Client:
    """Return the request-scoped client, or fall back to service-role.

    NEVER call this from background workers — they explicitly run outside
    any request scope, so the contextvar is unset, and _sb_service is
    correct. This documents the contract.
    """
    c = _current_client.get()
    return c if c is not None else _sb_service


def make_user_client(jwt: str) -> Client:
    """Build a per-request user-scoped Supabase client.

    Uses the anon key (not service-role) plus the user's JWT in the
    Authorization header. PostgREST switches to the `authenticated`
    Postgres role (per the JWT's `role` claim) and evaluates RLS policies
    against the JWT's other claims (company_id, app_role, etc.).

    PERF NOTE: this constructs a fresh httpx.Client. If staging
    measurement shows >50ms p95 added to request latency, switch to a
    pooled-client pattern. See docs/archive/PHASE_B_PLAN.md §8.4 (H3).
    """
    return create_client(
        _url, _anon_key,
        options=ClientOptions(
            headers={"Authorization": f"Bearer {jwt}"},
        ),
    )


# Default company for users who don't belong to a specific company yet
DEFAULT_COMPANY_ID = "00000000-0000-0000-0000-000000000001"

# Storage bucket names
BUCKET_UPLOADS = "invoice-uploads"
BUCKET_EXPORTS = "invoice-exports"


# ═══════════════════════════════════════════════════════════════
# STORAGE  (Supabase Storage for PDFs + Excel files)
# ═══════════════════════════════════════════════════════════════
# All storage operations go through _sb_service explicitly.
# storage.objects has no RLS policies; tenant isolation comes from the
# {company_id}/… path-prefix that callers always include.
def storage_upload(bucket: str, path: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Upload bytes to a bucket at the given path. Returns the storage path."""
    _sb_service.storage.from_(bucket).upload(
        path=path,
        file=data,
        file_options={"content-type": content_type, "upsert": "true"},
    )
    return path


def storage_download(bucket: str, path: str) -> bytes:
    """Download a file's bytes from storage."""
    return _sb_service.storage.from_(bucket).download(path)


def storage_delete(bucket: str, path: str) -> None:
    """Delete a file from storage."""
    _sb_service.storage.from_(bucket).remove([path])


_STORAGE_LIST_PAGE = 1000


def _storage_list_folder(bucket: str, prefix: str) -> list[dict]:
    """One folder's entries (files + subfolders), following pagination."""
    store = _sb_service.storage.from_(bucket)
    out: list[dict] = []
    offset = 0
    while True:
        page = store.list(prefix, {"limit": _STORAGE_LIST_PAGE, "offset": offset})
        if not page:
            break
        out.extend(page)
        if len(page) < _STORAGE_LIST_PAGE:
            break
        offset += _STORAGE_LIST_PAGE
    return out


def storage_list_all(bucket: str, prefix: str = "") -> list[dict]:
    """Recursively list every file object in a bucket.

    Objects are stored as ``{company_id}/<file>`` so we descend one level
    per company folder. Returns dicts with ``path``, ``size`` (bytes), and
    ``created_at`` (ISO string or None). Used by the retention purge.
    """
    files: list[dict] = []
    for entry in _storage_list_folder(bucket, prefix):
        name = entry.get("name")
        if not name:
            continue
        full = f"{prefix}/{name}" if prefix else name
        meta = entry.get("metadata")
        if isinstance(meta, dict) and meta.get("size") is not None:
            files.append({
                "path": full,
                "size": int(meta.get("size") or 0),
                "created_at": entry.get("created_at") or entry.get("updated_at"),
            })
        else:
            # Folder placeholder → recurse.
            files.extend(storage_list_all(bucket, full))
    return files


def storage_delete_many(bucket: str, paths: list[str]) -> int:
    """Delete many objects in batches. Returns the count removed."""
    store = _sb_service.storage.from_(bucket)
    removed = 0
    BATCH = 100
    for i in range(0, len(paths), BATCH):
        chunk = paths[i:i + BATCH]
        store.remove(chunk)
        removed += len(chunk)
    return removed


# ═══════════════════════════════════════════════════════════════
# COMPANIES
# ═══════════════════════════════════════════════════════════════
def get_company_by_name(name: str) -> Optional[dict]:
    r = _client().table("companies").select("*").eq("name", name).limit(1).execute()
    return r.data[0] if r.data else None


def create_company(name: str) -> dict:
    r = _client().table("companies").insert({"name": name}).execute()
    return r.data[0]


def list_companies() -> list[dict]:
    r = _client().table("companies").select("*").order("name").execute()
    return r.data


def delete_company(company_id: str) -> None:
    """Delete a company. RLS policy `companies_super_admin` permits this
    only for super_admin JWTs; admin/user requests get 0 rows affected."""
    _client().table("companies").delete().eq("id", company_id).execute()


# ═══════════════════════════════════════════════════════════════
# USERS
# ═══════════════════════════════════════════════════════════════
def get_user(username: str, company_id: Optional[str] = None) -> Optional[dict]:
    """Find a user by username. If company_id is provided, filter by it."""
    q = _client().table("users").select("*").eq("username", username)
    if company_id:
        q = q.eq("company_id", company_id)
    r = q.limit(1).execute()
    return r.data[0] if r.data else None


def get_user_by_id(user_id: str) -> Optional[dict]:
    r = _client().table("users").select("*").eq("id", user_id).limit(1).execute()
    return r.data[0] if r.data else None


def list_users(company_id: str) -> list[dict]:
    r = (_client().table("users")
         .select("id, username, role, created_at")
         .eq("company_id", company_id)
         .order("username")
         .execute())
    return r.data


def create_user(company_id: str, username: str, password_hash: str, role: str = "user") -> dict:
    r = _client().table("users").insert({
        "company_id": company_id,
        "username": username,
        "password_hash": password_hash,
        "role": role,
    }).execute()
    return r.data[0]


def update_user_password(user_id: str, password_hash: str) -> None:
    _client().table("users").update({"password_hash": password_hash}).eq("id", user_id).execute()


def delete_user(user_id: str) -> None:
    _client().table("users").delete().eq("id", user_id).execute()


# ═══════════════════════════════════════════════════════════════
# INVOICES
# ═══════════════════════════════════════════════════════════════
def create_invoice(company_id: str, data: dict) -> dict:
    payload = {"company_id": company_id, **data}
    r = _client().table("invoices").insert(payload).execute()
    return r.data[0]


# Summary columns for list views. NEVER select("*") on this table in a
# polled path: the heavy JSONB columns (rows, tariff_data, totals, …) made
# every 5-second dashboard poll pull the entire table (~750 KB) out of
# Supabase just to render 6 small fields — the primary source of the
# 23 GB/month egress that got the free-tier project restricted.
_INVOICE_SUMMARY_COLS = "id, supplier, filename, date, created_at, value, status"


def list_invoices(company_id: str, columns: str = _INVOICE_SUMMARY_COLS,
                  limit: int = 500) -> list[dict]:
    # Bounded: /invoices is polled every 5s. Without a row cap the payload
    # grows with history forever — the row-axis twin of the column-axis
    # egress leak. The dashboard never renders more than a page anyway.
    r = (_client().table("invoices")
         .select(columns)
         .eq("company_id", company_id)
         .order("date", desc=True)
         .limit(limit)
         .execute())
    return r.data


def count_invoices(company_id: str, status: Optional[str] = None) -> int:
    q = (_client().table("invoices")
         .select("id", count="exact")
         .eq("company_id", company_id))
    if status:
        q = q.eq("status", status)
    return q.execute().count or 0


def count_memory(company_id: str) -> int:
    r = (_client().table("product_memory")
         .select("id", count="exact")
         .eq("company_id", company_id)
         .execute())
    return r.count or 0


def get_invoice(invoice_id: str, company_id: str) -> Optional[dict]:
    r = (_client().table("invoices")
         .select("*")
         .eq("id", invoice_id)
         .eq("company_id", company_id)
         .limit(1)
         .execute())
    return r.data[0] if r.data else None


def update_invoice(invoice_id: str, company_id: str, updates: dict) -> None:
    (_client().table("invoices")
     .update(updates)
     .eq("id", invoice_id)
     .eq("company_id", company_id)
     .execute())


def delete_invoice(invoice_id: str, company_id: str) -> None:
    (_client().table("invoices")
     .delete()
     .eq("id", invoice_id)
     .eq("company_id", company_id)
     .execute())


# ═══════════════════════════════════════════════════════════════
# PRODUCT MEMORY
# ═══════════════════════════════════════════════════════════════
def list_memory(company_id: str) -> list[dict]:
    r = (_client().table("product_memory")
         .select("*")
         .eq("company_id", company_id)
         .order("updated_at", desc=True)
         .execute())
    return r.data


def get_memory_entry(company_id: str, code: str, description: str) -> Optional[dict]:
    r = (_client().table("product_memory")
         .select("*")
         .eq("company_id", company_id)
         .eq("code", code)
         .eq("description", description)
         .limit(1)
         .execute())
    return r.data[0] if r.data else None


def upsert_memory(company_id: str, entry: dict) -> dict:
    """Insert or update based on (company_id, code, description) unique key."""
    payload = {"company_id": company_id, **entry}
    # supabase-py upsert needs the composite conflict target
    r = (_client().table("product_memory")
         .upsert(payload, on_conflict="company_id,code,description")
         .execute())
    return r.data[0] if r.data else {}


def update_memory(memory_id: str, company_id: str, updates: dict) -> None:
    (_client().table("product_memory")
     .update(updates)
     .eq("id", memory_id)
     .eq("company_id", company_id)
     .execute())


def count_memory_pending(company_id: str) -> int:
    r = (_client().table("product_memory")
         .select("id", count="exact")
         .eq("company_id", company_id)
         .eq("confirmed", False)
         .execute())
    return r.count or 0


def delete_memory_entry(memory_id: str, company_id: str) -> None:
    """Delete a memory entry. Filtered by company_id for tenant safety
    (RLS also filters via memory_tenant_all policy under user JWT)."""
    (_client().table("product_memory")
     .delete()
     .eq("id", memory_id)
     .eq("company_id", company_id)
     .execute())


# ═══════════════════════════════════════════════════════════════
# CLIENTS + COMMODITY-CODE LIST
# A "client" is the supplier/exporter — the registry holds their
# identity (name, REX, EORI, aliases) used to match invoices and to
# fill the export's REX fallback. Commodity codes live in ONE shared
# company-wide list (commodity_codes: general code -> full code +
# description + CDS fields) that replaces both the per-client lists
# and the gov.uk tariff lookup.
# ═══════════════════════════════════════════════════════════════
def list_clients(company_id: str) -> list[dict]:
    r = (_client().table("clients").select("*")
         .eq("company_id", company_id).order("name").execute())
    return r.data


def get_client(company_id: str, client_id: str) -> Optional[dict]:
    r = (_client().table("clients").select("*")
         .eq("company_id", company_id).eq("id", client_id).limit(1).execute())
    return r.data[0] if r.data else None


def create_client_record(company_id: str, entry: dict) -> dict:
    # NOTE: named *_record (not create_client) so it does NOT shadow the
    # supabase `create_client` imported at the top of this module.
    r = _client().table("clients").insert({"company_id": company_id, **entry}).execute()
    return r.data[0] if r.data else {}


def update_client(company_id: str, client_id: str, updates: dict) -> None:
    (_client().table("clients").update(updates)
     .eq("company_id", company_id).eq("id", client_id).execute())


def delete_client(company_id: str, client_id: str) -> None:
    """Deletes the client (any legacy client_products rows cascade via FK
    until migration 007 drops that table)."""
    (_client().table("clients").delete()
     .eq("company_id", company_id).eq("id", client_id).execute())


def find_client_by_identity(company_id: str, *, rex: str = "",
                            eori: str = "", name: str = "") -> Optional[dict]:
    """Resolve which client an invoice belongs to. Prefer the stable REX/EORI
    identifiers; fall back to an exact (case-insensitive) name or alias match."""
    def base():
        return _client().table("clients").select("*").eq("company_id", company_id)
    if rex:
        r = base().eq("rex", rex).limit(1).execute()
        if r.data:
            return r.data[0]
    if eori:
        r = base().eq("eori", eori).limit(1).execute()
        if r.data:
            return r.data[0]
    if name:
        r = base().ilike("name", name).limit(1).execute()
        if r.data:
            return r.data[0]
        # aliases is jsonb — containment needs a JSON-encoded value, not a
        # Postgres array literal (which raises 22P02).
        r = base().contains("aliases", _json.dumps([name])).limit(1).execute()
        if r.data:
            return r.data[0]
    return None


_CODES_PAGE = 1000


def list_commodity_codes(company_id: str) -> list[dict]:
    """The company's whole list, paged past PostgREST's row cap.

    An unbounded select is capped by Supabase's "Max rows" setting (1000 by
    default) and comes back silently short — with a list this size the app
    would show a partial list with nothing to say the rest was missing.
    Ordering by (general_code, full_code) is total, so paging cannot skip
    or repeat a row; full_code is unique per company.
    """
    out: list[dict] = []
    while True:
        r = (_client().table("commodity_codes").select("*")
             .eq("company_id", company_id)
             .order("general_code").order("full_code")
             .range(len(out), len(out) + _CODES_PAGE - 1).execute())
        page = r.data or []
        out.extend(page)
        if len(page) < _CODES_PAGE:
            return out


def upsert_commodity_codes(company_id: str, entries: list[dict]) -> int:
    """Upsert many list rows at once, keyed on (company_id, full_code).

    One request per chunk instead of one per code — loading a full list is
    ~1700 rows, which as individual upserts is ~1700 round trips.
    """
    written = 0
    for i in range(0, len(entries), _CODES_PAGE):
        chunk = [{"company_id": company_id, **e} for e in entries[i:i + _CODES_PAGE]]
        r = (_client().table("commodity_codes")
             .upsert(chunk, on_conflict="company_id,full_code").execute())
        written += len(r.data or chunk)
    return written


def get_commodity_codes_by_general_code(company_id: str,
                                        general_code: str) -> list[dict]:
    """The VLOOKUP: candidate subcodes for a general code in the company list."""
    r = (_client().table("commodity_codes").select("*")
         .eq("company_id", company_id)
         .eq("general_code", general_code).execute())
    return r.data


def upsert_commodity_code(company_id: str, entry: dict) -> dict:
    """Insert/update one list row, keyed on (company_id, full_code)."""
    payload = {"company_id": company_id, **entry}
    r = (_client().table("commodity_codes")
         .upsert(payload, on_conflict="company_id,full_code").execute())
    return r.data[0] if r.data else {}


def count_commodity_codes(company_id: str) -> int:
    r = (_client().table("commodity_codes").select("id", count="exact")
         .eq("company_id", company_id).execute())
    return r.count or 0


def delete_commodity_codes(company_id: str) -> None:
    (_client().table("commodity_codes").delete()
     .eq("company_id", company_id).execute())


def delete_commodity_code(company_id: str, code_id: str) -> None:
    """Delete ONE list row (the in-app editor's per-row delete)."""
    (_client().table("commodity_codes").delete()
     .eq("company_id", company_id)
     .eq("id", code_id).execute())


# ═══════════════════════════════════════════════════════════════
# JOBS
# ═══════════════════════════════════════════════════════════════
def create_job(company_id: str, data: dict) -> dict:
    payload = {"company_id": company_id, **data}
    r = _client().table("jobs").insert(payload).execute()
    return r.data[0]


_JOB_COLS = "id, filename, status, progress, step, error, invoice_id, created_at"


def list_jobs(company_id: str, limit: int = 60) -> list[dict]:
    """Most recent jobs first, summary columns only. Polled every 2 s; the
    UI renders active + recently-terminal jobs, so cap rows and select just
    the fields the client uses (no select(*)) to keep this cheap on egress."""
    r = (_client().table("jobs")
         .select(_JOB_COLS)
         .eq("company_id", company_id)
         .order("created_at", desc=True)
         .limit(limit)
         .execute())
    return r.data


def fail_stale_active_jobs(message: str) -> int:
    """Mark every 'running'/'queued' job as failed. Service-role, cross-tenant.

    Called once at boot: the job queue is in-memory and does not survive a
    restart/redeploy, so any row still active at startup belongs to a dead
    process and would otherwise stay 'running' in the UI forever. The rows
    become failed → the UI shows its Retry button (retry re-reads the
    original upload from storage, so nothing is lost)."""
    r = (_sb_service.table("jobs")
         .update({"status": "failed", "progress": 0,
                  "step": message, "error": message})
         .in_("status", ["running", "queued"])
         .execute())
    return len(r.data or [])


def get_job(job_id: str) -> Optional[dict]:
    r = _client().table("jobs").select("*").eq("id", job_id).limit(1).execute()
    return r.data[0] if r.data else None


def update_job(job_id: str, updates: dict) -> None:
    _client().table("jobs").update(updates).eq("id", job_id).execute()


def delete_job(job_id: str, company_id: str) -> None:
    """Delete a job. Filtered by company_id for tenant safety (RLS also
    filters via jobs_tenant_all policy under user JWT)."""
    (_client().table("jobs")
     .delete()
     .eq("id", job_id)
     .eq("company_id", company_id)
     .execute())


def count_jobs_today(company_id: str) -> int:
    """Count jobs completed today for the dashboard stat."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    r = (_client().table("jobs")
         .select("id", count="exact")
         .eq("company_id", company_id)
         .eq("status", "done")
         .gte("created_at", today.isoformat())
         .execute())
    return r.count or 0
