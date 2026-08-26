#!/usr/bin/env python3
"""Copy the self-contained web application into the projects-site sub-page."""

from __future__ import annotations

import shutil
from pathlib import Path


SOURCE = Path(__file__).resolve().parent / "web"
DESTINATION = Path.home() / "projects-site" / "p" / "sql-explorer"


def main() -> None:
    if not SOURCE.is_dir():
        raise SystemExit(f"missing source tree: {SOURCE}")

    if DESTINATION.exists():
        shutil.rmtree(DESTINATION)
    shutil.copytree(SOURCE, DESTINATION)

    written = sorted(path.relative_to(DESTINATION) for path in DESTINATION.rglob("*") if path.is_file())
    print(f"wrote {DESTINATION} ({len(written)} files)")
    for path in written:
        print(path)


if __name__ == "__main__":
    main()
