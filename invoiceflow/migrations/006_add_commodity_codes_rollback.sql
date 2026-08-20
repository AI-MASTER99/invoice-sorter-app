-- ============================================================
-- Rollback for Migration 006 — drop commodity_codes
-- ============================================================
-- Dropping the table also drops its policies and indexes.
-- client_products was never touched by 006, so the per-client
-- lists are still intact after this rollback.
-- ============================================================

BEGIN;

DROP TABLE IF EXISTS commodity_codes;

COMMIT;
