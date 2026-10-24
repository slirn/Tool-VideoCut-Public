import argparse
import re
from dataclasses import dataclass
from pathlib import Path


LINE_RE = re.compile(
    r"^(?P<index>\d+)\.\s+\[(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s-\s(?P<end>\d{2}:\d{2}:\d{2},\d{3})\]\s+(?P<text>.*)$"
)
DEFAULT_REPEAT_MARKER = "〿"
DEFAULT_RULES_PATH = Path(__file__).resolve().parent.parent / "references" / "validation-rules.md"
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
DEFAULT_TERM_CONFUSIONS = {
    "短句": "短剧",
    "短局": "短剧",
    "短距": "短剧",
    "短缺": "短剧",
    "短学": "短剧",
    "慢剧": "漫剧",
    "视脚": "视觉",
}
TERM_RULE_RE = re.compile(r"`([^`]+)`\s*->\s*`([^`]+)`")


@dataclass
class SpanIssue:
    start: int
    end: int
    issue: str
    suggestion: str | None = None


def read_lines(path: Path) -> list[str]:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding).splitlines()
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("unknown", b"", 0, 1, f"Unable to decode {path}")


def load_term_confusions(path: Path | None) -> dict[str, str]:
    term_confusions = dict(DEFAULT_TERM_CONFUSIONS)
    if not path or not path.exists():
        return term_confusions

    for line in read_lines(path):
        match = TERM_RULE_RE.search(line)
        if not match:
            continue
        wrong_term = match.group(1).strip()
        correct_term = match.group(2).strip()
        if wrong_term and correct_term and wrong_term != correct_term:
            term_confusions[wrong_term] = correct_term

    return term_confusions


def is_meaningful_repeat_unit(unit: str) -> bool:
    return any((char not in NON_CONTENT_CHARS) for char in unit)


def classify_repeat_unit(unit: str) -> str:
    if len(unit) == 1:
        return "单字重复"
    if len(unit) <= 3:
        return "词语重复"
    return "短语重复"


def is_semantically_valid_reduplication(text: str, index: int, unit: str, repeat_count: int) -> bool:
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


def detect_repetition_issues(text: str, max_unit_len: int = 6) -> list[SpanIssue]:
    issues: list[SpanIssue] = []
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
            for copy_index in range(1, best_repeat_count):
                start = index + best_unit_len * copy_index
                end = start + best_unit_len
                issues.append(
                    SpanIssue(
                        start=start,
                        end=end,
                        issue=f'{classify_repeat_unit(repeated_unit)}: "{repeated_unit}" 连续出现 {best_repeat_count} 次',
                    )
                )
            index += best_unit_len * best_repeat_count
            continue

        index += 1

    return issues


def detect_term_confusions(text: str, term_confusions: dict[str, str]) -> list[SpanIssue]:
    issues: list[SpanIssue] = []
    for wrong_term, correct_term in term_confusions.items():
        search_start = 0
        while True:
            found = text.find(wrong_term, search_start)
            if found == -1:
                break
            issues.append(
                SpanIssue(
                    start=found,
                    end=found + len(wrong_term),
                    issue=f'异常名词/同音词: "{wrong_term}"',
                    suggestion=correct_term,
                )
            )
            search_start = found + len(wrong_term)
    return issues


def merge_spans(spans: list[SpanIssue]) -> list[SpanIssue]:
    sorted_spans = sorted(spans, key=lambda item: (item.start, item.end))
    merged: list[SpanIssue] = []
    occupied_end = -1

    for span in sorted_spans:
        if span.start < occupied_end:
            continue
        merged.append(span)
        occupied_end = span.end

    return merged


def apply_marker(text: str, spans: list[SpanIssue], marker: str) -> str:
    if not spans:
        return text

    parts: list[str] = []
    cursor = 0
    for span in spans:
        if cursor < span.start:
            parts.append(text[cursor : span.start])
        parts.append(marker * max(span.end - span.start, 1))
        cursor = span.end

    if cursor < len(text):
        parts.append(text[cursor:])

    return "".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate a manually finalized subtitle file by checking repetition and abnormal-term issues."
    )
    parser.add_argument("--input", required=True, help="Path to the manually finalized kept-content file.")
    parser.add_argument("--output", required=True, help="Path to the validation report file.")
    parser.add_argument(
        "--repeat-marker",
        default=DEFAULT_REPEAT_MARKER,
        help="Marker used to replace problematic repeated characters, words, or suspicious terms.",
    )
    parser.add_argument(
        "--term-rules",
        default=str(DEFAULT_RULES_PATH),
        help="Path to the markdown rules file that contains abnormal-term mappings in the form `错误词` -> `建议词`.",
    )
    args = parser.parse_args()

    source = Path(args.input)
    target = Path(args.output)
    term_rules_path = Path(args.term_rules) if args.term_rules else None
    term_confusions = load_term_confusions(term_rules_path)
    output_lines: list[str] = []

    for raw_line in read_lines(source):
        line = raw_line.strip()
        if not line:
            continue

        match = LINE_RE.match(line)
        if not match:
            continue

        index = match.group("index")
        start = match.group("start")
        end = match.group("end")
        text = match.group("text")

        span_issues = merge_spans(
            detect_repetition_issues(text) + detect_term_confusions(text, term_confusions)
        )
        if not span_issues:
            continue

        marked_text = apply_marker(text, span_issues, args.repeat_marker)
        issue_text = "；".join(dict.fromkeys(issue.issue for issue in span_issues))
        suggestions = [issue.suggestion for issue in span_issues if issue.suggestion]
        suggestion_text = "；".join(f'建议改为 "{item}"' for item in dict.fromkeys(suggestions))

        output_lines.append(f"{index}. [{start} - {end}] {text}")
        output_lines.append(f"标记后: {index}. [{start} - {end}] {marked_text}")
        output_lines.append(f"问题: {issue_text}")
        if suggestion_text:
            output_lines.append(f"建议: {suggestion_text}")
        output_lines.append("")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(output_lines).rstrip() + ("\n" if output_lines else ""), encoding="utf-8")


if __name__ == "__main__":
    main()
