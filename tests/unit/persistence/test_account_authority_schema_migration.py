from __future__ import annotations

from pathlib import Path

import pytest

from firmquant.persistence.database import Database
from firmquant.persistence.schema import CURRENT_SCHEMA_VERSION


def test_account_authority_is_owned_by_central_schema_migration(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        assert CURRENT_SCHEMA_VERSION == 3
        assert database.scalar("SELECT max(version) FROM schema_migrations") == 3
        tables = {
            str(row["name"])
            for row in database.query_all("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        }
        assert {
            "account_bindings",
            "reviewed_account_adjustments",
            "account_bootstrap_operations",
        } <= tables

        triggers = {
            str(row["name"])
            for row in database.query_all(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
            )
        }
        assert {
            "account_bindings_reject_update",
            "account_bindings_reject_delete",
            "reviewed_account_adjustments_reject_update",
            "reviewed_account_adjustments_reject_delete",
        } <= triggers
    finally:
        database.close()


def test_account_authority_tables_are_immutable_at_schema_boundary(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        with database.transaction():
            database.write(
                """
                INSERT INTO account_bindings(
                    binding_id, account_id_hash, account_type, broker_snapshot_sha256,
                    account_state_sha256, uquant_commit, uquant_code_fingerprint,
                    data_hash, data_as_of, data_symbols_json, created_at,
                    payload_json, payload_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "acctbind_" + "1" * 64,
                    "a" * 64,
                    "CASH",
                    "b" * 64,
                    "c" * 64,
                    "1" * 40,
                    "d" * 64,
                    "e" * 64,
                    "2026-01-05",
                    '["sz300308"]',
                    "2026-01-06T08:05:00+00:00",
                    "{}",
                    "f" * 64,
                ),
            )
        with pytest.raises(Exception, match="append-only"), database.transaction():
            database.write("DELETE FROM account_bindings")
    finally:
        database.close()
