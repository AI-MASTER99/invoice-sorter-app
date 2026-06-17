-- ============================================================
-- Verify Migration 004
-- ============================================================
-- Expected:
--   tables : clients, client_products            (2 rows)
--   rls    : both relrowsecurity = true          (2 rows)
--   polices: clients_tenant_all, clients_super_admin,
--            client_products_tenant_all, client_products_super_admin  (4 rows)
-- ============================================================

-- Tables exist
SELECT tablename
FROM pg_tables
WHERE schemaname = 'public'
  AND tablename IN ('clients', 'client_products')
ORDER BY tablename;

-- RLS enabled
SELECT relname, relrowsecurity
FROM pg_class
WHERE relname IN ('clients', 'client_products')
ORDER BY relname;

-- Policies present
SELECT tablename, policyname
FROM pg_policies
WHERE tablename IN ('clients', 'client_products')
ORDER BY tablename, policyname;
