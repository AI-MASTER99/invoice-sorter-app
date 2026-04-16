"""
Supabase database layer — all DB queries live here.
Multi-tenant: every query is filtered by company_id.
"""
import os
from pathlib import Path
from typing import Any, Optional
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)

from supabase import Client, create_client

_url = os.environ.get("SUPABASE_URL", "")
_key = os.environ.get("SUPABASE_KEY", "")

if not _url or not _key:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set in .env")

sb: Client = create_client(_url, _key)

# Default company for users who don't belong to a specific company yet
DEFAULT_COMPANY_ID = "00000000-0000-0000-0000-000000000001"

# Storage bucket names
BUCKET_UPLOADS = "invoice-uploads"
BUCKET_EXPORTS = "invoice-exports"


# ═══════════════════════════════════════════════════════════════
# STORAGE  (Supabase Storage for PDFs + Excel files)
# ═══════════════════════════════════════════════════════════════
def storage_upload(bucket: str, path: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Upload bytes to a bucket at the given path. Returns the storage path."""
    sb.storage.from_(bucket).upload(
        path=path,
        file=data,
        file_options={"content-type": content_type, "upsert": "true"},
    )
    return path


def storage_download(bucket: str, path: str) -> bytes:
    """Download a file's bytes from storage."""
    return sb.storage.from_(bucket).download(path)


def storage_signed_url(bucket: str, path: str, expires_in: int = 3600) -> str:
    """Generate a signed URL that allows temporary access to a private file."""
    result = sb.storage.from_(bucket).create_signed_url(path, expires_in)
    return result.get("signedURL") or result.get("signedUrl") or ""


def storage_delete(bucket: str, path: str) -> None:
    """Delete a file from storage."""
    sb.storage.from_(bucket).remove([path])


# ═══════════════════════════════════════════════════════════════
# COMPANIES
# ═══════════════════════════════════════════════════════════════
def get_company_by_name(name: str) -> Optional[dict]:
    r = sb.table("companies").select("*").eq("name", name).limit(1).execute()
    return r.data[0] if r.data else None


def create_company(name: str) -> dict:
    r = sb.table("companies").insert({"name": name}).execute()
    return r.data[0]


def list_companies() -> list[dict]:
    r = sb.table("companies").select("*").order("name").execute()
    return r.data


# ═══════════════════════════════════════════════════════════════
# USERS
# ═══════════════════════════════════════════════════════════════
def get_user(username: str, company_id: Optional[str] = None) -> Optional[dict]:
    """Find a user by username. If company_id is provided, filter by it."""
    q = sb.table("users").select("*").eq("username", username)
    if company_id:
        q = q.eq("company_id", company_id)
    r = q.limit(1).execute()
    return r.data[0] if r.data else None


def get_user_by_id(user_id: str) -> Optional[dict]:
    r = sb.table("users").select("*").eq("id", user_id).limit(1).execute()
    return r.data[0] if r.data else None


def list_users(company_id: str) -> list[dict]:
    r = (sb.table("users")
         .select("id, username, role, created_at")
         .eq("company_id", company_id)
         .order("username")
         .execute())
    return r.data


def create_user(company_id: str, username: str, password_hash: str, role: str = "user") -> dict:
    r = sb.table("users").insert({
        "company_id": company_id,
        "username": username,
        "password_hash": password_hash,
        "role": role,
    }).execute()
    return r.data[0]


def update_user_password(user_id: str, password_hash: str) -> None:
    sb.table("users").update({"password_hash": password_hash}).eq("id", user_id).execute()


def delete_user(user_id: str) -> None:
    sb.table("users").delete().eq("id", user_id).execute()


# ═══════════════════════════════════════════════════════════════
# INVOICES
# ═══════════════════════════════════════════════════════════════
def create_invoice(company_id: str, data: dict) -> dict:
    payload = {"company_id": company_id, **data}
    r = sb.table("invoices").insert(payload).execute()
    return r.data[0]


def list_invoices(company_id: str) -> list[dict]:
    r = (sb.table("invoices")
         .select("*")
         .eq("company_id", company_id)
         .order("date", desc=True)
         .execute())
    return r.data


def get_invoice(invoice_id: str, company_id: str) -> Optional[dict]:
    r = (sb.table("invoices")
         .select("*")
         .eq("id", invoice_id)
         .eq("company_id", company_id)
         .limit(1)
         .execute())
    return r.data[0] if r.data else None


def update_invoice(invoice_id: str, company_id: str, updates: dict) -> None:
    (sb.table("invoices")
     .update(updates)
     .eq("id", invoice_id)
     .eq("company_id", company_id)
     .execute())


def delete_invoice(invoice_id: str, company_id: str) -> None:
    (sb.table("invoices")
     .delete()
     .eq("id", invoice_id)
     .eq("company_id", company_id)
     .execute())


# ═══════════════════════════════════════════════════════════════
# PRODUCT MEMORY
# ═══════════════════════════════════════════════════════════════
def list_memory(company_id: str) -> list[dict]:
    r = (sb.table("product_memory")
         .select("*")
         .eq("company_id", company_id)
         .order("updated_at", desc=True)
         .execute())
    return r.data


def get_memory_by_code(company_id: str, code: str) -> list[dict]:
    """Find all memory entries for a given commodity code (any description)."""
    r = (sb.table("product_memory")
         .select("*")
         .eq("company_id", company_id)
         .eq("code", code)
         .execute())
    return r.data


def get_memory_entry(company_id: str, code: str, description: str) -> Optional[dict]:
    r = (sb.table("product_memory")
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
    r = (sb.table("product_memory")
         .upsert(payload, on_conflict="company_id,code,description")
         .execute())
    return r.data[0] if r.data else {}


def update_memory(memory_id: str, company_id: str, updates: dict) -> None:
    (sb.table("product_memory")
     .update(updates)
     .eq("id", memory_id)
     .eq("company_id", company_id)
     .execute())


def count_memory_pending(company_id: str) -> int:
    r = (sb.table("product_memory")
         .select("id", count="exact")
         .eq("company_id", company_id)
         .eq("confirmed", False)
         .execute())
    return r.count or 0


# ═══════════════════════════════════════════════════════════════
# JOBS
# ═══════════════════════════════════════════════════════════════
def create_job(company_id: str, data: dict) -> dict:
    payload = {"company_id": company_id, **data}
    r = sb.table("jobs").insert(payload).execute()
    return r.data[0]


def list_jobs(company_id: str) -> list[dict]:
    r = (sb.table("jobs")
         .select("*")
         .eq("company_id", company_id)
         .order("created_at", desc=True)
         .execute())
    return r.data


def get_job(job_id: str) -> Optional[dict]:
    r = sb.table("jobs").select("*").eq("id", job_id).limit(1).execute()
    return r.data[0] if r.data else None


def update_job(job_id: str, updates: dict) -> None:
    sb.table("jobs").update(updates).eq("id", job_id).execute()


def count_jobs_today(company_id: str) -> int:
    """Count jobs completed today for the dashboard stat."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    r = (sb.table("jobs")
         .select("id", count="exact")
         .eq("company_id", company_id)
         .eq("status", "done")
         .gte("created_at", today.isoformat())
         .execute())
    return r.count or 0
