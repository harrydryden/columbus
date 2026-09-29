-- settings: one row per (tab, key) version of the settings sheet (SPEC 5, 6).
-- SPEC 5: "Every version is stored with effective_from and effective_to." The version in
-- force has effective_to NULL. key is the General key, or the row's natural key on other
-- tabs; values is the raw sheet row as a JSON object of column to text.
CREATE TABLE IF NOT EXISTS `{project}.us_outbound.settings` (
  tab STRING NOT NULL OPTIONS (description = "One of: General, Signals, Angles, Industries, States, Roles, Copy, Mailboxes, Overrides, Tests."),
  `key` STRING NOT NULL OPTIONS (description = "The General key, or the row's natural key (settings.validate.KEY_COLUMNS, joined with |)."),
  `values` STRING OPTIONS (description = "JSON text: the raw sheet row, column to text."),
  effective_from TIMESTAMP NOT NULL,
  effective_to TIMESTAMP OPTIONS (description = "NULL while the version is in force."),
  synced_at TIMESTAMP
)
OPTIONS (
  description = "One row per setting version (SPEC 6). settings_sync validates every tab first; a tab that fails keeps the version in force (SPEC 5)."
);
