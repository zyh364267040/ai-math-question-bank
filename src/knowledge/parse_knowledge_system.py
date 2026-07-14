"""Parse the high-school mathematics Markdown hierarchy into stable seed rows."""

import argparse
import json
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MARKDOWN_PATH = PROJECT_ROOT / "docs" / "domain" / "高中数学高考知识点体系.md"
DEFAULT_JSON_PATH = PROJECT_ROOT / "data" / "samples" / "knowledge_points_v1.json"
SYSTEM_VERSION = "v1"

MODULE_RE = re.compile(r"^## (\d{2})\s+(.+?)\s*$")
TOPIC_RE = re.compile(r"^### (\d{2}\.\d{2})\s+(.+?)\s*$")
BULLET_RE = re.compile(r"^-\s+(.+?)\s*$")


class KnowledgeSystemError(ValueError):
    """Raised when the Markdown or generated hierarchy is not structurally safe."""


def _point(code, name, level, parent_code, sort_order, system_version):
    return {
        "code": code,
        "name": name,
        "level": level,
        "parent_code": parent_code,
        "system_version": system_version,
        "sort_order": sort_order,
    }


def parse_knowledge_system(markdown_path=DEFAULT_MARKDOWN_PATH, system_version=SYSTEM_VERSION):
    """Return validated level 1-3 points parsed from modules ``## 01`` to ``## 11``."""
    path = Path(markdown_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    points = []
    modules_seen = set()
    topics_seen = set()
    current_module = None
    current_topic = None
    topic_names = set()
    topic_child_count = 0
    parsing = False

    def finish_topic(line_number):
        if current_topic is not None and topic_child_count == 0:
            raise KnowledgeSystemError(
                f"第 {line_number} 行前的二级专题 {current_topic} 没有三级知识点"
            )

    for line_number, line in enumerate(lines, start=1):
        module_match = MODULE_RE.match(line)
        if module_match:
            code, name = module_match.groups()
            if not parsing:
                if code != "01":
                    continue
                parsing = True
            if code not in {f"{number:02d}" for number in range(1, 12)}:
                finish_topic(line_number)
                break
            finish_topic(line_number)
            if code in modules_seen:
                raise KnowledgeSystemError(f"第 {line_number} 行一级模块编号重复：{code}")
            modules_seen.add(code)
            current_module = code
            current_topic = None
            topic_names = set()
            topic_child_count = 0
            points.append(_point(code, name, 1, None, int(code), system_version))
            continue

        if parsing and line.startswith("## "):
            finish_topic(line_number)
            break
        if not parsing:
            continue

        topic_match = TOPIC_RE.match(line)
        if topic_match:
            finish_topic(line_number)
            code, name = topic_match.groups()
            if current_module is None or code.split(".")[0] != current_module:
                raise KnowledgeSystemError(
                    f"第 {line_number} 行二级专题 {code} 不属于当前一级模块 {current_module}"
                )
            if code in topics_seen:
                raise KnowledgeSystemError(f"第 {line_number} 行二级专题编号重复：{code}")
            topics_seen.add(code)
            current_topic = code
            topic_names = set()
            topic_child_count = 0
            points.append(
                _point(code, name, 2, current_module, int(code.split(".")[1]), system_version)
            )
            continue

        if line.startswith("### "):
            raise KnowledgeSystemError(f"第 {line_number} 行二级专题标题格式或编号错误：{line}")

        bullet_match = BULLET_RE.match(line)
        if bullet_match:
            if current_topic is None:
                raise KnowledgeSystemError(
                    f"第 {line_number} 行三级知识点不在任何二级专题内：{bullet_match.group(1)}"
                )
            name = bullet_match.group(1)
            if name in topic_names:
                raise KnowledgeSystemError(
                    f"第 {line_number} 行二级专题 {current_topic} 存在重复三级知识点：{name}"
                )
            topic_names.add(name)
            topic_child_count += 1
            points.append(
                _point(
                    f"{current_topic}.{topic_child_count:02d}",
                    name,
                    3,
                    current_topic,
                    topic_child_count,
                    system_version,
                )
            )

    if parsing:
        finish_topic(len(lines) + 1)
    expected_modules = {f"{number:02d}" for number in range(1, 12)}
    if modules_seen != expected_modules:
        missing = sorted(expected_modules - modules_seen)
        raise KnowledgeSystemError(
            f"一级模块必须完整且唯一地包含 01 至 11；缺少：{', '.join(missing) or '无'}"
        )
    return validate_knowledge_points(points)


def validate_knowledge_points(points):
    """Validate seed rows and return them unchanged for convenient composition."""
    required = {"code", "name", "level", "parent_code", "system_version", "sort_order"}
    codes = set()
    for position, point in enumerate(points, start=1):
        if set(point) != required:
            raise KnowledgeSystemError(f"第 {position} 条种子字段不完整或含未知字段")
        code = point["code"]
        if code in codes:
            raise KnowledgeSystemError(f"知识点稳定代码重复：{code}")
        codes.add(code)
        if point["level"] not in (1, 2, 3):
            raise KnowledgeSystemError(f"知识点 {code} 的层级无效：{point['level']}")
        if not isinstance(point["sort_order"], int) or point["sort_order"] < 1:
            raise KnowledgeSystemError(f"知识点 {code} 的排序值必须为正整数")
    for point in points:
        parent = point["parent_code"]
        if point["level"] == 1 and parent is not None:
            raise KnowledgeSystemError(f"一级模块 {point['code']} 不应有父级")
        if point["level"] > 1 and parent not in codes:
            raise KnowledgeSystemError(f"知识点 {point['code']} 的 parent_code 不存在：{parent}")
    return points


def write_knowledge_points_json(points, output_path=DEFAULT_JSON_PATH):
    """Write human-reviewable UTF-8 JSON with Chinese characters preserved."""
    validated = validate_knowledge_points(points)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(validated, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser(description="生成高中数学知识点稳定 JSON 种子")
    parser.add_argument("markdown_path", nargs="?", type=Path, default=DEFAULT_MARKDOWN_PATH)
    parser.add_argument("output_path", nargs="?", type=Path, default=DEFAULT_JSON_PATH)
    args = parser.parse_args()
    points = parse_knowledge_system(args.markdown_path)
    write_knowledge_points_json(points, args.output_path)
    counts = {level: sum(point["level"] == level for point in points) for level in (1, 2, 3)}
    print(f"已生成：{args.output_path.resolve()}（一级 {counts[1]}，二级 {counts[2]}，三级 {counts[3]}）")


if __name__ == "__main__":
    main()
