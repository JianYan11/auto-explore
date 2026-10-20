"""Provider and configuration regressions; no network or Android required."""

import json
import io
import os
from contextlib import redirect_stderr
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from harness.config import ExplorerConfig
from harness.model_api import chat_request_options, completion_token_budget
from harness.summary import ExperienceSummaryClient
from harness.tools import VLMAnalyzer
from runtime_config import default_adb, load_environment


class ModelApiTests(unittest.TestCase):
    def test_glm_53_always_thinks_and_never_sends_qwen_fields(self):
        for model in ("glm-5.3-flash", "vendor/GLM-5.3-FlashX"):
            options = chat_request_options(model)
            self.assertEqual(options["extra_body"], {"thinking": {"type": "enabled"}})
            self.assertEqual(options["reasoning_effort"], "low")
            self.assertEqual(options["temperature"], 1)
            self.assertNotIn("enable_thinking", json.dumps(options))

    def test_qwen_retains_non_thinking_and_other_models_get_no_vendor_fields(self):
        self.assertEqual(chat_request_options("Qwen/Qwen3.8-27B"),
                         {"temperature": 0, "extra_body": {"enable_thinking": False}})
        self.assertEqual(chat_request_options("custom-model"), {"temperature": 0})
        self.assertEqual(chat_request_options("Qwen/custom", profile="openai"), {"temperature": 0})
        self.assertNotIn("reasoning_effort", chat_request_options("glm-4.6v"))

    def test_invalid_configuration_is_rejected_before_a_request(self):
        for kwargs in ({"profile": "typo"}, {"reasoning_effort": "disabled"}):
            with self.assertRaises(ValueError):
                chat_request_options("glm-5.3-flash", **kwargs)

    def test_glm_budget_accommodates_complex_screen_json(self):
        self.assertEqual(completion_token_budget("glm-5.3-flash", 4096), 8192)
        self.assertEqual(completion_token_budget("glm-5.3-flash", 12000), 12000)
        self.assertEqual(completion_token_budget("Qwen/Qwen3.8", 768), 768)
        self.assertEqual(completion_token_budget("glm-5.3-flash", 768, profile="openai"), 768)

    def analyzer(self, *, content='{"elements": []}', finish_reason="stop", reasoning=None):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason=finish_reason,
            message=SimpleNamespace(content=content, reasoning_content=reasoning),
        )])
        with patch("openai.OpenAI", return_value=client):
            analyzer = VLMAnalyzer(ExplorerConfig(model="glm-5.3-flash", vlm_api_key="test-only",
                                                 model_api_profile="auto", model_reasoning_effort="low"))
        return analyzer, client

    def test_visual_request_uses_glm_parameters_and_image_content(self):
        analyzer, client = self.analyzer()
        self.assertEqual(analyzer._request_json("JSON only", [("current", "encoded")]), {"elements": []})
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["reasoning_effort"], "low")
        self.assertEqual(request["max_tokens"], 8192)
        self.assertEqual(request["extra_body"], {"thinking": {"type": "enabled"}})
        self.assertEqual(request["messages"][0]["content"][0]["type"], "image_url")

    def test_truncated_json_and_reasoning_are_not_accepted_as_an_answer(self):
        analyzer, _ = self.analyzer(content='{"elements": []}', finish_reason="length")
        with self.assertRaisesRegex(ValueError, "token budget"):
            analyzer._request_json("test", [])
        analyzer, _ = self.analyzer(content=None, reasoning='{"elements": []}')
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            analyzer._request_json("test", [])

    def test_summary_client_uses_the_same_glm_adapter(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content='{"summary": "ok"}'))])
        with patch("openai.OpenAI", return_value=client):
            summary = ExperienceSummaryClient(ExplorerConfig(
                model="other-execution-model", summary_model="glm-5.3-flash", vlm_api_key="test-only",
                model_api_profile="auto", model_reasoning_effort="low"))
        self.assertEqual(summary.summarize("test", {}), {"summary": "ok"})
        self.assertEqual(client.chat.completions.create.call_args.kwargs["extra_body"],
                         {"thinking": {"type": "enabled"}})


class EnvironmentTests(unittest.TestCase):
    def test_adb_override_local_tool_and_path_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"ADB_PATH": "explicit-adb"}):
                self.assertEqual(default_adb(), "explicit-adb")
            with patch.dict(os.environ), patch("runtime_config.REPOSITORY_ROOT", root):
                os.environ.pop("ADB_PATH", None)
                self.assertEqual(default_adb(), "adb")
                local = root / ".tools/platform-tools/adb"
                local.parent.mkdir(parents=True)
                local.touch()
                self.assertEqual(default_adb(), str(local))

    def test_exploration_requires_explicit_connected_device(self):
        from harness.cli import build_parser, config_from_args
        with patch.dict(os.environ):
            os.environ.pop("ANDROID_SERIAL", None)
            parser = build_parser()
            args = parser.parse_args(["--package", "test.app"])
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                config_from_args(args, parser)
            self.assertEqual(error.exception.code, 2)
            args = parser.parse_args(["--package", "test.app", "--device", "127.0.0.1:15001",
                                      "--summary-model", ""])
            self.assertEqual(config_from_args(args, parser).device_name, "127.0.0.1:15001")

    def test_dotenv_respects_environment_and_preserves_explicit_empty_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("EXPLORER_TEST_ENV=file\nEXPLORER_TEST_EMPTY=\n", encoding="utf-8")
            with patch.dict(os.environ, {"EXPLORER_TEST_ENV": "process"}):
                os.environ.pop("EXPLORER_TEST_EMPTY", None)
                load_environment(path)
                self.assertEqual(os.environ["EXPLORER_TEST_ENV"], "process")
                self.assertEqual(os.environ["EXPLORER_TEST_EMPTY"], "")

    def test_settings_load_before_config_import_and_blank_summary_disables_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("QWEN_MODEL=glm-5.3-flash\nSUMMARY_MODEL=\n", encoding="utf-8")
            env = dict(os.environ)
            for key in ("QWEN_MODEL", "SUMMARY_MODEL"):
                env.pop(key, None)
            code = ("from pathlib import Path; import sys; "
                    "from runtime_config import load_environment; load_environment(Path(sys.argv[1])); "
                    "from harness.config import ExplorerConfig; "
                    "print(repr((ExplorerConfig.model, ExplorerConfig.summary_model)))")
            result = subprocess.run([sys.executable, "-c", code, str(path)], env=env,
                                    cwd=Path(__file__).resolve().parents[2],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), "('glm-5.3-flash', '')")


if __name__ == "__main__":
    unittest.main()
