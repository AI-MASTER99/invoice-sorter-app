# MultiFreight CDS Items — per-code "rule engine" implementation plan

Status: **DESIGN + PHASE 1 (research)**. Nothing here is deployed.
Firm constraints (do not break):
- **Never guess.** Anything not determinable from authoritative data + the invoice → flag for a human.
- **Local only until the user says "last-call".** No commit-push/deploy before that.
- **Error clarity is goal #1.** Flags must be clear and directly addressable.
- **Source of truth = UK Trade Tariff (official API), never an LLM's memory.**

---

## 1. Goal

On the MultiFreight CDS "Items" export, auto-fill the **per-commodity-code "other info"** that today is left blank or hardcoded:
- required **documents / certificates** (e.g. N853 CHED-P, C085 phyto, Y-exemptions),
- **additional codes** (national additional code / VAT, anti-dumping TARIC additional codes),
- **supplementary units** (e.g. number of items, litres of alcohol),
- any **restrictions** that must surface.

Reliably, per the constraints above.

## 2. Baseline (already built)

`build_items_xlsx()` in `invoiceflow/main.py` already fills:
- invoice-derived columns (desc, weights, value, packages, commodity code split 8+2),
- hardcoded defaults (`_ITEMS_DEFAULTS`: procedure 4000, valuation method 1, package PK, marks N/M),
- **origin-based preference rule** — EU origin → `[4/17]=300` + `[5/16]` (member state, deliberate) + **`U116`** doc (id = invoice number, status `JE`); non-EU → `100`. *(Product-independent — works for food and non-food alike.)*
- always-present docs `N935` (slot 01) + `Y929` (slot 02).

DB: `client_products` (migration 004) holds `general_code, full_code, description` and currently-unused `coo, cop, nat_add_code, documents jsonb, taric_code, mop`. Per-client lists. DE 2/3 has **3 document slots** (01/02/03) in the template.

## 3. Design (agreed with user)

Three categories for each per-code field:

| Cat | Meaning | Storage | Resolved using |
|-----|---------|---------|----------------|
| 1 | Always the same for that code | DB value | — |
| 2 | Conditional on values already in the invoice (origin/weight/value/alcohol%/qty) | DB **rule** (WHEN cond → outcome, with else/fallback) | **local** evaluation, no runtime API |
| 3 | Not determinable from the invoice | — | **flag for human** (never guess) |

- Rules are **DATA in the DB** (not hardcoded) so new rules need no code change.
- Rules are **global** (customs law is universal across clients) **+ per-client/product overrides** for cases where a client always makes a different valid choice.
- The **UK Tariff API** is used to *author* rules (the real thresholds/country-lists/doc-codes) and to *periodically re-validate* them (codes drift — e.g. C085 replaced N851/N002 in Feb 2026).
- Evaluation is local & cheap; the **rigor lives in authoring** the rule correctly.

## 4. Phases

### Phase 1 — UK Trade Tariff API capability research (READ-ONLY) ← current
Find out, from authoritative sources + real sample calls:
- Does the public API exist, base URL, auth, rate limits, stability. (Expected: `api.trade-tariff.service.gov.uk`.)
- For a commodity code (+ origin), what is returned: **measures**, **measure conditions** (document codes / certificate requirements / exemption codes / thresholds), **supplementary units**, **additional codes** (VAT/excise/anti-dumping), duty rates.
- How are conditions/thresholds represented in the JSON (so we can model Cat-2 rules).
- Real sample responses for a **food** code (dairy, e.g. 0406…) and a **non-food** code (escalator/lift, e.g. 8428…).
- Output: capability report → what maps to Cat 1/2/3, and exactly what the DB schema must hold.

### Phase 2 — Data model design  ✅ migration written (005), not yet applied
**Refinement from Phase 1:** the API already carries the conditional logic (measure_conditions, permutation groups, origin scope, thresholds), so we do NOT hand-author a big ruleset — we interpret the API response. The DB shrinks to three tables (`invoiceflow/migrations/005_add_tariff_engine.sql`):
- `tariff_cache` (GLOBAL) — raw API responses keyed by (commodity_code, geographical_area_id), ~24h `expires_at`. Public data → permissive RLS.
- `tariff_reference` (GLOBAL) — decode dictionaries (measure_type, condition codes, certificates, units…) by (kind, code), refreshed weekly. Public data → permissive RLS.
- `client_tariff_overrides` (TENANT-scoped) — the user's "rules as data": per (company, client, commodity_code, origin?, measure_type?) a `decision` jsonb for the choice-cases the API can't resolve alone. Tenant RLS + super_admin (cf. 004).
- Invoice-row fields usable as condition inputs: origin, net/gross kg, value, currency, qty, packages, commodity code, description (alcohol% only if reliably extractable → else flag).
- Migration follows the repo's `00X_*.sql` + `_rollback.sql` + `_verify.sql` pattern.

### Phase 3 — Author the real rules (populate)
- Using Phase-1 findings, author rules for the codes Apicella actually imports (from their existing list) — and the generic global ones.
- **Human-review checkpoint**: present proposed rules for the user's approval before storing (correctness matters).

### Phase 4 — Engine + integration
- Pure function `resolve_rules(rules, invoice_row) -> {documents[], additional_codes[], suppl_units[], flags[]}`.
- Integrate into `build_items_xlsx`: feed engine output into the DE 2/3 doc slots + additional-code / suppl-unit columns; keep N935/U116; **make Y929 conditional** (food chapters 01–24).
- Handle the **3-slot DE 2/3 limit**: deterministic priority + **flag (never silently drop)** if more than 3 docs.
- Surface Cat-3 flags clearly (reuse the NOT-IN-LIST / review mechanism).

### Phase 5 — Verification & safety
- Unit tests (like the U116 smoke test) for: food code with N853-vs-Y930 conditional, non-food code, a weight-threshold rule, an unknown code → flag, >3 docs → flag.
- Drift re-validation: periodic API re-check that flags changed requirements.
- No silent caps anywhere (log/flag anything dropped).

## 5. Risks / open items
- 3-slot DE 2/3 limit → needs prioritization + flagging.
- API rate limits / availability → consider caching / periodic snapshot rather than per-line live calls.
- Rule drift → Phase-5 re-validation.
- U117 (importer's knowledge) / U118 (multiple shipments) variants deferred — only U116 today.
- Some condition inputs (e.g. alcohol%) may not be reliably on the invoice → extract or flag.
