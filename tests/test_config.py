import tempfile
import unittest
from pathlib import Path

from alya.core.config import load_settings
from alya.core.errors import ConfigError


def write(tmp: str, text: str) -> Path:
    path = Path(tmp) / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


class ConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        s = load_settings(None, env={})
        self.assertEqual(s.app.timezone, "Asia/Yekaterinburg")
        self.assertEqual(s.llm.base_url, "https://openrouter.ai/api/v1")
        self.assertEqual(s.telegram.mode, "userbot")

    def test_env_overrides_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write(tmp, '[health]\nport = 9000\n[llm.roles.chat]\nmodel = "a/b"\n')
            s = load_settings(path, env={"ALYA_HEALTH__PORT": "9100"})
        self.assertEqual(s.health.port, 9100)
        self.assertEqual(s.llm.roles["chat"].model, "a/b")

    def test_env_nested_roles_and_secret(self) -> None:
        env = {
            "ALYA_LLM__API_KEY": "sk-or-v1-abcdefghijklmnopqrstuvwxyz",
            "ALYA_LLM__ROLES__EMBEDDING__MODEL": "emb/model",
            "ALYA_TELEGRAM__API_ID": "12345",
            "UNRELATED": "x",
        }
        s = load_settings(None, env=env)
        self.assertEqual(s.llm.roles["embedding"].model, "emb/model")
        self.assertEqual(s.telegram.api_id, 12345)
        self.assertNotIn("abcdefghij", repr(s))
        self.assertNotIn("abcdefghij", str(s.summary()))

    def test_secret_in_file_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write(tmp, '[llm]\napi_key = "sk-secret-value-123456789"\n')
            with self.assertRaises(ConfigError) as ctx:
                load_settings(path, env={})
        self.assertIn("ALYA_LLM__API_KEY", str(ctx.exception))
        self.assertNotIn("sk-secret", str(ctx.exception))

    def test_validation_error_hides_values(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            load_settings(None, env={"ALYA_TELEGRAM__API_ID": "not-a-number-secret"})
        self.assertIn("telegram.api_id", str(ctx.exception))
        self.assertNotIn("not-a-number-secret", str(ctx.exception))

    def test_unknown_key_and_role_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            load_settings(None, env={"ALYA_APP__TYPO": "1"})
        with self.assertRaises(ConfigError):
            load_settings(None, env={"ALYA_LLM__ROLES__WHATEVER__MODEL": "x"})

    def test_bad_timezone_and_url(self) -> None:
        with self.assertRaises(ConfigError):
            load_settings(None, env={"ALYA_APP__TIMEZONE": "Mars/Base"})
        with self.assertRaises(ConfigError):
            load_settings(None, env={"ALYA_LLM__BASE_URL": "http://example.com/v1"})
        s = load_settings(None, env={"ALYA_LLM__BASE_URL": "http://localhost:8000/v1/"})
        self.assertEqual(s.llm.base_url, "http://localhost:8000/v1")

    def test_missing_file_and_bad_toml(self) -> None:
        with self.assertRaises(ConfigError):
            load_settings(Path("/nonexistent/config.toml"), env={})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError):
                load_settings(write(tmp, "[app\n"), env={})

    def test_problems_for_run(self) -> None:
        s = load_settings(None, env={})
        problems = " ".join(s.problems_for_run())
        self.assertIn("api_id", problems)
        self.assertIn("api_key", problems)
        full = load_settings(None, env={
            "ALYA_TELEGRAM__API_ID": "1",
            "ALYA_TELEGRAM__API_HASH": "hash-value",
            "ALYA_LLM__API_KEY": "key-value",
            "ALYA_LLM__ROLES__CHAT__MODEL": "m",
            "ALYA_APP__OWNER_USER_ID": "42",
        })
        self.assertEqual(full.problems_for_run(), [])
        self.assertEqual(sorted(full.secret_values()), ["hash-value", "key-value"])


if __name__ == "__main__":
    unittest.main()
