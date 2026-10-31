"""Small SQLite-compatible DB-API layer backed by PostgreSQL/Neon.

The wiki's local mode continues to use sqlite3. In Vercel mode, DATABASE_URL
activates this adapter so the existing application's mostly portable SQL can
run on PostgreSQL without silently losing edits to an ephemeral filesystem.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Iterable


class HybridRow(dict):
    """Mapping row that also supports sqlite3.Row-style integer indexes."""
    def __init__(self, mapping: dict[str, Any]):
        super().__init__(mapping)
        self._values = list(mapping.values())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)

    def keys(self):
        return super().keys()


class CursorCompat:
    def __init__(self, cursor, lastrowid=None):
        self._cursor = cursor
        self.lastrowid = lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def _row(self, row):
        return HybridRow(row) if row is not None and isinstance(row, dict) else row

    def fetchone(self):
        return self._row(self._cursor.fetchone())

    def fetchall(self):
        return [self._row(row) for row in self._cursor.fetchall()]

    def fetchmany(self, size=None):
        rows = self._cursor.fetchmany(size) if size is not None else self._cursor.fetchmany()
        return [self._row(row) for row in rows]

    def __iter__(self):
        for row in self._cursor:
            yield self._row(row)


class PostgresCompatConnection:
    """Adapter exposing the small subset of sqlite3.Connection used by app.py."""
    def __init__(self, url: str):
        import psycopg
        from psycopg.rows import dict_row
        self._psycopg = psycopg
        self._conn = psycopg.connect(url, connect_timeout=10, row_factory=dict_row, prepare_threshold=None)

    @staticmethod
    def _replace_qmarks(sql: str) -> str:
        """Replace ? placeholders outside SQL string literals and identifiers."""
        out = []
        i = 0
        quote_char = None
        while i < len(sql):
            ch = sql[i]
            if quote_char:
                out.append(ch)
                if ch == quote_char:
                    if i + 1 < len(sql) and sql[i + 1] == quote_char:
                        out.append(sql[i + 1])
                        i += 1
                    else:
                        quote_char = None
            elif ch in ("'", '"'):
                quote_char = ch
                out.append(ch)
            elif ch == "?":
                out.append("%s")
            else:
                out.append(ch)
            i += 1
        return "".join(out)

    def _prepare(self, sql: str):
        clean = sql.strip()
        table_info = re.match(r"PRAGMA\s+table_info\((['\"]?)([A-Za-z_][\w]*)\1\)", clean, re.I)
        if table_info:
            table = table_info.group(2).lower()
            if not re.fullmatch(r"[a-z_][a-z0-9_]*", table):
                raise sqlite3.OperationalError("invalid table name")
            return (
                "SELECT column_name AS name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s ORDER BY ordinal_position",
                False,
                table,
            )
        if re.match(r"^PRAGMA\s+.+$", clean, re.I):
            return "", False, None
        # SQLite's INSERT OR IGNORE maps to PostgreSQL's ON CONFLICT DO NOTHING.
        ignore_insert = bool(re.search(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", clean, re.I))
        clean = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", clean, flags=re.I)
        if ignore_insert and not re.search(r"\bON\s+CONFLICT\b", clean, re.I):
            clean = clean.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        clean = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "BIGSERIAL PRIMARY KEY", clean, flags=re.I)
        clean = re.sub(r"\bINTEGER\b", "BIGINT", clean, flags=re.I)
        clean = re.sub(r"\bBLOB\b", "BYTEA", clean, flags=re.I)
        # SQLite's NOCASE collation is not a PostgreSQL built-in. LOWER()
        # preserves the intended case-insensitive sort without requiring an
        # optional database collation to be installed.
        clean = re.sub(
            r"\b([A-Za-z_][A-Za-z0-9_.]*)\s+COLLATE\s+NOCASE\b",
            r"LOWER(\1)", clean, flags=re.I,
        )
        # SQLite's default LIKE is ASCII case-insensitive; use ILIKE for the
        # equivalent search behavior on PostgreSQL. The old child-page query
        # uses SQLite's instr(), whose PostgreSQL equivalent is strpos().
        clean = re.sub(r"\bLIKE\b", "ILIKE", clean, flags=re.I)
        clean = re.sub(r"\bINSTR\s*\(", "STRPOS(", clean, flags=re.I)
        clean = self._replace_qmarks(clean)
        # app.py uses cursor.lastrowid for account and page insertions. PostgreSQL
        # needs an explicit RETURNING clause to obtain that value.
        lastrowid_tables = r"users|pages|revisions|discussions|page_acls|user_bans|user_warnings|ip_bans|login_logs|registration_logs"
        insert_match = re.match(rf"INSERT\s+INTO\s+({lastrowid_tables})\b", clean, re.I)
        wants_id = bool(insert_match and not re.search(r"\bRETURNING\b", clean, re.I))
        if wants_id:
            clean = clean.rstrip().rstrip(";") + " RETURNING id"
        return clean, wants_id, None

    def execute(self, sql: str, parameters: Iterable[Any] | None = None):
        query, wants_id, special = self._prepare(sql)
        if not query:
            return CursorCompat(_EmptyCursor())
        params = (special,) if special is not None else tuple(parameters or ())
        try:
            cur = self._conn.cursor()
            cur.execute(query, params)
            lastrowid = None
            if wants_id:
                rows = cur.fetchall()
                if rows:
                    lastrowid = rows[-1]["id"]
                # SQLite INSERT cursors do not expose a result set; callers use only lastrowid.
                return CursorCompat(_EmptyCursor(), lastrowid)
            return CursorCompat(cur)
        except self._psycopg.IntegrityError as exc:
            raise sqlite3.IntegrityError(str(exc)) from exc
        except self._psycopg.OperationalError as exc:
            raise sqlite3.OperationalError(str(exc)) from exc
        except self._psycopg.Error as exc:
            raise sqlite3.DatabaseError(str(exc)) from exc

    def executemany(self, sql: str, seq_of_parameters):
        query = self._replace_qmarks(sql.strip())
        query = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "BIGSERIAL PRIMARY KEY", query, flags=re.I)
        try:
            cur = self._conn.cursor()
            cur.executemany(query, seq_of_parameters)
            return CursorCompat(cur)
        except self._psycopg.IntegrityError as exc:
            raise sqlite3.IntegrityError(str(exc)) from exc
        except self._psycopg.Error as exc:
            raise sqlite3.DatabaseError(str(exc)) from exc

    def executescript(self, script: str):
        # NamuLite's schema statements contain no semicolons inside strings.
        results = []
        for statement in script.split(";"):
            if statement.strip():
                results.append(self.execute(statement))
        return results[-1] if results else CursorCompat(_EmptyCursor())

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()
        return False


class _EmptyCursor:
    rowcount = 0

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def fetchmany(self, size=None):
        return []

    def __iter__(self):
        return iter(())
