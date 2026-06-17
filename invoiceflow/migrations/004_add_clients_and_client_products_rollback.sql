-- ============================================================
-- Rollback for Migration 004 — drop clients + client_products
-- ============================================================
-- Dropping the tables also drops their policies and indexes.
-- client_products is dropped first (it FKs to clients).
-- ============================================================

BEGIN;

DROP TABLE IF EXISTS client_products;
DROP TABLE IF EXISTS clients;

COMMIT;
