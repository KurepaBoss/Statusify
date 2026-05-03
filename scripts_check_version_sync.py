#!/usr/bin/env python3
"""Fail if README version strings diverge from runtime version."""
import re
import sys
from pathlib import Path

from version import VERSION

readme = Path("README.md").read_text(encoding="utf-8")

patterns = {
    "header": r"Statusify v([0-9]+\.[0-9]+\.[0-9]+)",
    "badge": r"badge/Statusify-v([0-9]+\.[0-9]+\.[0-9]+)-",
    "whats_new": r"What's New in v([0-9]+\.[0-9]+\.[0-9]+)",
}

errors = []
for label, pat in patterns.items():
    m = re.search(pat, readme)
    if not m:
        errors.append(f"Missing README {label} version pattern")
        continue
    found = m.group(1)
    if found != VERSION:
        errors.append(f"README {label} version {found} != runtime version {VERSION}")

if errors:
    print("Version sync check failed:")
    for e in errors:
        print(f"- {e}")
    sys.exit(1)

print(f"Version sync OK: {VERSION}")
