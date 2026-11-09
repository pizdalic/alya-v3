import asyncio
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from alya.app import build_app
from alya.cli import EXIT_INVALID, EXIT_OK, EXIT_PROBLEMS, main
from alya.core.config import load_settings
from alya.observability.health import HealthRegistry


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_health_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(None, env={
                "ALYA_STORAGE__DB_PATH": str(Path(tmp) / "a.sqlite3"),
                "ALYA_HEALTH__ENABLED": "false",
            })
            app = build_app(settings)
            task = asyncio.create_task(app.run())
            for _ in range(100):
                if "storage" in app.lifecycle.started:
                    break
                await asyncio.sleep(0.01)
            report = await app.container.resolve(HealthRegistry).run()
            self.assertEqual(report["status"], "ok", report)
            app.lifecycle.request_stop()
            await asyncio.wait_for(task, 2)
            self.assertEqual(app.lifecycle.started, [])


def run_cli(*argv: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    clean = {k: v for k, v in os.environ.items() if not k.startswith("ALYA_")}
    with mock.patch.dict(os.environ, {**clean, **(env or {})}, clear=True), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def test_check_config(self) -> None:
        code, out, err = run_cli("check-config", env={"ALYA_LLM__API_KEY": "sk-hidden-1234567890"})
        self.assertEqual(code, EXIT_PROBLEMS)
        self.assertIn("llm.api_key: задан", out)
        self.assertNotIn("sk-hidden", out + err)
        code, _, err = run_cli("check-config", env={"ALYA_HEALTH__PORT": "0"})
        self.assertEqual(code, EXIT_INVALID)
        self.assertIn("health.port", err)

    def test_migrate_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"ALYA_STORAGE__DB_PATH": str(Path(tmp) / "m.sqlite3")}
            code, out, _ = run_cli("migrate", env=env)
            self.assertEqual((code, out.strip()), (EXIT_OK, "применены: [1, 2, 3, 4, 5, 6]"))
            code, out, _ = run_cli("migrate", env=env)
            self.assertEqual(out.strip(), "схема уже актуальна")

    def test_healthcheck_unreachable(self) -> None:
        code, _, err = run_cli(
            "healthcheck", "--url", "http://127.0.0.1:1/health", "--timeout", "1")
        self.assertEqual(code, EXIT_PROBLEMS)
        self.assertIn("недоступен", err)

    def test_missing_config_file(self) -> None:
        code, _, _ = run_cli("--config", "/nonexistent.toml", "check-config")
        self.assertEqual(code, EXIT_INVALID)


if __name__ == "__main__":
    unittest.main()
