"""Offboarding revokes all and only the accounts linked to one employee ID."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi import HTTPException

from app.routers import users


class FakeDb:
    def __init__(self):
        self.accounts = [
            {"id": 1, "dingtalk_user_id": "staff-42", "active": True},
            {"id": 2, "dingtalk_user_id": "staff-42", "active": True},
            {"id": 3, "dingtalk_user_id": "other", "active": True},
        ]
        self.audit = []

    def acquire(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def fetch(self, query, employee_id):
        return [{"id": a["id"]} for a in self.accounts
                if a["dingtalk_user_id"] == employee_id and a["active"]]

    async def execute(self, query, *args):
        if query.startswith("UPDATE users"):
            for account in self.accounts:
                if account["dingtalk_user_id"] == args[0]:
                    account["active"] = False
        elif "INSERT INTO audit_log" in query:
            self.audit.append(args)


def test_offboard_disables_all_linked_accounts_only_once():
    db = FakeDb()
    with patch.object(users, "pool", lambda: db):
        assert asyncio.run(users.deactivate_employee_accounts(" staff-42 ", source="test")) == [1, 2]
        assert [a["active"] for a in db.accounts] == [False, False, True]
        assert len(db.audit) == 2
        assert asyncio.run(users.deactivate_employee_accounts("staff-42", source="test")) == []
        assert len(db.audit) == 2


def test_staff_account_requires_dingtalk_binding():
    class CreateDb(FakeDb):
        async def fetchrow(self, query, *args):
            if query.startswith("SELECT id, dingtalk_user_id"):
                return None
            if "INSERT INTO users" in query:
                return {"id": 4, "name": args[0], "role": args[1], "active": True,
                        "telegram_id": None, "dingtalk_user_id": args[4],
                        "must_change_password": True, "created_at": datetime.now(timezone.utc)}
            raise AssertionError(query)

    db = CreateDb()
    with patch.object(users, "pool", lambda: db):
        try:
            asyncio.run(users.create_user(users.UserIn(name="worker", role="returns"), actor={"id": 1}))
            assert False, "unlinked staff account should be rejected"
        except HTTPException as error:
            assert error.status_code == 400
        row = asyncio.run(users.create_user(
            users.UserIn(name="worker", role="returns", dingtalk_user_id="staff-42"),
            actor={"id": 1},
        ))
        assert row["dingtalk_user_id"] == "staff-42"
        assert row["must_change_password"] is True
