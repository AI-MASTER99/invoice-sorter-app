-- ============================================================
-- Rollback for Migration 005 — drop the tariff rule-engine tables
-- ============================================================
-- Dropping the tables also drops their policies and indexes.
-- client_tariff_overrides is dropped first (it FKs to clients).
-- ============================================================

BEGIN;

DROP TABLE IF EXISTS client_tariff_overrides;
DROP TABLE IF EXISTS tariff_reference;
DROP TABLE IF EXISTS tariff_cache;

COMMIT;
