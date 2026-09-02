# Archive

Historical planning and hand-over documents, kept for the decisions they
record. Nothing here describes the current state of the app; read the
top-level `README.md` and `git log` for that.

| File | What it was |
|------|-------------|
| `PHASE_B_PLAN.md`, `PHASE_B_PREREQUISITES.md`, `PHASE_B_REVIEW.md` | Design, checklist and external review for the Phase B cutover to a per-request user-scoped Supabase client (migrations 001–003a). Shipped; the invariant is now enforced by `scripts/check_no_raw_sb.sh`. |
| `SESSION_HANDOFF_2026-06-18.md` | Machine-to-machine hand-over. Records the deliberate U116 = supplier REX divergence from gov.uk guidance and the N853 first-3-digits rule. |
| `SESSION_HANDOFF_2026-07-10.md` | Production storage-quota recovery. The temporary `FORCE_ADMIN_RESET` it mentions was reverted; the retention purge and `scripts/storage_cleanup.py` it introduced are live. |
