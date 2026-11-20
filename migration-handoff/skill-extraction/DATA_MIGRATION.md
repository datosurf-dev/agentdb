# DATA_MIGRATION — Preserving Existing Skill Data

AgentDB never deletes existing skill data as part of this extraction. The
skill tables live in a **separate SQLite file** and are left untouched on disk.
This document explains where the data is, how to export it, and how to restore it.

## 1. Where skill data lives

| Artifact | Location | Notes |
|---|---|---|
| Skill records | `<db_dir>/skills.sqlite` → `skills` table | separate file from `agentdb.sqlite` |
| Advisory locks | `<db_dir>/skills.sqlite` → `skill_locks` table | transient |
| Skill embeddings | `<db_dir>/embeddings.db` → rows with `namespace = "skills"`, key `"{id}:{version}"` | only when embeddings enabled |
| Skill proposals | `agentdb.sqlite` → memory records with key prefix `proposal/` | proposals were stored as memory records |

> `<db_dir>` is the directory of the configured DB path. For a workspace this is
> typically `<memory_dir>/database/`. For `:memory:` DBs there is no file (nothing
> to migrate — data was never persisted).

**Nothing changes these files during extraction.** Skill code is removed; the
SQLite data file remains valid and readable.

## 2. Export procedure

Use the standalone exporter (does not import `agentdb`):

```bash
python migration-handoff/skill-extraction/export_skills.py \
    /path/to/database/skills.sqlite \
    -o skills_export.jsonl
```

Output: a header line (columns, lock snapshot) followed by one JSON object per
skill row. JSON-encoded fields (`scope`, `health_score_reasons`, `metadata`) are
decoded to native JSON.

Verify the count matches the DB (stderr reports exported rows):

```bash
sqlite3 /path/to/database/skills.sqlite "SELECT COUNT(*) FROM skills;"
```

## 3. Verifying the export

```bash
# Row count parity
grep -c '"_type": "skill"' skills_export.jsonl     # should equal DB count

# Spot-check content integrity
python - <<'PY'
import json, sqlite3
db = sqlite3.connect("/path/to/database/skills.sqlite")
exp = [json.loads(l) for l in open("skills_export.jsonl") if '"_type": "skill"' in l]
for r in exp:
    row = db.execute("SELECT content FROM skills WHERE id=? AND version=?", (r["id"], r["version"])).fetchone()
    assert row and row[0] == r["content"], f"mismatch {r['id']}:{r['version']}"
print(f"verified {len(exp)} skills, content hashes match")
PY
```

## 4. Restoration instructions

Skill rows are self-contained. To restore into a **future** Skill-Governance DB
with the same schema, re-insert from JSONL:

```python
import json, sqlite3
conn = sqlite3.connect("skills_restored.sqlite")
conn.execute("""CREATE TABLE skills (id TEXT, name TEXT, content TEXT,
  authored_by TEXT, scope TEXT, version TEXT, archived INTEGER,
  health_score REAL, health_score_reasons TEXT, created_at TEXT,
  updated_at TEXT, last_retrieved TEXT, parent_skill_id TEXT,
  origin TEXT DEFAULT 'human', validation_score REAL, metadata TEXT,
  PRIMARY KEY (id, version))""")
for line in open("skills_export.jsonl"):
    r = json.loads(line)
    if r.get("_type") != "skill":
        continue
    conn.execute(
      "INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
      (r["id"], r["name"], r["content"], r["authored_by"],
       json.dumps(r.get("scope", [])), r["version"], r.get("archived", 0),
       r.get("health_score", 1.0), json.dumps(r.get("health_score_reasons", [])),
       r["created_at"], r["updated_at"], r.get("last_retrieved"),
       r.get("parent_skill_id"), r.get("origin", "human"),
       r.get("validation_score"), json.dumps(r.get("metadata", {}))))
conn.commit()
```

**Skill embeddings** are *not* exported as vectors (model-dependent). Re-embed in
the new package using its own model, or restore the raw `embeddings.db` if the
same model/dimensions are reused.

**Proposals** stored as `proposal/` memory records are AgentDB memories. They are
not converted to skills. If desired, list them with:

```bash
sqlite3 /path/to/database/agentdb.sqlite \
  "SELECT key, payload FROM memories WHERE key LIKE 'proposal/%' AND archived=0;"
```
(actual column layout depends on the `memories` table — inspect with `.schema memories`).

## 5. No silent conversion or deletion

- No skill record is ever turned into a memory by this extraction.
- `purge_session()` documentation already states session-derived skills are kept;
  that behavior is unaffected (skill code is simply gone).
- The `skills.sqlite` file is never truncated or dropped by AgentDB code or tests.
