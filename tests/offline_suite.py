"""Run all tests with a fail-closed boundary for physical outputs."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from contextlib import ExitStack
from pathlib import Path
import unittest
import sys
from unittest.mock import patch


def main():
    root = Path(__file__).resolve().parent.parent
    violations = []

    def blocked(target, *args, **kwargs):
        violations.append(target)
        raise AssertionError("Unexpected real-world output in offline tests: " + target)
    with ExitStack() as stack:
        for target in ("midictl.win_input._send", "mido.open_input", "mido.open_output",
                       "midictl.actions.Volume._endpoint", "os.startfile",
                       "subprocess.Popen", "webbrowser.open"):
            stack.enter_context(patch(target, side_effect=lambda *a, target=target, **kw: blocked(target, *a, **kw)))
        stack.enter_context(patch("mido.get_input_names", return_value=[]))
        stack.enter_context(patch("mido.get_output_names", return_value=[]))
        suite = (unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:]) if sys.argv[1:] else
                 unittest.defaultTestLoader.discover(str(root / "tests"), top_level_dir=str(root)))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    if violations:
        print("BLOCKED unexpected output attempts: " + repr(violations), file=sys.stderr)
    return 0 if result.wasSuccessful() and not violations else 1


if __name__ == "__main__":
    raise SystemExit(main())
