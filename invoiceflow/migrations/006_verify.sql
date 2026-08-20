-- ============================================================
-- Verify Migration 006
-- ============================================================
-- Expected:
--   tables : commodity_codes                       (1 row)
--   rls    : relrowsecurity = true                 (1 row)
--   polices: commodity_codes_tenant_all,
--            commodity_codes_super_admin           (2 rows)
--   seed   : commodity_codes row count equals the number of
--            DISTINCT (company_id, full_code) in client_products
-- ============================================================

-- Table exists
SELECT tablename
FROM pg_tables
WHERE schemaname = 'public'
  AND tablename = 'commodity_codes';

-- RLS enabled
SELECT relname, relrowsecurity
FROM pg_class
WHERE relname = 'commodity_codes';

-- Policies present
SELECT tablename, policyname
FROM pg_policies
WHERE tablename = 'commodity_codes'
ORDER BY policyname;

-- Seed sanity: both counts should match
SELECT
  (SELECT count(*) FROM commodity_codes)                                    AS seeded,
  (SELECT count(DISTINCT (company_id, full_code)) FROM client_products)    AS source_distinct;
