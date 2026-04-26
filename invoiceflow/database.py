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
{company_id}/… path-prefix in app code. See migrations/PHASE_B_PLAN.md
for full rationale.
"""
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Optional
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
    pooled-client pattern. See PHASE_B_PLAN.md §8.4 (H3).
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


def storage_signed_url(bucket: str, path: str, expires_in: int = 3600) -> str:
    """Generate a signed URL that allows temporary access to a private file."""
    result = _sb_service.storage.from_(bucket).create_signed_url(path, expires_in)
    return result.get("signedURL") or result.get("signedUrl") or ""


def storage_delete(bucket: str, path: str) -> None:
    """Delete a file from storage."""
    _sb_service.storage.from_(bucket).remove([path])


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


def list_invoices(company_id: str) -> list[dict]:
    r = (_client().table("invoices")
         .select("*")
         .eq("company_id", company_id)
         .order("date", desc=True)
         .execute())
    return r.data


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


def get_memory_by_code(company_id: str, code: str) -> list[dict]:
    """Find all memory entries for a given commodity code (any description)."""
    r = (_client().table("product_memory")
         .select("*")
         .eq("company_id", company_id)
         .eq("code", code)
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
# JOBS
# ═══════════════════════════════════════════════════════════════
def create_job(company_id: str, data: dict) -> dict:
    payload = {"company_id": company_id, **data}
    r = _client().table("jobs").insert(payload).execute()
    return r.data[0]


def list_jobs(company_id: str) -> list[dict]:
    r = (_client().table("jobs")
         .select("*")
         .eq("company_id", company_id)
         .order("created_at", desc=True)
         .execute())
    return r.data


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
