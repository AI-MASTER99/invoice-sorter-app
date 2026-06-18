-- ============================================================
-- Verify Migration 005
-- ============================================================
-- Expected:
--   tables : tariff_cache, tariff_reference, client_tariff_overrides   (3 rows)
--   rls    : all three relrowsecurity = true                           (3 rows)
--   polices: tariff_cache_authenticated_all,
--            tariff_reference_authenticated_all,
--            client_tariff_overrides_tenant_all,
--            client_tariff_overrides_super_admin                       (4 rows)
-- ============================================================

-- Tables exist
SELECT tablename
FROM pg_tables
WHERE schemaname = 'public'
  AND tablename IN ('tariff_cache', 'tariff_reference', 'client_tariff_overrides')
ORDER BY tablename;

-- RLS enabled
SELECT relname, relrowsecurity
FROM pg_class
WHERE relname IN ('tariff_cache', 'tariff_reference', 'client_tariff_overrides')
ORDER BY relname;

-- Policies present
SELECT tablename, policyname
FROM pg_policies
WHERE tablename IN ('tariff_cache', 'tariff_reference', 'client_tariff_overrides')
ORDER BY tablename, policyname;
