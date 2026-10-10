"""Returns has every operational permission admin has, but no account access."""

import asyncio
import unittest

from fastapi import HTTPException

from app.routers import defectives, exports, imports, inventory, spx, users


class ReturnsAdminParityTests(unittest.TestCase):
    def test_operational_route_gates_accept_returns(self):
        for router in (defectives.router, exports.router, imports.router, inventory.router, spx.router):
            for route in router.routes:
                for dependency in route.dependant.dependencies:
                    gate = dependency.call
                    if getattr(gate, "__name__", "") != "dep":
                        continue
                    with self.subTest(route=route.path, methods=route.methods):
                        admin = asyncio.run(gate(user={"role": "admin"}))
                        returns = asyncio.run(gate(user={"role": "returns"}))
                        self.assertEqual(admin["role"], "admin")
                        self.assertEqual(returns["role"], "returns")

    def test_account_route_gates_still_reject_returns(self):
        for route in users.router.routes:
            for dependency in route.dependant.dependencies:
                gate = dependency.call
                with self.subTest(route=route.path, methods=route.methods):
                    with self.assertRaises(HTTPException) as caught:
                        asyncio.run(gate(user={"role": "returns"}))
                    self.assertEqual(caught.exception.status_code, 403)
                    self.assertEqual(asyncio.run(gate(user={"role": "admin"}))["role"], "admin")
