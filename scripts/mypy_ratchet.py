#!/usr/bin/env python3
"""Run mypy and fail if the error count rises above the checked-in baseline.

The integration ships py.typed and mypy.ini asks for real checking, but CI ran
mypy with continue-on-error, so new type errors landed green (issue #107).
Fixing every existing error at once is a large step; this ratchet gets most of
the value now. The baseline can only go down: when a run comes in under it,
the script says so and asks for the baseline to be lowered.

Usage: python scripts/mypy_ratchet.py [--update]
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASELINE = ROOT / "mypy_baseline.txt"
TARGET = "custom_components/nem_pd7day/"


def main() -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", TARGET],
        cwd=ROOT, capture_output=True, text=True,
    )
    output = proc.stdout + proc.stderr
    print(output, end="")
    match = re.search(r"Found (\d+) errors?", output)
    if match:
        count = int(match.group(1))
    elif "Success: no issues found" in output:
        count = 0
    else:
        print("mypy_ratchet: could not read an error count from mypy output")
        return 2

    if "--update" in sys.argv:
        BASELINE.write_text(f"{count}\n")
        print(f"mypy_ratchet: baseline set to {count}")
        return 0

    baseline = int(BASELINE.read_text().strip())
    if count > baseline:
        print(f"mypy_ratchet: {count} errors, baseline is {baseline}: new type errors were introduced")
        return 1
    if count < baseline:
        print(f"mypy_ratchet: {count} errors, under the baseline of {baseline}; "
              f"lower mypy_baseline.txt to {count} so it cannot creep back")
    else:
        print(f"mypy_ratchet: {count} errors, at baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
