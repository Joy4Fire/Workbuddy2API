"""部署形态与管理鉴权回归：使用隔离库，不读取真实凭据或启动上游任务。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from workbuddy_one.app import create_app
from workbuddy_one.config import config
from workbuddy_one.db import Database


class TestDeploymentSecurity(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory(prefix="wb_security_")
        self.addCleanup(temp.cleanup)
        self.enterContext(patch.object(config, "db_path", str(Path(temp.name) / "test.db")))
        self.enterContext(patch("workbuddy_one.app.find_auth_files", return_value=[]))
        self.db = Database(config.db_path)
        self.addCleanup(self.db._conn.close)
        self.enterContext(patch("workbuddy_one.app.Database", return_value=self.db))
        self.enterContext(patch.object(config, "_overrides", {}))
        self.enterContext(patch.object(config, "admin_token", ""))
        dist = Path(temp.name) / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<html>WebUI</html>", encoding="utf-8")
        (dist / "assets" / "app.js").write_text("/* WebUI */", encoding="utf-8")
        self.enterContext(patch("workbuddy_one.routes.webui._DIST_DIR", dist))
        # ASGITransport 不启动 lifespan，避免测试触发签到、额度查询等真实网络操作。
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(), client=("172.18.0.1", 43210)),
            base_url="http://localhost", trust_env=False)
        self.addAsyncCleanup(self.client.aclose)

    async def test_token_allows_lan_webui_and_assets(self):
        config.admin_token = "test-admin-token"
        for host in ("192.168.1.20:8787", "gateway.lan:8787", "[fd00::20]:8787"):
            for path in ("/", "/assets/app.js"):
                with self.subTest(host=host, path=path):
                    response = await self.client.get(path, headers={"Host": host})
                    self.assertEqual(response.status_code, 200)

    async def test_lan_admin_still_requires_correct_token(self):
        config.admin_token = "test-admin-token"
        for extra in ({}, {"X-Admin-Token": "wrong"}, {"Authorization": "Bearer wrong"}):
            with self.subTest(headers=extra):
                response = await self.client.get("/admin/accounts", headers={"Host": "192.168.1.20:8787", **extra})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["error"]["message"], "需要有效的 Admin Token")
        for extra in ({"X-Admin-Token": config.admin_token},
                      {"Authorization": f"Bearer {config.admin_token}"}):
            with self.subTest(headers=extra):
                response = await self.client.get("/admin/accounts", headers={"Host": "192.168.1.20:8787", **extra})
                self.assertEqual(response.status_code, 200)

    async def test_without_token_remote_hosts_are_blocked(self):
        for host in ("192.168.1.20:8787", "gateway.lan:8787", "evil.example",
                     "localhost.evil.example", "localhost:invalid", "[::1].evil.example",
                     "[fd00::20]:8787"):
            for path in ("/", "/admin/accounts", "/health", "/v1/models"):
                with self.subTest(host=host, path=path):
                    response = await self.client.get(path, headers={"Host": host})
                    self.assertEqual(response.status_code, 403)

    async def test_without_token_docker_loopback_hosts_work(self):
        for host in ("localhost:8787", "127.0.0.1:8787", "[::1]:8787"):
            for path in ("/", "/admin/accounts", "/health"):
                with self.subTest(host=host, path=path):
                    response = await self.client.get(path, headers={"Host": host})
                    self.assertEqual(response.status_code, 200)

    async def test_token_mode_keeps_inference_key_auth(self):
        config.admin_token = "test-admin-token"
        response = await self.client.get("/v1/models", headers={"Host": "192.168.1.20:8787"})
        self.assertEqual(response.status_code, 401)

    async def test_token_mode_does_not_allow_arbitrary_cors_origins(self):
        config.admin_token = "test-admin-token"
        headers = {"Host": "192.168.1.20:8787", "Origin": "https://evil.example"}
        for method in ("get", "options"):
            with self.subTest(method=method):
                response = await getattr(self.client, method)("/", headers=headers)
                self.assertNotIn("Access-Control-Allow-Origin", response.headers)


if __name__ == "__main__":
    unittest.main()
