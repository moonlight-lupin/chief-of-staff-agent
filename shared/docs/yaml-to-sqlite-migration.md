# YAML → SQLite Migration Guide

This guide helps the agent decide when to migrate from YAML data files to SQLite, and how to do it without breaking skills that depend on the data.

## When to Migrate

### Signs it's time

| Signal | Threshold | Why |
|---|---|---|
| File size | Any single YAML file > 500KB | Parse time becomes noticeable, editor lag |
| Record count | Any single file > 500 records | YAML linear scan is slow for lookups |
| Query patterns | Frequent filtering/sorting by field | SQL indexes beat in-memory YAML filtering |
| Concurrent writes | Multiple skills writing same file | YAML has no locking — risk of overwrites |
| User complaints | "It's slow" or "the file is huge" | Practical signal, not theoretical |

### When NOT to migrate

- **< 100 records** — YAML is fine, don't over-engineer
- **Single-user, sequential access** — no concurrency concern
- **User manually edits the YAML** — human readability is the priority
- **Only one skill reads the data** — no cross-skill query optimization needed

### Rule of thumb

> If the agent is loading a YAML file and filtering/sorting it in Python more than once per session, and the file has > 200 records, migrate that specific data store to SQLite. Leave the rest as YAML.

## What Can Be Migrated

| Data Store | Current File | Likely First to Migrate | Why |
|---|---|---|---|
| Pipeline | `pipeline.yaml` | Yes — grows with deals, frequently queried by stage/client | Frequent filtering by stage, stale detection requires date math on all records |
| Invoices | `invoices.yaml` | Yes — grows linearly, P&L requires aggregation | P&L report scans all records every run |
| Expenses | `expenses.yaml` | Yes — grows linearly, category aggregation | Same as invoices |
| To-Dos | `todos.yaml` | Maybe later — small, infrequent queries | Usually < 100 items, fine as YAML |
| Company config | `company.yaml` | **Never** — config stays YAML | Human-edited, small, rarely changes |
| Drive map | `drive-map.yaml` | **Never** — config stays YAML | Human-edited, small, rarely changes |
| Queries | `queries.yaml` | **Never** — config stays YAML | Human-edited, small, rarely changes |
| Wiki | `wiki/` markdown | **Never** — markdown files by design | The wiki IS markdown, that's the point |

## How to Migrate

### Step 1: Create the SQLite database

```python
import sqlite3
import json
from pathlib import Path

DB_PATH = Path(project_root) / "chief_of_staff.db"

def init_db(db_path):
    """Create tables if they don't exist. Safe to call repeatedly."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")  # concurrent reads during writes
    conn.execute("PRAGMA foreign_keys=ON")

    # Pipeline / CRM
    conn.execute("""
        CREATE TABLE IF NOT EXISTS deals (
            id TEXT PRIMARY KEY,
            client_name TEXT NOT NULL,
            contact_name TEXT,
            contact_email TEXT,
            stage TEXT NOT NULL,
            value REAL,
            currency TEXT DEFAULT 'SGD',
            created TEXT NOT NULL,
            last_activity TEXT,
            notes TEXT,
            documents_json TEXT,    -- JSON array of {type, path, status}
            esign_submission_id INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deals_stage ON deals(stage)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deals_client ON deals(client_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deals_last_activity ON deals(last_activity)")

    # Invoices (AR + AP)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS invoices (
            id TEXT PRIMARY KEY,
            direction TEXT NOT NULL,    -- 'sent' (AR) or 'received' (AP)
            counterparty TEXT NOT NULL,
            deal_id TEXT,
            amount REAL NOT NULL,
            currency TEXT DEFAULT 'SGD',
            issue_date TEXT NOT NULL,
            due_date TEXT NOT NULL,
            status TEXT NOT NULL,       -- draft | sent | paid | overdue | cancelled
            paid_date TEXT,
            document_path TEXT,
            notes TEXT,
            FOREIGN KEY (deal_id) REFERENCES deals(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_due ON invoices(due_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_deal ON invoices(deal_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_direction ON invoices(direction)")

    # Expenses
    conn.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            vendor TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT DEFAULT 'SGD',
            date TEXT NOT NULL,
            status TEXT NOT NULL,       -- paid | pending
            document_path TEXT,
            recurring TEXT,             -- one-time | monthly | quarterly | yearly | NULL
            notes TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_expenses_category ON expenses(category)")

    # To-Dos (optional migration, but included for completeness)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS todos (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            priority TEXT DEFAULT 'medium',
            due TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            source TEXT,
            tags_json TEXT,             -- JSON array of strings
            created TEXT NOT NULL,
            completed TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_todos_due ON todos(due)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_todos_priority ON todos(priority)")

    # Migration tracking
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _migration_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            source_file TEXT NOT NULL,
            record_count INTEGER NOT NULL,
            migrated_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()
    print(f"Database initialized at {db_path}")
```

### Step 2: Migrate existing YAML data

```python
import yaml
from datetime import datetime

def migrate_yaml_to_sqlite(db_path, project_root):
    """One-time migration. Reads YAML, inserts into SQLite, logs migration."""
    conn = sqlite3.connect(str(db_path))

    # --- Pipeline ---
    pipeline_path = Path(project_root) / "pipeline.yaml"
    if pipeline_path.exists():
        with open(pipeline_path) as f:
            data = yaml.safe_load(f) or {}
        deals = data.get("deals", [])
        for deal in deals:
            conn.execute("""
                INSERT OR REPLACE INTO deals
                (id, client_name, contact_name, contact_email, stage, value,
                 currency, created, last_activity, notes, documents_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                deal["id"], deal["client_name"],
                deal.get("contact_name"), deal.get("contact_email"),
                deal["stage"], deal.get("value"),
                deal.get("currency", "SGD"),
                deal["created"], deal.get("last_activity"),
                deal.get("notes"),
                json.dumps(deal.get("documents", []))
            ))
        conn.execute("""
            INSERT INTO _migration_log (table_name, source_file, record_count, migrated_at)
            VALUES ('deals', ?, ?, ?)
        """, (str(pipeline_path), len(deals), datetime.now().isoformat()))
        print(f"Migrated {len(deals)} deals from pipeline.yaml")

    # --- Invoices ---
    invoices_path = Path(project_root) / "invoices.yaml"
    if invoices_path.exists():
        with open(invoices_path) as f:
            data = yaml.safe_load(f) or {}
        invoices = data.get("invoices", [])
        for inv in invoices:
            conn.execute("""
                INSERT OR REPLACE INTO invoices
                (id, direction, counterparty, deal_id, amount, currency,
                 issue_date, due_date, status, paid_date, document_path, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                inv["id"], inv["direction"], inv["counterparty"],
                inv.get("deal_id"), inv["amount"],
                inv.get("currency", "SGD"),
                inv["issue_date"], inv["due_date"], inv["status"],
                inv.get("paid_date"), inv.get("document_path"),
                inv.get("notes")
            ))
        conn.execute("""
            INSERT INTO _migration_log (table_name, source_file, record_count, migrated_at)
            VALUES ('invoices', ?, ?, ?)
        """, (str(invoices_path), len(invoices), datetime.now().isoformat()))
        print(f"Migrated {len(invoices)} invoices from invoices.yaml")

    # --- Expenses ---
    expenses_path = Path(project_root) / "expenses.yaml"
    if expenses_path.exists():
        with open(expenses_path) as f:
            data = yaml.safe_load(f) or {}
        expenses = data.get("expenses", [])
        for exp in expenses:
            conn.execute("""
                INSERT OR REPLACE INTO expenses
                (id, category, vendor, amount, currency, date, status,
                 document_path, recurring, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                exp["id"], exp["category"], exp["vendor"],
                exp["amount"], exp.get("currency", "SGD"),
                exp["date"], exp["status"],
                exp.get("document_path"), exp.get("recurring"),
                exp.get("notes")
            ))
        conn.execute("""
            INSERT INTO _migration_log (table_name, source_file, record_count, migrated_at)
            VALUES ('expenses', ?, ?, ?)
        """, (str(expenses_path), len(expenses), datetime.now().isoformat()))
        print(f"Migrated {len(expenses)} expenses from expenses.yaml")

    # --- To-Dos ---
    todos_path = Path(project_root) / "todos.yaml"
    if todos_path.exists():
        with open(todos_path) as f:
            data = yaml.safe_load(f) or {}
        todos = data.get("todos", [])
        for todo in todos:
            conn.execute("""
                INSERT OR REPLACE INTO todos
                (id, title, priority, due, status, source, tags_json, created, completed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                todo["id"], todo["title"],
                todo.get("priority", "medium"),
                todo.get("due"), todo["status"],
                todo.get("source"),
                json.dumps(todo.get("tags", [])),
                todo["created"], todo.get("completed")
            ))
        conn.execute("""
            INSERT INTO _migration_log (table_name, source_file, record_count, migrated_at)
            VALUES ('todos', ?, ?, ?)
        """, (str(todos_path), len(todos), datetime.now().isoformat()))
        print(f"Migrated {len(todos)} to-dos from todos.yaml")

    conn.commit()
    conn.close()
    print("Migration complete.")
```

### Step 3: Create a data access layer

After migration, skills should NOT read the SQLite database directly with raw SQL scattered everywhere. Instead, use a thin data access module:

```python
# shared/scripts/data_access.py
"""
Thin data access layer. Skills call these functions instead of
reading YAML or SQLite directly.

Automatically detects whether SQLite DB exists (migrated) or
falls back to YAML files. This means skills work BEFORE and AFTER
migration without code changes.
"""

import sqlite3
import json
import yaml
from pathlib import Path
from typing import Optional, List, Dict, Any


class DataAccess:
    def __init__(self, project_root: str, db_path: Optional[str] = None):
        self.project_root = Path(project_root)
        self.db_path = Path(db_path) if db_path else self.project_root / "chief_of_staff.db"
        self._use_sqlite = self.db_path.exists()

    # --- Pipeline ---

    def list_deals(self, stage: Optional[str] = None) -> List[Dict]:
        if self._use_sqlite:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            if stage:
                rows = conn.execute("SELECT * FROM deals WHERE stage = ? ORDER BY last_activity DESC", (stage,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM deals ORDER BY last_activity DESC").fetchall()
            conn.close()
            return [self._row_to_deal(r) for r in rows]
        else:
            data = self._load_yaml("pipeline.yaml")
            deals = data.get("deals", [])
            if stage:
                deals = [d for d in deals if d.get("stage") == stage]
            return deals

    def get_deal(self, deal_id: str) -> Optional[Dict]:
        if self._use_sqlite:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
            conn.close()
            return self._row_to_deal(row) if row else None
        else:
            data = self._load_yaml("pipeline.yaml")
            for d in data.get("deals", []):
                if d.get("id") == deal_id:
                    return d
            return None

    def add_deal(self, deal: Dict) -> None:
        if self._use_sqlite:
            conn = sqlite3.connect(str(self.db_path))
            conn.execute("""
                INSERT OR REPLACE INTO deals
                (id, client_name, contact_name, contact_email, stage, value,
                 currency, created, last_activity, notes, documents_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                deal["id"], deal["client_name"],
                deal.get("contact_name"), deal.get("contact_email"),
                deal["stage"], deal.get("value"),
                deal.get("currency", "SGD"),
                deal["created"], deal.get("last_activity"),
                deal.get("notes"),
                json.dumps(deal.get("documents", []))
            ))
            conn.commit()
            conn.close()
        else:
            data = self._load_yaml("pipeline.yaml")
            data.setdefault("deals", []).append(deal)
            self._save_yaml("pipeline.yaml", data)

    def move_deal_stage(self, deal_id: str, new_stage: str) -> None:
        if self._use_sqlite:
            conn = sqlite3.connect(str(self.db_path))
            conn.execute("UPDATE deals SET stage = ?, last_activity = ? WHERE id = ?",
                        (new_stage, datetime.now().strftime("%Y-%m-%d"), deal_id))
            conn.commit()
            conn.close()
        else:
            data = self._load_yaml("pipeline.yaml")
            for d in data.get("deals", []):
                if d.get("id") == deal_id:
                    d["stage"] = new_stage
                    d["last_activity"] = datetime.now().strftime("%Y-%m-%d")
                    break
            self._save_yaml("pipeline.yaml", data)

    def stale_deals(self, threshold_days: int) -> List[Dict]:
        """Deals that haven't moved in threshold_days."""
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=threshold_days)).strftime("%Y-%m-%d")
        if self._use_sqlite:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM deals WHERE last_activity < ? ORDER BY last_activity", (cutoff,)).fetchall()
            conn.close()
            return [self._row_to_deal(r) for r in rows]
        else:
            data = self._load_yaml("pipeline.yaml")
            return [d for d in data.get("deals", []) if (d.get("last_activity") or "9999") < cutoff]

    # --- Invoices ---

    def list_invoices(self, direction: Optional[str] = None, status: Optional[str] = None) -> List[Dict]:
        if self._use_sqlite:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM invoices WHERE 1=1"
            params = []
            if direction:
                query += " AND direction = ?"
                params.append(direction)
            if status:
                query += " AND status = ?"
                params.append(status)
            query += " ORDER BY due_date"
            rows = conn.execute(query, params).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        else:
            data = self._load_yaml("invoices.yaml")
            invs = data.get("invoices", [])
            if direction:
                invs = [i for i in invs if i.get("direction") == direction]
            if status:
                invs = [i for i in invs if i.get("status") == status]
            return invs

    def overdue_invoices(self) -> List[Dict]:
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        if self._use_sqlite:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM invoices WHERE due_date < ? AND status NOT IN ('paid', 'cancelled') ORDER BY due_date",
                (today,)
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        else:
            data = self._load_yaml("invoices.yaml")
            return [i for i in data.get("invoices", [])
                    if i.get("due_date", "9999") < today
                    and i.get("status") not in ("paid", "cancelled")]

    def outstanding_ar_total(self) -> float:
        """Sum of sent invoices not yet paid."""
        if self._use_sqlite:
            conn = sqlite3.connect(str(self.db_path))
            row = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) as total FROM invoices WHERE direction = 'sent' AND status NOT IN ('paid', 'cancelled')"
            ).fetchone()
            conn.close()
            return row[0]
        else:
            data = self._load_yaml("invoices.yaml")
            return sum(i["amount"] for i in data.get("invoices", [])
                       if i.get("direction") == "sent"
                       and i.get("status") not in ("paid", "cancelled"))

    # --- Expenses ---

    def list_expenses(self, category: Optional[str] = None, month: Optional[str] = None) -> List[Dict]:
        """List expenses, optionally filtered by category and/or month (YYYY-MM)."""
        if self._use_sqlite:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM expenses WHERE 1=1"
            params = []
            if category:
                query += " AND category = ?"
                params.append(category)
            if month:
                query += " AND date LIKE ?"
                params.append(f"{month}%")
            query += " ORDER BY date"
            rows = conn.execute(query, params).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        else:
            data = self._load_yaml("expenses.yaml")
            exps = data.get("expenses", [])
            if category:
                exps = [e for e in exps if e.get("category") == category]
            if month:
                exps = [e for e in exps if (e.get("date") or "").startswith(month)]
            return exps

    def expense_total_by_category(self, month: Optional[str] = None) -> Dict[str, float]:
        """Returns {category: total_amount} for a given month or all time."""
        expenses = self.list_expenses(month=month)
        totals = {}
        for exp in expenses:
            cat = exp.get("category", "other")
            totals[cat] = totals.get(cat, 0) + exp["amount"]
        return totals

    # --- To-Dos ---

    def list_todos(self, status: Optional[str] = None) -> List[Dict]:
        if self._use_sqlite:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            if status:
                rows = conn.execute("SELECT * FROM todos WHERE status = ? ORDER BY priority DESC, due", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM todos ORDER BY priority DESC, due").fetchall()
            conn.close()
            return [self._row_to_todo(r) for r in rows]
        else:
            data = self._load_yaml("todos.yaml")
            todos = data.get("todos", [])
            if status:
                todos = [t for t in todos if t.get("status") == status]
            return todos

    def add_todo(self, todo: Dict) -> None:
        if self._use_sqlite:
            conn = sqlite3.connect(str(self.db_path))
            conn.execute("""
                INSERT OR REPLACE INTO todos
                (id, title, priority, due, status, source, tags_json, created, completed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                todo["id"], todo["title"],
                todo.get("priority", "medium"),
                todo.get("due"), todo.get("status", "open"),
                todo.get("source"),
                json.dumps(todo.get("tags", [])),
                todo["created"], todo.get("completed")
            ))
            conn.commit()
            conn.close()
        else:
            data = self._load_yaml("todos.yaml")
            data.setdefault("todos", []).append(todo)
            self._save_yaml("todos.yaml", data)

    def complete_todo(self, todo_id: str) -> None:
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        if self._use_sqlite:
            conn = sqlite3.connect(str(self.db_path))
            conn.execute("UPDATE todos SET status = 'done', completed = ? WHERE id = ?", (today, todo_id))
            conn.commit()
            conn.close()
        else:
            data = self._load_yaml("todos.yaml")
            for t in data.get("todos", []):
                if t.get("id") == todo_id:
                    t["status"] = "done"
                    t["completed"] = today
                    break
            self._save_yaml("todos.yaml", data)

    # --- Helpers ---

    def _load_yaml(self, filename: str) -> Dict:
        path = self.project_root / filename
        if path.exists():
            with open(path) as f:
                return yaml.safe_load(f) or {}
        return {}

    def _save_yaml(self, filename: str, data: Dict) -> None:
        path = self.project_root / filename
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def _row_to_deal(self, row) -> Dict:
        d = dict(row)
        d["documents"] = json.loads(d.pop("documents_json", "[]"))
        return d

    def _row_to_todo(self, row) -> Dict:
        t = dict(row)
        t["tags"] = json.loads(t.pop("tags_json", "[]"))
        return t
```

### Step 4: Update company.yaml

Add a `data_store` section so skills know which backend to use:

```yaml
data_store:
  backend: sqlite           # yaml | sqlite
  db_path: "~/.hermes/projects/acme/chief_of_staff.db"
  # When backend: sqlite, the data_access.py module auto-detects the DB.
  # When backend: yaml, it reads YAML files as before.
  # During migration, set to sqlite after running migrate_yaml_to_sqlite().
```

### Step 5: Archive old YAML files

After verifying the SQLite migration is correct:

```bash
# Don't delete — archive in case rollback is needed
mv ~/.hermes/projects/{company}/pipeline.yaml ~/.hermes/projects/{company}/archive/
mv ~/.hermes/projects/{company}/invoices.yaml ~/.hermes/projects/{company}/archive/
mv ~/.hermes/projects/{company}/expenses.yaml ~/.hermes/projects/{company}/archive/
# Keep todos.yaml if not migrated, or archive it too
```

### Step 6: Update Backup skill

The Backup skill must include the SQLite database in backups:

```yaml
# In backup config, add:
backup:
  include:
    - "chief_of_staff.db"      # SQLite database (if migrated)
    - "chief_of_staff.db-wal"  # Write-ahead log (if WAL mode)
```

## Migration Checklist for the Agent

When the agent decides to migrate (based on the thresholds above):

1. [ ] Check current record counts in all YAML files
2. [ ] Run `init_db()` to create the SQLite database
3. [ ] Run `migrate_yaml_to_sqlite()` to transfer data
4. [ ] Verify: compare record counts (YAML vs SQLite) — must match
5. [ ] Update `company.yaml` → `data_store.backend: sqlite`
6. [ ] Test: run Daily Briefing, Weekly Review, P&L report — verify outputs are identical
7. [ ] Archive old YAML files (don't delete)
8. [ ] Update Backup skill config to include the .db file
9. [ ] Inform the user: "Migrated N records from YAML to SQLite. Old files archived. Everything verified working."

## Rollback

If something breaks after migration:

1. Set `company.yaml` → `data_store.backend: yaml`
2. Restore YAML files from archive
3. Delete the SQLite database
4. Investigate the issue, fix, re-migrate

The `DataAccess` class in `data_access.py` handles both backends transparently, so rollback is just a config change — no skill code changes needed.

## Design Principles

1. **Dual-backend by design** — `data_access.py` works with both YAML and SQLite. Skills call `DataAccess`, not raw SQL or raw YAML. This means migration is a config flip, not a code rewrite.
2. **WAL mode** — Write-Ahead Logging allows concurrent reads during writes (important when Daily Briefing reads while Bookkeeper writes).
3. **JSON columns for flexible fields** — `documents_json` and `tags_json` store arrays as JSON strings. This avoids needing junction tables for simple arrays while keeping the schema clean.
4. **Indexes on query patterns** — Every field that skills filter/sort by has an index (stage, client_name, due_date, status, category, date).
5. **Foreign keys** — `invoices.deal_id` references `deals.id`. SQLite enforces this when `PRAGMA foreign_keys=ON`.
6. **Config stays YAML** — `company.yaml`, `drive-map.yaml`, `queries.yaml` are NEVER migrated. They're small, human-edited, and rarely change.
7. **Wiki stays markdown** — The knowledge base is markdown by design. SQLite is for structured data, not prose.