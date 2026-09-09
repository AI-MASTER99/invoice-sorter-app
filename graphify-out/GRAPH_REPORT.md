# Graph Report - invoice-sorter-app  (2026-09-09)

## Corpus Check
- 55 files · ~73,511 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 745 nodes · 1327 edges · 64 communities (42 shown, 22 thin omitted)
- Extraction: 95% EXTRACTED · 5% INFERRED · 0% AMBIGUOUS · INFERRED: 64 edges (avg confidence: 0.84)
- Token cost: 160,700 input · 0 output

## Community Hubs (Navigation)
- Frontend SPA (app.js)
- CDS Export List Parser
- Commodity Code Tests
- Review Issue Detector Tests
- Items Export & Review Checks
- Tariff Document Rules
- Supabase Data Access Layer
- MultiFreight Rules Engine Plan
- CDS List Parser Tests
- Read API Endpoints
- Phase B Security Review
- Login Rate Limiter Tests
- Login & Security Hardening
- Admin Provisioning & Job Queue
- Working Sheet Layout Tests
- UI Pages & Landing Site
- Claude Invoice Extraction
- Delete Endpoints
- User Admin Privilege Tests
- CI, Deployment & Config
- Storage Cleanup Script
- Auth Dependencies & JWT Design
- Phase B Prerequisites & DAL Pattern
- Items Export Previous-Doc Tests
- Tariff Cache Refresh
- Invoice Processing Pipeline
- Row Normalisation & A/B Match
- Write API Endpoints
- UK Tariff API Lookup
- Per-Request User JWT
- Storage Quota Incident
- Storage Retention Purge
- Memory Cleanup & Resolve
- Totals Comparison
- Sub-code Matching
- User-Scoped Supabase Client
- Storage Listing
- Default Admin Bootstrap
- Login Failure Buckets
- A/B Cell Disagreements
- Commodity List Lookup
- User-Facing Job Errors
- Jobs Today Counter
- Client Deletion
- Commodity Code Deletion
- Company Deletion
- Job Deletion
- Memory Entry Deletion
- Stale Job Sweep
- Client Identity Resolution
- Sub-code Candidates Lookup
- User Lookup
- Commodity List Paging
- Job Listing
- User Client Factory
- Storage Upload
- Storage Download
- Storage Batch Delete
- Memory Upsert
- Commodity Codes Bulk Upsert
- Commodity Code Upsert
- Admin Auth Dependency
- Super-Admin Auth Dependency
- Raw-Client Lint Script

## God Nodes (most connected - your core abstractions)
1. `_client()` - 44 edges
2. `api()` - 29 edges
3. `_row()` - 27 edges
4. `_process_invoice()` - 25 edges
5. `toast()` - 19 edges
6. `escHtml()` - 17 edges
7. `Phase B: per-request user-scoped Supabase client` - 15 edges
8. `Phase B external review (RED -> YELLOW -> GREEN)` - 15 edges
9. `refreshJobs()` - 13 edges
10. `_tc()` - 13 edges

## Surprising Connections (you probably didn't know these)
- `Commodity codes page (shared V-lookup editor + import)` --implements--> `Commodity-code list (V-lookup)`  [INFERRED]
  invoiceflow/static/index.html → README.md
- `scripts/storage_cleanup.py (bulk purge, dry-run default)` --semantically_similar_to--> `STORAGE_RETENTION_DAYS env var (daily purge)`  [INFERRED] [semantically similar]
  docs/archive/SESSION_HANDOFF_2026-07-10.md → render.yaml
- `passlib 1.7.4 + bcrypt 4.0.1 pin` --conceptually_related_to--> `Migration 003: change_own_password SECURITY DEFINER RPC`  [INFERRED]
  invoiceflow/requirements.txt → docs/archive/PHASE_B_PLAN.md
- `Marketing landing page (www.invoice-sorter.com)` --semantically_similar_to--> `App shell (index.html SPA layout)`  [INFERRED] [semantically similar]
  website/index.html → invoiceflow/static/index.html
- `UK Tariff lookup page` --references--> `UK Trade Tariff API (api.trade-tariff.service.gov.uk)`  [INFERRED]
  invoiceflow/static/index.html → docs/multifreight_rules_engine_plan.md

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Phase B per-request auth flow (session -> JWT -> user client -> RLS)** — docs_archive_phase_b_plan_authed_dependency, docs_archive_phase_b_plan_mint_user_jwt, docs_archive_phase_b_plan_make_user_client, docs_archive_phase_b_plan_contextvar_dal_pattern, docs_archive_phase_b_plan_rls_policies, docs_archive_phase_b_plan_jwt_design [EXTRACTED 1.00]
- **Code paths that must stay on the service-role client** — docs_archive_phase_b_prerequisites_login_flow_service_role, docs_archive_phase_b_prerequisites_ensure_default_admin, docs_archive_phase_b_plan_queue_worker_service_role, docs_archive_phase_b_plan_storage_service_role_carveout, docs_archive_phase_b_plan_sb_service_client [EXTRACTED 1.00]
- **MultiFreight CDS Items document-code rules (U116, N853, N935/Y929, DE 2/3 slots)** — docs_archive_session_handoff_2026_06_18_u116_supplier_rex_rule, docs_archive_session_handoff_2026_06_18_n853_first_3_digits_rule, docs_archive_session_handoff_2026_06_18_origin_preference_rule, docs_archive_session_handoff_2026_06_18_de23_three_slot_limit, docs_multifreight_rules_engine_plan_build_items_xlsx, docs_archive_session_handoff_2026_07_10_tariff_rules_module [INFERRED 0.85]

## Communities (64 total, 22 thin omitted)

### Community 0 - "Frontend SPA (app.js)"
Cohesion: 0.09
Nodes (58): ACTIONS, actionsHtml(), api(), badgeHtml(), cancelClientEdit(), cancelJob(), cleanupInvalidMemory(), clientEditRow() (+50 more)

### Community 1 - "CDS Export List Parser"
Cohesion: 0.05
Nodes (48): add_entries(), build_list(), choose_description(), clean_description(), _decode(), group_by_rex(), _header_index(), _list_rows() (+40 more)

### Community 2 - "Commodity Code Tests"
Cohesion: 0.06
Nodes (29): _fake_db(), _FakeClient, _FakeQuery, _gates(), Tests for the company-wide commodity-code list (the shared "V-lookup"). Covers…, 7-digit invoice codes (dropped leading zero) still hit the list., The slice of UploadFile the handler uses (chunked async read)., Records the chained call and serves one page out of `rows`. (+21 more)

### Community 3 - "Review Issue Detector Tests"
Cohesion: 0.10
Nodes (36): Unit tests for review.py — the pure issue detector. No app, no DB, no network:…, Build a totals_check dict like compare_totals() returns., _row(), _tc(), test_bad_commodity_code_length(), test_clean_goods_row_no_field_issues(), test_clean_invoice_has_no_high_or_medium(), test_currency_clean() (+28 more)

### Community 4 - "Items Export & Review Checks"
Cohesion: 0.09
Nodes (37): build_items_xlsx(), _ensure_document_slots(), Widen the Items sheet to `want` document slots. The stock MultiFreight template…, Fill the MultiFreight CDS 'Items' tab from the processed rows. Loads the real…, build_review_issues(), _canon_ccy(), check_currency(), check_fields() (+29 more)

### Community 5 - "Tariff Document Rules"
Cohesion: 0.07
Nodes (20): _chapter(), n853_required(), norm_digits(), Per-commodity-code document rules for the MultiFreight CDS Items export. Phase…, First two digits of a commodity code, or None if not derivable., Y929 ('not organic') belongs on agri-food lines only (ch. 01-24)., Operator's rule: animal-origin prefix → CHED-P (N853) is expected., Build the ordered DE 2/3 document list + human-review flags for one line.… (+12 more)

### Community 6 - "Supabase Data Access Layer"
Cohesion: 0.10
Nodes (34): _client(), count_commodity_codes(), count_invoices(), count_memory(), count_memory_pending(), create_client_record(), create_company(), create_invoice() (+26 more)

### Community 7 - "MultiFreight Rules Engine Plan"
Cohesion: 0.09
Nodes (33): DE 2/3 three document-slot limit, Session handoff 2026-06-18 (continue-2026-06-18 branch), N853 first-3-digits rule (animal-origin chapters), Merge NOT-IN-LIST lines sharing a commodity code, Origin-based Preference 300 (EU) / 100 (non-EU), U116 reference = supplier REX (deliberate gov.uk divergence), tariff_rules.py (Y929 food-only, N853 flag), build_items_xlsx() baseline (main.py) (+25 more)

### Community 8 - "CDS List Parser Tests"
Cohesion: 0.08
Nodes (16): Counter, Unit tests for cds_list — the CDS Items export -> V-lookup list parser. Covers…, row(), test_added_entries_survive_the_derived_round_trip(), test_build_list_folds_lines_into_one_entry_per_code(), test_build_list_keeps_each_rex_separate(), test_choose_description_counts_casing_variants_together(), test_choose_description_is_deterministic_on_a_tie() (+8 more)

### Community 9 - "Read API Endpoints"
Cohesion: 0.11
Nodes (27): get, api_list_all_companies(), api_list_clients(), api_list_commodity_codes(), api_list_users(), api_me(), api_storage_usage(), export_full() (+19 more)

### Community 10 - "Phase B Security Review"
Cohesion: 0.21
Nodes (17): Session cookie bump is_session -> is_session_v2, Phase B deploy order and rollback, Supabase user JWT design (role=authenticated, app_role), Migration 003: change_own_password SECURITY DEFINER RPC, Migration 003a: rename role -> app_role in 8 policies, Phase B: per-request user-scoped Supabase client, RLS policies (001_enable_rls + 002_role_clamp_fix), Storage stays on service-role (path-prefix isolation) (+9 more)

### Community 11 - "Login Rate Limiter Tests"
Cohesion: 0.21
Nodes (15): Standalone smoke tests for the login rate limiter. Runs in-process against…, NAT scenario: bob evicted under load, colleagues can still succeed-clean., Round-3 fix: 429 logs include IP + count but never the username., Round-2 fix: alice succeeding shouldn't free bob's contributions., A legitimate success doesn't relieve attacker pressure on the IP cap., Round-3 fix: user-dict eviction cascades into the IP bucket so the success path…, _reset(), _step() (+7 more)

### Community 12 - "Login & Security Hardening"
Cohesion: 0.14
Nodes (16): add_noindex_header(), api_login(), api_logout(), _check_login_rate_limit(), _clear_login_failures(), _client_ip(), _prune_attempts(), One-time boot sweep: jobs still 'running'/'queued' in the DB belong to the… (+8 more)

### Community 13 - "Admin Provisioning & Job Queue"
Cohesion: 0.20
Nodes (15): api_add_user(), api_create_company(), api_import_commodity_codes(), confirm_memory(), _enqueue_job(), Path, Super-admin only: provision a new customer company + first admin user., Bulk-add a commodity-code list (.xlsx or .csv) to the company list. The sheet… (+7 more)

### Community 14 - "Working Sheet Layout Tests"
Cohesion: 0.33
Nodes (14): _headers(), Working-sheet layout tests — the commodity code as two customs columns. The…, _row(), _sheet(), test_a_10_digit_code_is_written_as_8_plus_2(), test_a_clean_code_is_not_highlighted(), test_a_disagreement_on_the_code_highlights_both_parts(), test_a_repaired_leading_zero_is_flagged_not_silent() (+6 more)

### Community 15 - "UI Pages & Landing Site"
Cohesion: 0.16
Nodes (14): python-multipart==0.0.20 pin, App shell (index.html SPA layout), Commodity codes page (shared V-lookup editor + import), Dashboard page (drop zone, stats, jobs, recent invoices), Invoices page (verified / needs review / failed tabs), UK Tariff lookup page, Resolve subcode modal, Login page (login.html) (+6 more)

### Community 16 - "Claude Invoice Extraction"
Cohesion: 0.21
Nodes (13): AsyncAnthropic, extract_pdf_text(), Extract plain text from PDF locally (saves ~80% input tokens vs sending raw…, Frame extracted invoice text as untrusted data inside <invoice_text> tags.…, Run extraction on pre-extracted PDF text with prompt caching. Run A writes…, Send file + prompt to Claude and return raw text response. For PDFs: extract…, Structured extraction using tool_use. Hybrid input: when file_bytes + mime…, Structured extraction using tool_use on a raw file (used when pdfplumber can't… (+5 more)

### Community 17 - "Delete Endpoints"
Cohesion: 0.17
Nodes (12): delete, api_delete_client(), api_delete_commodity_code(), api_delete_company(), api_delete_user(), delete_invoice_endpoint(), delete_job(), delete_memory_entry() (+4 more)

### Community 18 - "User Admin Privilege Tests"
Cohesion: 0.29
Nodes (11): main(), Standalone smoke tests for user-management endpoints — privilege gate. Runs in-…, _step(), _stub_db(), test_admin_can_create_another_admin(), test_admin_can_create_normal_user(), test_admin_cannot_create_super_admin(), test_empty_username_rejected() (+3 more)

### Community 19 - "CI, Deployment & Config"
Cohesion: 0.25
Nodes (11): CI Workflow (GitHub Actions), Phase B lint step (check_no_raw_sb.sh), pyflakes lint step, Unit tests step (pytest, ignores tests_user_admin.py), graphify usage rules, Archive index, FORCE_ADMIN_RESET break-glass flag, Dev requirements (pytest, pyflakes) (+3 more)

### Community 20 - "Storage Cleanup Script"
Cohesion: 0.29
Nodes (10): datetime, _human(), _list_folder(), _load_env(), main(), _parse_created(), Bulk-clean Supabase Storage to get back under the free-tier quota. The app…, Recursively collect file objects (not folders) with full paths. (+2 more)

### Community 21 - "Auth Dependencies & JWT Design"
Cohesion: 0.18
Nodes (11): admin_authed / super_admin_authed, api_change_password endpoint, api_logout (intentionally session-only), api_me endpoint (GET /api/me), authed() yield dependency, mint_user_jwt (auth_jwt.py), H2: ContextVar reset vs StreamingResponse, N1: api_me / api_logout missing from endpoint sweep (+3 more)

### Community 22 - "Phase B Prerequisites & DAL Pattern"
Cohesion: 0.24
Nodes (11): CI lint: no raw db.sb.* / require_auth / inline request.session, ContextVar DAL pattern (_client() / _current_client), Queue worker stays on service-role, _sb_service (service-role client), Phase B prerequisites checklist, ensure_default_admin() startup bootstrap, Login flow keeps service-role, Phase C: add company_id filters to get_user_by_id / get_job (+3 more)

### Community 23 - "Items Export Previous-Doc Tests"
Cohesion: 0.51
Nodes (10): _cell(), _items(), MultiFreight Items export — the DE 2/1 previous-document columns. Every goods…, _row(), test_a_fee_row_is_not_an_items_line(), test_an_unknown_invoice_number_leaves_the_reference_blank_not_guessed(), test_every_line_refers_back_to_the_invoice_as_z_380(), test_only_the_first_previous_document_slot_is_used() (+2 more)

### Community 24 - "Tariff Cache Refresh"
Cohesion: 0.24
Nodes (10): _auto_match_from_tariff(), lookup_tariff(), If an entry has no matched_code yet but the tariff has exactly one sub-code,…, Refresh tariff data from gov.uk for all memory entries. Also fills in…, Refetch tariff data for entries whose cache is older than 30 days, and auto-…, Return True if the cached tariff entry was fetched more than…, Wrapper: calls _lookup_tariff_raw and stamps fetched_at for cache aging., refresh_memory_tariff() (+2 more)

### Community 25 - "Invoice Processing Pipeline"
Cohesion: 0.22
Nodes (10): build_excel(), extract_value_number(), parse_structured_rows(), _process_invoice(), _queue_worker(), Convert tool_use structured output → list of canonical column dicts. Numbers…, Parse a numeric cell from Claude's TSV. Claude is instructed to output numbers…, Return the REX number printed in `text` (e.g. 'ITREXIT06167560157'), or ''. (+2 more)

### Community 26 - "Row Normalisation & A/B Match"
Cohesion: 0.20
Nodes (10): _norm_num(), normalise_row(), parse_totals(), Map any variant header names to the canonical column names, and repair a common…, Normalize a numeric string for comparison (2-decimal string). Parsing itself is…, Stable sort key for a row (commodity code + description)., Check if two extractions match on key numeric/code fields, after sorting rows…, Parse the Run C totals output into a dict of normalized numbers. Keys:… (+2 more)

### Community 27 - "Write API Endpoints"
Cohesion: 0.22
Nodes (9): api_change_password(), api_create_client(), api_update_client(), api_upsert_commodity_code(), _clean_str(), Change a user's password. Self-change goes through the SECURITY DEFINER RPC…, Add/update one V-lookup row. Keyed on full_code (upsert): saving an existing…, verify_password() (+1 more)

### Community 28 - "UK Tariff API Lookup"
Cohesion: 0.25
Nodes (8): _extract_commodity_desc(), _extract_duty_vat(), _lookup_tariff_raw(), Look up a numeric tariff code directly. Handles 4-10 digit codes: - 10 digits →…, Extract duty and VAT strings from a UK Tariff API commodity response. The API…, Extract the commodity description from included items., Query UK Trade Tariff API for duty/VAT rates + possible sub-codes. EU invoices…, _tariff_code_lookup()

### Community 29 - "Per-Request User JWT"
Cohesion: 0.29
Nodes (6): mint_user_jwt(), Any, JWT minting for the per-request user-scoped Supabase client. Phase B switch:…, Mint a Supabase-compatible HS256 JWT for the current request's user. `ctx` must…, authed(), Authenticated dep — binds a per-request user-scoped Supabase client. 1. Reads…

### Community 30 - "Storage Quota Incident"
Cohesion: 0.53
Nodes (6): Session handoff 2026-07-10 (storage-quota recovery), scripts/storage_cleanup.py (bulk purge, dry-run default), Supabase free-tier storage quota incident (23 GB / 5 GB), passlib 1.7.4 + bcrypt 4.0.1 pin, Admin panel (provision company, companies list, storage purge), STORAGE_RETENTION_DAYS env var (daily purge)

### Community 31 - "Storage Retention Purge"
Cohesion: 0.33
Nodes (6): api_storage_purge(), purge_old_storage(), Super-admin: delete upload/export files older than `days` (default = the…, Delete upload/export objects older than `days`. Returns a per-bucket summary.…, Daemon: run the storage purge shortly after boot, then once a day. Guarded so a…, _retention_worker()

### Community 32 - "Memory Cleanup & Resolve"
Cohesion: 0.33
Nodes (6): cleanup_invalid_memory(), is_real_commodity_code(), Return True only for strings that look like real HS/commodity codes. Real…, Mark a subcode_needed invoice as verified after manual review. This also adds…, Remove memory entries whose 'code' is not a real commodity code (SKUs, short…, resolve_invoice()

### Community 33 - "Totals Comparison"
Cohesion: 0.33
Nodes (6): compare_totals(), _parse_num(), Parse a numeric string to float WITHOUT rounding. Handles EU (1.234,56) and US…, Sum a numeric column across rows, return normalized string (2 decimals). Sums…, Compare summed rows against invoice totals. Returns a dict per field:…, sum_rows_numeric()

### Community 34 - "Sub-code Matching"
Cohesion: 0.40
Nodes (5): _first_text(), match_subcodes(), Any, First text block's content, or '' on a refusal / no text block. Text-mode calls…, For each product that has multiple possible sub-codes, ask Claude to pick the…

### Community 35 - "User-Scoped Supabase Client"
Cohesion: 0.67
Nodes (4): make_user_client (anon key + Bearer JWT), Staging gates (JWT shape, storage, TTL, perf), H3: make_user_client opens an httpx pool per request, SUPABASE_ANON_KEY env var

### Community 36 - "Storage Listing"
Cohesion: 0.50
Nodes (4): One folder's entries (files + subfolders), following pagination., Recursively list every file object in a bucket. Objects are stored as…, storage_list_all(), _storage_list_folder()

### Community 37 - "Default Admin Bootstrap"
Cohesion: 0.50
Nodes (4): ensure_default_admin(), Make sure the default admin user exists with a correct password hash. Runs once…, Best-effort admin bootstrap that NEVER crashes process startup. On Supabase's…, _try_ensure_default_admin()

### Community 38 - "Login Failure Buckets"
Cohesion: 0.50
Nodes (4): _evict_if_full(), When we hit the size cap, drop empty entries first, then oldest. Returns a list…, Record a failed attempt in both buckets. The SAME timestamp is pushed into both…, _record_login_failure()

### Community 39 - "A/B Cell Disagreements"
Cohesion: 0.50
Nodes (4): find_cell_disagreements(), _norm_desc_for_match(), Aggressive description normalizer for A/B row matching only. Lowercase, keep…, For each row in rows_a, return the set of column names whose value disagrees…

### Community 40 - "Commodity List Lookup"
Cohesion: 0.50
Nodes (4): lookup_commodity_list(), _norm_general_code(), Digits-only key for the client-list VLOOKUP. Lists store the general code zero-…, Commodity-list equivalent of lookup_tariff. VLOOKUP the invoice's general…

### Community 41 - "User-Facing Job Errors"
Cohesion: 0.67
Nodes (3): Exception, Map an internal exception to a safe, still-actionable user message. Raw…, _user_facing_job_error()

## Ambiguous Edges - Review These
- `Tech stack: FastAPI + Claude + Supabase + Render` → `Settings page (users, change password, system info)`  [AMBIGUOUS]
  invoiceflow/static/index.html · relation: conceptually_related_to

## Knowledge Gaps
- **18 isolated node(s):** `ACTIONS`, `knownJobs`, `_memoryItems`, `_commodityCodes`, `_clients` (+13 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 249 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **22 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `Tech stack: FastAPI + Claude + Supabase + Render` and `Settings page (users, change password, system info)`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `Commodity-code list (V-lookup)` connect `MultiFreight Rules Engine Plan` to `CDS Export List Parser`, `CI, Deployment & Config`, `UI Pages & Landing Site`?**
  _High betweenness centrality (0.222) - this node is a cross-community bridge._
- **Why does `Invoice Sorter (project)` connect `CI, Deployment & Config` to `UI Pages & Landing Site`, `Auth Dependencies & JWT Design`, `MultiFreight Rules Engine Plan`?**
  _High betweenness centrality (0.140) - this node is a cross-community bridge._
- **Why does `Archive index` connect `CI, Deployment & Config` to `Phase B Security Review`, `Storage Quota Incident`, `Phase B Prerequisites & DAL Pattern`, `MultiFreight Rules Engine Plan`?**
  _High betweenness centrality (0.059) - this node is a cross-community bridge._
- **What connects `ACTIONS`, `knownJobs`, `_memoryItems` to the rest of the system?**
  _18 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Frontend SPA (app.js)` be split into smaller, more focused modules?**
  _Cohesion score 0.08701923076923077 - nodes in this community are weakly interconnected._
- **Should `CDS Export List Parser` be split into smaller, more focused modules?**
  _Cohesion score 0.05411764705882353 - nodes in this community are weakly interconnected._