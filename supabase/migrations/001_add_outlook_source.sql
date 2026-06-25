-- Migration: Add password, source, extra_data columns to tokens table
-- untuk mendukung Outlook Account Pool

-- Tambah kolom password untuk nyimpen password akun
ALTER TABLE tokens ADD COLUMN IF NOT EXISTS password TEXT DEFAULT '';

-- Tambah kolom source untuk nandain asal akun ('' = biasa, 'outlook' = outlook pool)
ALTER TABLE tokens ADD COLUMN IF NOT EXISTS source TEXT DEFAULT '';

-- Tambah kolom extra_data untuk metadata tambahan (JSON string)
ALTER TABLE tokens ADD COLUMN IF NOT EXISTS extra_data TEXT DEFAULT '{}';

-- Tambah index di source untuk query filtering
CREATE INDEX IF NOT EXISTS idx_tokens_source ON tokens(source);

-- Index untuk email biar cepet filtering domain
CREATE INDEX IF NOT EXISTS idx_tokens_email ON tokens(email);

-- Update status outlook untuk akun yang emailnya @outlook.com
UPDATE tokens SET source = 'outlook' WHERE email LIKE '%@outlook.com' AND (source IS NULL OR source = '');
