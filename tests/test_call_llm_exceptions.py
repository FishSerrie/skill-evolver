import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import importlib.util

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "plugin" / "skills" / "skill-evolver" / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


llm = _load_module("skill_evolver_llm", SCRIPTS_DIR / "llm.py")


class CallLlmBroadExceptionHandlingTests(unittest.TestCase):
    """Real bug found via adversarial review: _call_llm only caught
    subprocess.TimeoutExpired and FileNotFoundError — any other
    subprocess.run exception (OSError/E2BIG from an oversized argv,
    UnicodeDecodeError from non-UTF-8 output) propagated uncaught and
    would crash the whole evolve loop instead of degrading one call."""

    def test_oserror_degrades_to_error_string_not_exception(self):
        with mock.patch.object(llm.subprocess, "run",
                               side_effect=OSError("[Errno 7] Argument list too long")):
            result = llm._call_llm("hi", backend="claude")
        self.assertIn("[ERROR:", result)
        self.assertIn("Argument list too long", result)

    def test_unicode_decode_error_degrades_to_error_string_not_exception(self):
        err = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        with mock.patch.object(llm.subprocess, "run", side_effect=err):
            result = llm._call_llm("hi", backend="claude")
        self.assertIn("[ERROR:", result)

    def test_timeout_and_filenotfound_still_handled_as_before(self):
        with mock.patch.object(llm.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=180)):
            result = llm._call_llm("hi", backend="claude", timeout=180)
        self.assertIn("timed out", result)

        with mock.patch.object(llm.subprocess, "run", side_effect=FileNotFoundError()):
            result = llm._call_llm("hi", backend="claude")
        self.assertIn("CLI not found", result)


if __name__ == "__main__":
    unittest.main()
