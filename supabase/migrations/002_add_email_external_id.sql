-- Migration: Add external_id column to emails table
-- Dipakai untuk dedupe email yang disinkron dari IMAP (Outlook)
-- via job /api/cron/outlook-sync, supaya email yang sama tidak
-- tersimpan berkali-kali tiap kali sync job jalan.

ALTER TABLE emails ADD COLUMN IF NOT EXISTS external_id TEXT;

-- Unique index parsial: hanya email dari IMAP (yang punya external_id)
-- yang harus unik. Email dari webhook biasa (external_id NULL) tidak kena.
CREATE UNIQUE INDEX IF NOT EXISTS idx_emails_external_id
    ON emails (external_id)
    WHERE external_id IS NOT NULL;
