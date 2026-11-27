#!/usr/bin/python3
"""Run every test in this directory and summarise.

    python3 tests/run.py

test_sigv4 needs botocore and skips itself without it:

    pip install botocore
"""
import subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
failed = []
for test in sorted(HERE.glob("test_*.py")):
    print(f"\n\033[1m{test.stem}\033[0m")
    result = subprocess.run([sys.executable, "-I", str(test)], capture_output=True, text=True)
    body = (result.stdout + result.stderr).strip()
    print("\n".join("  " + line for line in body.splitlines()[-40:]))
    if result.returncode != 0:
        failed.append(test.stem)

print("\n" + ("All green." if not failed else "Failed: " + ", ".join(failed)))
sys.exit(1 if failed else 0)
