import argparse
import re
from pathlib import Path


LINE_RE = re.compile(
    r"^\s*(\d+)\.\s+\[(\d{2}:\d{2}:\d{2},\d{3})\s-\s(\d{2}:\d{2}:\d{2},\d{3})\]\s+(.*)\s*$"
)
DEFAULT_REPEAT_MARKER = "〿"
NON_CONTENT_CHARS = set(" \t\r\n，。？！、,.!?;；:：\"'“”‘’（）()[]【】<>《》-—…")
STRONG_BREAK_CHARS = "。！？!?；;：:"
SOFT_BREAK_CHARS = "，、,"
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


def read_lines(path: Path) -> list[str]:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding).splitlines()
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("unknown", b"", 0, 1, f"Unable to decode {path}")


def parse_timestamp(value: str) -> int:
    hours, minutes, seconds_ms = value.split(":")
    seconds, millis = seconds_ms.split(",")
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + int(seconds) * 1_000
        + int(millis)
    )


def format_timestamp(total_ms: int) -> str:
    hours = total_ms // 3_600_000
    total_ms %= 3_600_000
    minutes = total_ms // 60_000
    total_ms %= 60_000
    seconds = total_ms // 1_000
    millis = total_ms % 1_000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


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
            issues.append(
                f'{classify_repeat_unit(repeated_unit)}: "{repeated_unit}" 连续出现 {best_repeat_count} 次'
            )
            index += best_unit_len * best_repeat_count
            changed = True
            continue

        parts.append(text[index])
        index += 1

    return "".join(parts), changed, issues


def parse_cleaned_entries(lines: list[str]) -> list[tuple[int, str, str, str]]:
    entries: list[tuple[int, str, str, str]] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        match = LINE_RE.match(line)
        if not match:
            continue
        index, start, end, text = match.groups()
        entries.append((int(index), start, end, text))
    return entries


def find_split_position(segment: str, max_chars: int) -> int:
    if len(segment) <= max_chars:
        return len(segment)

    window = segment[:max_chars]

    for break_chars in (STRONG_BREAK_CHARS, SOFT_BREAK_CHARS):
        for index in range(len(window) - 1, -1, -1):
            if window[index] in break_chars:
                return index + 1

    for index in range(len(window) - 1, max(max_chars // 2, 1) - 1, -1):
        if window[index].isspace():
            return index + 1

    return max_chars


def split_text_by_max_chars(text: str, max_chars: int) -> list[str]:
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]

    parts: list[str] = []
    remaining = text.strip()

    while remaining:
        if len(remaining) <= max_chars:
            parts.append(remaining)
            break

        split_pos = find_split_position(remaining, max_chars)
        current = remaining[:split_pos].rstrip()
        if not current:
            current = remaining[:max_chars]
            split_pos = len(current)

        parts.append(current)
        remaining = remaining[split_pos:].lstrip()

    return parts


def build_continuous_srt(
    entries: list[tuple[int, str, str, str]],
    marker: str,
    max_chars_per_caption: int,
) -> tuple[str, str]:
    srt_blocks: list[str] = []
    issue_blocks: list[str] = []
    current_start = 0
    next_srt_index = 1

    for cleaned_index, original_start, original_end, text in entries:
        start_ms = parse_timestamp(original_start)
        end_ms = parse_timestamp(original_end)
        duration = max(end_ms - start_ms, 1)
        current_end = current_start + duration

        marked_text, has_issue, issues = mark_inline_repetitions(text, marker=marker)
        chunks = split_text_by_max_chars(marked_text, max_chars_per_caption)
        chunk_weights = [max(len(chunk.strip()), 1) for chunk in chunks]
        total_weight = sum(chunk_weights)
        accumulated_end = current_start
        chunk_time_ranges: list[tuple[int, int]] = []

        for chunk_index, weight in enumerate(chunk_weights):
            chunk_start = accumulated_end
            if chunk_index == len(chunk_weights) - 1:
                chunk_end = current_end
            else:
                allocated = max(duration * weight // total_weight, 1)
                chunk_end = min(chunk_start + allocated, current_end - (len(chunk_weights) - chunk_index - 1))
            chunk_time_ranges.append((chunk_start, chunk_end))
            accumulated_end = chunk_end

        for chunk, (chunk_start, chunk_end) in zip(chunks, chunk_time_ranges):
            srt_blocks.append(
                "\n".join(
                    [
                        str(next_srt_index),
                        f"{format_timestamp(chunk_start)} --> {format_timestamp(chunk_end)}",
                        chunk,
                    ]
                )
            )
            next_srt_index += 1

        if has_issue:
            continuous_ranges = " | ".join(
                f"{format_timestamp(chunk_start)} --> {format_timestamp(chunk_end)}"
                for chunk_start, chunk_end in chunk_time_ranges
            )
            issue_blocks.append(
                "\n".join(
                    [
                        str(cleaned_index),
                        f"原始时间: {original_start} --> {original_end}",
                        f"连续时间: {continuous_ranges}",
                        f"原文: {text}",
                        f"替换后: {marked_text}",
                        f"问题: {'；'.join(issues)}",
                    ]
                )
            )

        current_start = current_end

    return "\n\n".join(srt_blocks) + ("\n" if srt_blocks else ""), "\n\n".join(issue_blocks) + (
        "\n" if issue_blocks else ""
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export cleaned subtitle lines to a continuous-timestamp SRT file."
    )
    parser.add_argument("--input", required=True, help="Path to the cleaned txt file.")
    parser.add_argument("--output", required=True, help="Path to the continuous SRT file.")
    parser.add_argument(
        "--repeat-marker",
        default=DEFAULT_REPEAT_MARKER,
        help="Marker used in the continuous SRT when repeated characters or phrases are detected.",
    )
    parser.add_argument(
        "--repeat-output",
        help="Optional path to a repeat-issue report for the continuous SRT output.",
    )
    parser.add_argument(
        "--max-chars-per-caption",
        type=int,
        default=20,
        help="Maximum number of characters per continuous SRT caption block.",
    )
    args = parser.parse_args()

    source = Path(args.input)
    target = Path(args.output)
    repeat_target = Path(args.repeat_output) if args.repeat_output else None

    entries = parse_cleaned_entries(read_lines(source))
    srt_text, repeat_text = build_continuous_srt(
        entries,
        marker=args.repeat_marker,
        max_chars_per_caption=args.max_chars_per_caption,
    )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(srt_text, encoding="utf-8")

    if repeat_target is not None:
        repeat_target.parent.mkdir(parents=True, exist_ok=True)
        repeat_target.write_text(repeat_text, encoding="utf-8")


if __name__ == "__main__":
    main()
