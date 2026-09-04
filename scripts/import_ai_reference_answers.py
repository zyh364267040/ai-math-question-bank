#!/usr/bin/env python3
"""Validate or apply one four-file AI reference-answer evidence bundle."""

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.database.initialize import DEFAULT_DATABASE_PATH
from src.processing.ai_reference_answers import (
    AiReferenceAnswerError,
    import_ai_reference_answers,
)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="严格校验并导入独立的 AI参考答案（默认仅 dry-run）"
    )
    parser.add_argument("source_json", type=Path, help="10 题源清单 JSON")
    parser.add_argument(
        "generator_json", type=Path,
        help="第一轮生成证据 JSON（不作为展示内容）",
    )
    parser.add_argument(
        "independent_json", type=Path,
        help="独立求解证据 JSON（不作为展示内容）",
    )
    parser.add_argument(
        "final_review_json", type=Path,
        help="最终复核 JSON（唯一可展示内容来源）",
    )
    parser.add_argument(
        "--database", type=Path, default=DEFAULT_DATABASE_PATH,
        help="已完成公共 schema 迁移的 SQLite 题库",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="实际写入；省略时只验证并报告",
    )
    args = parser.parse_args(argv)
    try:
        result = import_ai_reference_answers(
            args.database, args.source_json, args.generator_json,
            args.independent_json, args.final_review_json, apply=args.apply,
        )
    except (AiReferenceAnswerError, OSError) as error:
        parser.exit(2, f"AI参考答案导入失败：{error}\n")
    mode = "APPLIED" if args.apply else "DRY RUN"
    print(
        f"{mode}: inserted={result['inserted']} "
        f"unchanged={result['unchanged']} skipped={result['skipped']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
