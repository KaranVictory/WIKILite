"""Small, portable policy helpers used by the NamuLite app."""
from __future__ import annotations

MAX_ACCOUNTS_PER_IP = 5


def is_personal_subpage_title(title: str) -> bool:
    """Whether a document title is nested below a personal user page."""
    return title.startswith("사용자:") and "/" in title[len("사용자:"):]


def reserve_registration_ip_slot(db, ip_address: str, limit: int = MAX_ACCOUNTS_PER_IP) -> bool:
    """Atomically reserve one of the allowed account slots for an IP.

    The caller must commit after successful account creation or roll back if
    account creation fails, so the counter and account row remain consistent.
    """
    if not ip_address or limit < 1:
        return False
    # Seed a missing counter from existing accounts. This also protects the
    # first signup if it races with a cold-start migration/backfill.
    db.execute(
        "INSERT INTO registration_ip_limits(ip_address, account_count) "
        "SELECT ?, COUNT(*) FROM users WHERE registered_ip = ? "
        "ON CONFLICT(ip_address) DO NOTHING",
        (ip_address, ip_address),
    )
    result = db.execute(
        "UPDATE registration_ip_limits SET account_count = account_count + 1 "
        "WHERE ip_address = ? AND account_count < ?",
        (ip_address, limit),
    )
    return result.rowcount == 1
