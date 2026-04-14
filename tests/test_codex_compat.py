import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "plugin" / "skills" / "skill-evolver" / "scripts"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


llm = _load_module("skill_evolver_llm", SCRIPTS_DIR / "llm.py")
binary_judge = _load_module(
    "skill_evolver_binary_judge",
    SCRIPTS_DIR / "binary_judge.py",
)


class CodexCompatTests(unittest.TestCase):
    def test_call_llm_codex_uses_exec_and_reads_output_file(self):
        prompt = "Reply with exactly YES"

        def fake_run(cmd, **kwargs):
            self.assertEqual(cmd[:3], ["codex", "exec", "--skip-git-repo-check"])
            self.assertEqual(cmd[-1], "-")
            self.assertEqual(kwargs.get("input"), prompt)
            output_index = cmd.index("-o") + 1
            Path(cmd[output_index]).write_text("YES\n", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="ignored", stderr="")

        with mock.patch.object(llm.subprocess, "run", side_effect=fake_run):
            result = llm._call_llm(prompt, backend="codex")

        self.assertEqual(result, "YES")

    def test_call_llm_surfaces_nonzero_exit(self):
        with mock.patch.object(
            llm.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["codex", "exec"], 2, stdout="", stderr="bad flag"
            ),
        ):
            result = llm._call_llm("hi", backend="codex")

        self.assertIn("status 2", result)
        self.assertIn("bad flag", result)

    def test_call_llm_infers_stdin_for_exec_style_templates(self):
        prompt = "Reply with exactly YES"
        llm.LLM_BACKENDS["compat_codex"] = {
            "cmd": ["codex", "exec", "--skip-git-repo-check",
                    "-o", "{output_path}", "-"],
            "model_flag": "--model",
            "env_filter": lambda env: dict(env),
        }

        def fake_run(cmd, **kwargs):
            self.assertEqual(kwargs.get("input"), prompt)
            output_index = cmd.index("-o") + 1
            Path(cmd[output_index]).write_text("YES\n", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        try:
            with mock.patch.object(llm.subprocess, "run", side_effect=fake_run):
                result = llm._call_llm(prompt, backend="compat_codex")
        finally:
            del llm.LLM_BACKENDS["compat_codex"]

        self.assertEqual(result, "YES")

    def test_binary_judge_fallback_codex_uses_exec(self):
        judge = binary_judge.BinaryLLMJudge()

        def fake_run(cmd, **kwargs):
            if cmd[0] == "claude":
                raise FileNotFoundError
            self.assertEqual(cmd[:3], ["codex", "exec", "--skip-git-repo-check"])
            self.assertEqual(kwargs.get("input"), "question")
            output_index = cmd.index("-o") + 1
            Path(cmd[output_index]).write_text("YES\n", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with mock.patch.object(
            binary_judge.subprocess, "run", side_effect=fake_run
        ):
            result = judge._fallback_call_llm("question")

        self.assertEqual(result, "YES")


if __name__ == "__main__":
    unittest.main()
