"""Fast unit tests for SQL translation used by Vercel/PostgreSQL mode."""
import unittest

from db_compat import PostgresCompatConnection


class TestPostgresSQLTranslation(unittest.TestCase):
    def setUp(self):
        self.adapter = object.__new__(PostgresCompatConnection)

    def test_pragma_settings_become_noops(self):
        self.assertEqual(self.adapter._prepare("PRAGMA foreign_keys = ON"), ("", False, None))
        self.assertEqual(self.adapter._prepare("PRAGMA busy_timeout = 15000"), ("", False, None))

    def test_table_info_maps_to_information_schema(self):
        query, wants_id, table = self.adapter._prepare("PRAGMA table_info(users)")
        self.assertIn("information_schema.columns", query)
        self.assertFalse(wants_id)
        self.assertEqual(table, "users")

    def test_sqlite_schema_types_translate(self):
        query, wants_id, special = self.adapter._prepare(
            "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "is_admin INTEGER NOT NULL DEFAULT 0, photo BLOB)"
        )
        self.assertIn("id BIGSERIAL PRIMARY KEY", query)
        self.assertIn("is_admin BIGINT NOT NULL DEFAULT 0", query)
        self.assertIn("photo BYTEA", query)
        self.assertFalse(wants_id)
        self.assertIsNone(special)

    def test_insert_or_ignore_and_qmarks_translate(self):
        query, wants_id, special = self.adapter._prepare(
            "INSERT OR IGNORE INTO pages(title, created_at) VALUES (?, ?)"
        )
        self.assertIn("VALUES (%s, %s)", query)
        self.assertIn("ON CONFLICT DO NOTHING", query)
        self.assertIn("RETURNING id", query)
        self.assertTrue(wants_id)
        self.assertIsNone(special)

    def test_sqlite_nocase_and_like_are_adapted(self):
        query, _, _ = self.adapter._prepare(
            "SELECT title FROM pages WHERE title LIKE ? ORDER BY title COLLATE NOCASE"
        )
        self.assertIn("title ILIKE %s", query)
        self.assertIn("ORDER BY LOWER(title)", query)

    def test_registration_ip_quota_sql_keeps_upsert_and_bound_parameters(self):
        query, wants_id, special = self.adapter._prepare(
            "INSERT INTO registration_ip_limits(ip_address, account_count) "
            "VALUES (?, 0) ON CONFLICT(ip_address) DO NOTHING"
        )
        self.assertIn("ON CONFLICT(ip_address) DO NOTHING", query)
        self.assertIn("VALUES (%s, 0)", query)
        self.assertFalse(wants_id)
        self.assertIsNone(special)
        update, _, _ = self.adapter._prepare(
            "UPDATE registration_ip_limits SET account_count = account_count + 1 "
            "WHERE ip_address = ? AND account_count < ?"
        )
        self.assertIn("ip_address = %s AND account_count < %s", update)

    def test_registration_counter_seed_translates_for_postgres(self):
        query, wants_id, special = self.adapter._prepare(
            "INSERT INTO registration_ip_limits(ip_address, account_count) "
            "SELECT ?, COUNT(*) FROM users WHERE registered_ip = ? "
            "ON CONFLICT(ip_address) DO NOTHING"
        )
        self.assertIn("SELECT %s, COUNT(*) FROM users WHERE registered_ip = %s", query)
        self.assertIn("ON CONFLICT(ip_address) DO NOTHING", query)
        self.assertFalse(wants_id)
        self.assertIsNone(special)

    def test_sqlite_instr_maps_to_postgres_strpos(self):
        query, _, _ = self.adapter._prepare(
            "SELECT title FROM pages WHERE instr(substr(title, 2), '/') = 0"
        )
        self.assertIn("STRPOS(substr(title, 2), '/')", query)

    def test_question_mark_in_string_is_not_a_placeholder(self):
        self.assertEqual(
            self.adapter._replace_qmarks("SELECT '?' AS literal, value FROM t WHERE id = ?"),
            "SELECT '?' AS literal, value FROM t WHERE id = %s",
        )


if __name__ == "__main__":
    unittest.main()
