-- settings: one row per (tab, key) version of the settings sheet (SPEC 5, 6).
-- SPEC 5: "Every version is stored with effective_from and effective_to." The version in
-- force has effective_to NULL. key is the General key, or the row's natural key on other
-- tabs; values is the raw sheet row as a JSON object of column to text.
CREATE TABLE IF NOT EXISTS us_outbound.settings (
  tab text NOT NULL,
  "key" text NOT NULL,
  "values" jsonb,
  effective_from timestamptz NOT NULL,
  effective_to timestamptz,
  synced_at timestamptz,
  PRIMARY KEY (tab, "key", effective_from)
);
COMMENT ON TABLE us_outbound.settings IS 'One row per setting version (SPEC 6). settings_sync validates every tab first; a tab that fails keeps the version in force (SPEC 5).';
COMMENT ON COLUMN us_outbound.settings.tab IS 'One of: General, Signals, Angles, Industries, States, Roles, Copy, Mailboxes, Overrides, Tests, _order. An _order row keeps one tab''s keys in sheet order (key = the tab).';
COMMENT ON COLUMN us_outbound.settings."key" IS 'The General key, or the row''s natural key (settings.validate.KEY_COLUMNS, joined with |).';
COMMENT ON COLUMN us_outbound.settings."values" IS 'JSON: the raw sheet row, column to text.';
COMMENT ON COLUMN us_outbound.settings.effective_to IS 'NULL while the version is in force.';
