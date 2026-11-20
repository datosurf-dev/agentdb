# Migration and Rollback

**Scope:** Additive schema changes for distilled knowledge (ADB-06).
**Change set:** three nullable/defaulted columns on `memories`.

## Schema change

| Column | Type | Default | Purpose |
|---|---|---|---|
| `kind` | TEXT | `'source'` | `source` vs `derived_canonical` |
| `producer` | TEXT | NULL | `consolidation`, etc. |
| `derivation` | TEXT (JSON) | `'{}'` | per-source digests + strategy |

No table is dropped, no column is removed, no data is rewritten. The migration
is the same idempotent pattern already used for `consolidated`,
`source_record_ids`, `session_id` (`MemoryStore.__init__` → `_MEMORIES_MIGRATION_COLUMNS`):

```python
for column, coltype in _MEMORIES_MIGRATION_COLUMNS:
    try:
        conn.execute(f"ALTER TABLE memories ADD COLUMN {column} {coltype}")
    except sqlite3.OperationalError:
        pass  # column already exists
```

`ALTER TABLE ... ADD COLUMN` with a constant default is O(1) metadata-only in
SQLite; existing rows read back with the default (`kind='source'`,
`derivation='{}'`), so **legacy records are unaffected and fully readable**.

## Forward migration (automatic)

Opening any existing database with this version applies the columns on first
`MemoryStore` construction. Verified:

- Old-schema DB (only core columns) → opens, legacy row readable with `kind='source'`.
- New canonical features work on the migrated DB.
- Canonicals persist across process reopen.

## Rollback

Because the change is purely additive, rollback is non-destructive:

1. **Code rollback:** check out the prior version. Older code ignores the new
   columns (it `SELECT *` and reads only known keys) — the extra columns are
   harmless. No data loss.
2. **Optional column removal** (only if a clean schema is required):
   ```sql
   -- SQLite < 3.35 cannot DROP COLUMN; rebuild is required.
   -- Safe only after confirming no derived_canonical rows are relied upon.
   ```
   Recommended: leave the columns in place; they are inert to older code.

## Backup procedure (before any migration on production data)

```bash
sqlite3 /path/to/agentdb.sqlite ".backup '/path/to/agentdb.backup.sqlite'"
sha256sum /path/to/agentdb.sqlite /path/to/agentdb.backup.sqlite
```

Restore = stop process, replace the db file with the backup, restart.

## Tests

- `test_in_memory_db.py` — in-memory DB works without creating files (ADB-12).
- Legacy-corpus migration verified manually (see AGENTDB_ALIGNMENT_REPORT §Migration).
- Concurrency/rollback: writes use a single connection context with
  commit-on-success / rollback-on-error (`core/sqlite_connection.connected`).
