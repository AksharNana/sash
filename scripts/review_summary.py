#!/usr/bin/env -S uv run python3
"""Summarize a review progress file.

Usage:
    uv run python scripts/review_summary.py results/llm/<name>.report.review.json
"""

import json
import re
import sys
from pathlib import Path

_FRACTION_RE = re.compile(r"^(\d+)/(\d+)(.*)$")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: uv run python scripts/review_summary.py <review.json>")
        sys.exit(1)

    review_path = Path(sys.argv[1])
    if not review_path.exists():
        print(f"File not found: {review_path}", file=sys.stderr)
        sys.exit(1)

    data = json.loads(review_path.read_text(encoding="utf-8"))
    reviewed = data.get("reviewed", {})

    found = 0
    total = 0
    missed: list[tuple[str, str]] = []
    additional: list[tuple[str, str]] = []

    for bench_path, info in sorted(reviewed.items()):
        status = info.get("status", "").strip()
        m = _FRACTION_RE.match(status)
        name = Path(bench_path).parent.name
        if m:
            x = int(m.group(1))
            y = int(m.group(2))
            found += x
            total += y
            extra = m.group(3).strip()
            if x != y:
                missed.append((name, status))
            if extra:
                additional.append((name, status))
        else:
            additional.append((name, status))

    print(f"Bugs found: {found}/{total}")

    if missed:
        print()
        print("Missed bugs:")
        for name, note in missed:
            print(f"- {name}: {note}")

    if additional:
        print()
        print("Additional notes:")
        for name, note in additional:
            print(f"- {name}: {note}")


if __name__ == "__main__":
    main()
