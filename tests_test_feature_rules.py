import sqlite3
import unittest

from feature_rules import MAX_ACCOUNTS_PER_IP, is_personal_subpage_title, reserve_registration_ip_slot


class FeatureRuleTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, registered_ip TEXT)"
        )
        self.db.execute(
            "CREATE TABLE registration_ip_limits (ip_address TEXT PRIMARY KEY, account_count INTEGER NOT NULL DEFAULT 0)"
        )

    def tearDown(self):
        self.db.close()

    def test_personal_subpage_detection(self):
        self.assertFalse(is_personal_subpage_title("사용자:alice"))
        self.assertTrue(is_personal_subpage_title("사용자:alice/메모"))
        self.assertTrue(is_personal_subpage_title("사용자:alice/프로젝트/위키"))
        self.assertFalse(is_personal_subpage_title("일반문서/하위문서"))

    def test_ip_quota_allows_five_and_blocks_sixth(self):
        ip = "203.0.113.10"
        for _ in range(MAX_ACCOUNTS_PER_IP):
            self.assertTrue(reserve_registration_ip_slot(self.db, ip))
        self.assertFalse(reserve_registration_ip_slot(self.db, ip))
        count = self.db.execute(
            "SELECT account_count FROM registration_ip_limits WHERE ip_address = ?", (ip,)
        ).fetchone()[0]
        self.assertEqual(count, MAX_ACCOUNTS_PER_IP)

    def test_first_counter_seed_includes_existing_accounts(self):
        ip = "203.0.113.12"
        self.db.executemany(
            "INSERT INTO users(id, registered_ip) VALUES (?, ?)",
            [(1, ip), (2, ip), (3, ip), (4, ip)],
        )
        self.assertTrue(reserve_registration_ip_slot(self.db, ip))
        self.assertFalse(reserve_registration_ip_slot(self.db, ip))
        count = self.db.execute(
            "SELECT account_count FROM registration_ip_limits WHERE ip_address = ?", (ip,)
        ).fetchone()[0]
        self.assertEqual(count, MAX_ACCOUNTS_PER_IP)

    def test_ip_quota_is_isolated_per_ip(self):
        self.assertTrue(reserve_registration_ip_slot(self.db, "203.0.113.10"))
        self.assertTrue(reserve_registration_ip_slot(self.db, "203.0.113.11"))

    def test_failed_registration_can_rollback_slot(self):
        ip = "203.0.113.10"
        self.assertTrue(reserve_registration_ip_slot(self.db, ip))
        self.db.rollback()
        row = self.db.execute(
            "SELECT account_count FROM registration_ip_limits WHERE ip_address = ?", (ip,)
        ).fetchone()
        self.assertIsNone(row)


if __name__ == "__main__":
    unittest.main()
