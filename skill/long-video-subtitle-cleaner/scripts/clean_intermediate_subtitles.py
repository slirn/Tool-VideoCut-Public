import argparse
import re
from pathlib import Path


LINE_RE = re.compile(
    r"^\s*(\d+)\.\s+\[(\d{2}:\d{2}:\d{2},\d{3})\s-\s(\d{2}:\d{2}:\d{2},\d{3})\]\s+(.*)\s*$"
)
DEFAULT_REPEAT_MARKER = "〿"
NON_CONTENT_CHARS = set(" \t\r\n，。？！、,.!?;；:：\"'“”‘’（）()[]【】<>《》-—…")
ALLOWED_SINGLE_CHAR_REDUP_WORDS = {
    "看看",
    "想想",
    "说说",
    "聊聊",
    "听听",
    "试试",
    "问问",
    "找找",
    "查查",
    "讲讲",
    "谈谈",
    "算算",
    "写写",
    "读读",
    "画画",
    "点点",
    "翻翻",
    "练练",
    "学学",
    "走走",
    "逛逛",
    "比比",
    "瞧瞧",
    "摸摸",
    "闻闻",
    "尝尝",
    "搜搜",
}
ALLOWED_MULTI_CHAR_REDUP_WORDS = {
    "研究研究",
    "讨论讨论",
    "分析分析",
    "考虑考虑",
    "学习学习",
    "整理整理",
    "沟通沟通",
    "商量商量",
    "琢磨琢磨",
    "对比对比",
}
NUMERIC_PREFIX_CHARS = set("一二三四五六七八九十百千万两几多每整0123456789")
MEASURE_REDUP_CHARS = {"块", "层", "段", "片", "页", "行", "点", "次", "遍", "个"}

FILLER_ONLY = {
    "嗯",
    "啊",
    "呃",
    "哎",
    "哎呀",
    "哦",
    "额",
    "对",
    "对吧",
    "好",
    "好的",
}

INTERACTION_KEYWORDS = [
    "能看到吗",
    "看得到吗",
    "听得到吗",
    "能听清吗",
    "再大一点",
    "大一点",
    "大小",
    "打开",
    "开那个",
    "点这里",
    "底部",
    "这里这里",
    "够大了",
    "下载旁边",
    "演示",
    "PPT",
    "屏幕",
    "麦克风",
    "声音",
    "网络",
    "感冒",
    "快一点",
    "一分钟了",
    "讲再讲",
    "老师会讲到",
    "后面还有两位老师",
    "后面老师",
    "大家看一下",
]

DISFLUENCY_PATTERNS = [
    "这个我我",
    "我这边我这边",
    "然后然后",
    "不不",
    "OKOK",
]


def read_lines(path: Path) -> list[str]:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding).splitlines()
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("unknown", b"", 0, 1, f"Unable to decode {path}")


def is_filler_only(text: str) -> bool:
    stripped = re.sub(r"[，。？！、,.!?\s]+", "", text)
    return stripped in FILLER_ONLY


def has_interaction_keyword(text: str) -> bool:
    return any(keyword in text for keyword in INTERACTION_KEYWORDS)


def is_obvious_noise(text: str) -> bool:
    if is_filler_only(text):
        return True
    if has_interaction_keyword(text):
        return True
    if len(text.strip()) <= 1:
        return True
    if len(text.strip()) <= 4 and "？" not in text and "?" not in text:
        return True
    if text.count("？") >= 1 and len(text) <= 12:
        return True
    if re.fullmatch(r"(不|嗯|啊|呃|哦|对|好)+", re.sub(r"\s+", "", text)):
        return True
    return False


def looks_broken(text: str) -> bool:
    if "问啥" in text or "这个块的话" in text:
        return True
    if text.endswith(("可", "吧", "哦", "啊")) and len(text) <= 10:
        return True
    if text.startswith(("就是", "然后", "所以")) and len(text) <= 6:
        return True
    if sum(pattern in text for pattern in DISFLUENCY_PATTERNS) >= 2:
        return True
    return False


def removal_reason(text: str, previous_kept: str | None) -> str | None:
    if is_filler_only(text):
        return "口水词/语气词"
    if has_interaction_keyword(text):
        return "课堂互动或设备调试内容"
    if len(text.strip()) <= 1:
        return "无效短句"
    if len(text.strip()) <= 4 and "？" not in text and "?" not in text:
        return "碎片化短句"
    if text.count("？") >= 1 and len(text) <= 12:
        return "简短问答互动"
    if re.fullmatch(r"(不|嗯|啊|呃|哦|对|好)+", re.sub(r"\s+", "", text)):
        return "重复语气词"
    if previous_kept and text == previous_kept:
        return "重复内容"
    if looks_broken(text) and len(text) < 24:
        return "逻辑不完整或识别混乱"
    return None


def renumber(lines: list[str]) -> list[str]:
    output: list[str] = []
    next_index = 1
    for line in lines:
        match = LINE_RE.match(line)
        if not match:
            continue
        _, start, end, text = match.groups()
        output.append(f"{next_index}. [{start} - {end}] {text}")
        next_index += 1
    return output


def escape_markdown_cell(text: str) -> str:
    return text.replace("|", r"\|").replace("\n", " ").strip()


def is_meaningful_repeat_unit(unit: str) -> bool:
    return any((char not in NON_CONTENT_CHARS) for char in unit)


def classify_repeat_unit(unit: str) -> str:
    if len(unit) == 1:
        return "单字重复"
    if len(unit) <= 3:
        return "词语重复"
    return "短语重复"


def is_semantically_valid_reduplication(
    text: str,
    index: int,
    unit: str,
    repeat_count: int,
) -> bool:
    if repeat_count != 2:
        return False

    repeated_text = unit * repeat_count
    unit_len = len(unit)

    if unit_len == 1:
        if repeated_text in ALLOWED_SINGLE_CHAR_REDUP_WORDS:
            return True

        previous_char = text[index - 1] if index > 0 else ""
        if previous_char in NUMERIC_PREFIX_CHARS and unit in MEASURE_REDUP_CHARS:
            return True

        return False

    if repeated_text in ALLOWED_MULTI_CHAR_REDUP_WORDS:
        return True

    return False


def mark_inline_repetitions(
    text: str,
    marker: str = DEFAULT_REPEAT_MARKER,
    max_unit_len: int = 6,
) -> tuple[str, bool, list[str]]:
    if not text:
        return text, False, []

    parts: list[str] = []
    changed = False
    issues: list[str] = []
    index = 0
    text_length = len(text)

    while index < text_length:
        best_unit_len = 0
        best_repeat_count = 1
        max_probe_len = min(max_unit_len, (text_length - index) // 2)

        for unit_len in range(max_probe_len, 0, -1):
            unit = text[index : index + unit_len]
            if not is_meaningful_repeat_unit(unit):
                continue

            repeat_count = 1
            probe = index + unit_len
            while probe + unit_len <= text_length and text[probe : probe + unit_len] == unit:
                repeat_count += 1
                probe += unit_len

            if repeat_count > 1 and not is_semantically_valid_reduplication(
                text, index, unit, repeat_count
            ):
                best_unit_len = unit_len
                best_repeat_count = repeat_count
                break

        if best_repeat_count > 1:
            repeated_unit = text[index : index + best_unit_len]
            parts.append(repeated_unit)
            parts.append(marker * (best_unit_len * (best_repeat_count - 1)))
            index += best_unit_len * best_repeat_count
            changed = True
            issues.append(
                f'{classify_repeat_unit(repeated_unit)}: "{repeated_unit}" 连续出现 {best_repeat_count} 次'
            )
            continue

        parts.append(text[index])
        index += 1

    return "".join(parts), changed, issues


def format_source_line(index: str, start: str, end: str, text: str, deleted: bool) -> str:
    display_text = f"~~{text}~~" if deleted else text
    return f"{index}. [{start} - {end}] {display_text}"


def build_dual_column_review(review_rows: list[tuple[str, str]]) -> str:
    rows = ["| 中间文件 | 处理结果 |", "| --- | --- |"]

    for source_line, result_line in review_rows:
        rows.append(
            f"| {escape_markdown_cell(source_line)} | {escape_markdown_cell(result_line)} |"
        )

    return "\n".join(rows) + ("\n" if rows else "")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clean intermediate subtitle lines for teaching-content outputs."
    )
    parser.add_argument("--input", required=True, help="Path to the intermediate txt file.")
    parser.add_argument("--output", required=True, help="Path to the cleaned txt file.")
    parser.add_argument(
        "--removed-output",
        help="Optional path to a comparison file that records removed lines and reasons.",
    )
    parser.add_argument(
        "--review-output",
        help="Optional path to a dual-column review file with kept lines on the left and removed lines on the right.",
    )
    parser.add_argument(
        "--repeat-marker",
        default=DEFAULT_REPEAT_MARKER,
        help="Marker used to replace extra repeated characters or phrase copies in kept lines.",
    )
    parser.add_argument(
        "--repeat-output",
        help="Optional path to a repeat-only file. Only lines with inline repetition replacements are written.",
    )
    args = parser.parse_args()

    source = Path(args.input)
    target = Path(args.output)
    removed_target = Path(args.removed_output) if args.removed_output else None
    review_target = Path(args.review_output) if args.review_output else None
    repeat_target = Path(args.repeat_output) if args.repeat_output else None

    kept_lines: list[str] = []
    removed_lines: list[str] = []
    review_rows: list[tuple[str, str]] = []
    repeat_lines: list[str] = []
    previous_kept_text: str | None = None
    next_kept_index = 1
    repeat_marker = args.repeat_marker

    for raw_line in read_lines(source):
        line = raw_line.strip()
        if not line:
            continue

        match = LINE_RE.match(line)
        if not match:
            continue

        original_index, start, end, text = match.groups()
        reason = removal_reason(text, previous_kept_text)
        if reason is not None:
            source_line = format_source_line(original_index, start, end, text, deleted=True)
            removed_line = f"{source_line} || 删除原因: {reason}"
            removed_lines.append(removed_line)
            review_rows.append((source_line, f"删除: {reason}"))
            continue

        source_line = format_source_line(original_index, start, end, text, deleted=False)
        normalized_text, has_repeat_issue, repeat_issues = mark_inline_repetitions(
            text, marker=repeat_marker
        )
        kept_line = f"{next_kept_index}. [{start} - {end}] {text}"
        kept_lines.append(kept_line)
        review_rows.append((source_line, f"保留: {next_kept_index}"))
        if has_repeat_issue:
            repeat_lines.append(
                "\n".join(
                    [
                        f"{next_kept_index}. [{start} - {end}] {text}",
                        f"替换后: {next_kept_index}. [{start} - {end}] {normalized_text}",
                        f"问题: {'；'.join(repeat_issues)}",
                    ]
                )
            )
        previous_kept_text = text
        next_kept_index += 1

    output_lines = kept_lines
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(output_lines) + ("\n" if output_lines else ""), encoding="utf-8")

    if removed_target is not None:
        removed_target.parent.mkdir(parents=True, exist_ok=True)
        removed_target.write_text(
            "\n".join(removed_lines) + ("\n" if removed_lines else ""),
            encoding="utf-8",
        )

    if review_target is not None:
        review_target.parent.mkdir(parents=True, exist_ok=True)
        review_target.write_text(
            build_dual_column_review(review_rows),
            encoding="utf-8",
        )

    if repeat_target is not None:
        repeat_target.parent.mkdir(parents=True, exist_ok=True)
        repeat_target.write_text(
            "\n\n".join(repeat_lines) + ("\n" if repeat_lines else ""),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
