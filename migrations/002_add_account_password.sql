-- 002_add_account_password.sql
-- Add password column to accounts for automated Redroid ADB login

ALTER TABLE accounts
ADD COLUMN IF NOT EXISTS password TEXT;
