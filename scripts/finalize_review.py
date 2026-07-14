#!/usr/bin/env python3
"""CLI for safely finalizing one candidate review batch."""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.database.initialize import DEFAULT_DATABASE_PATH
from src.reviewing.finalize import finalize_review


def main():
    parser = argparse.ArgumentParser(description="AI二审批次收口并同步正式题库（默认仅预演）")
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--private-root", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="仅报告，不修改（默认）")
    mode.add_argument("--apply", action="store_true", help="备份后事务化应用")
    args = parser.parse_args()
    result = finalize_review(args.database, args.private_root, args.job_id, apply=args.apply)
    payload = dict(result.__dict__)
    payload["mode"] = "apply" if args.apply else "dry-run"
    for key, value in tuple(payload.items()):
        if isinstance(value, Path):
            payload[key] = str(value)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
