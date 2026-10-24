import argparse
import re
from dataclasses import dataclass
from pathlib import Path


LINE_RE = re.compile(
    r"^\s*(\d+)\.\s+\[(\d{2}:\d{2}:\d{2},\d{3})\s-\s(\d{2}:\d{2}:\d{2},\d{3})\]\s+(.*)\s*$"
)
STRONG_BREAK_CHARS = "。！？!?；;：:"
SOFT_BREAK_CHARS = "，、,"


@dataclass
class FinalEntry:
    index: int
    original_start: str
    original_end: str
    text: str


@dataclass
class ValidationEntry:
    index: int
    original_start: str
    original_end: str
    original_text: str
    marked_text: str
    issue_text: str
    suggestion_text: str


@dataclass
class ContinuousEntry:
    index: int
    original_start: str
    original_end: str
    original_text: str
    marked_text: str
    issue_text: str
    suggestion_text: str
    chunk_indices: list[int]
    chunk_ranges: list[tuple[int, int]]
    original_chunks: list[str]
    marked_chunks: list[str]


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


def parse_caption_line(line: str) -> tuple[int, str, str, str] | None:
    match = LINE_RE.match(line.strip())
    if not match:
        return None
    index, start, end, text = match.groups()
    return int(index), start, end, text


def parse_final_entries(path: Path) -> list[FinalEntry]:
    entries: list[FinalEntry] = []
    for raw_line in read_lines(path):
        parsed = parse_caption_line(raw_line)
        if not parsed:
            continue
        index, start, end, text = parsed
        entries.append(FinalEntry(index=index, original_start=start, original_end=end, text=text))
    return entries


def parse_validation_entries(path: Path) -> dict[int, ValidationEntry]:
    validation_map: dict[int, ValidationEntry] = {}
    block_lines: list[str] = []

    def flush_block(lines: list[str]) -> None:
        if not lines:
            return

        original_line = lines[0].strip()
        parsed_original = parse_caption_line(original_line)
        if not parsed_original:
            return

        marked_line = ""
        issue_line = ""
        suggestion_line = ""

        for line in lines[1:]:
            stripped = line.strip()
            if stripped.startswith("标记后:"):
                marked_line = stripped[len("标记后:") :].strip()
            elif stripped.startswith("问题:"):
                issue_line = stripped[len("问题:") :].strip()
            elif stripped.startswith("建议:"):
                suggestion_line = stripped[len("建议:") :].strip()

        parsed_marked = parse_caption_line(marked_line) if marked_line else None
        if not parsed_marked:
            return

        index, start, end, text = parsed_original
        _, _, _, marked_text = parsed_marked
        validation_map[index] = ValidationEntry(
            index=index,
            original_start=start,
            original_end=end,
            original_text=text,
            marked_text=marked_text,
            issue_text=issue_line,
            suggestion_text=suggestion_line,
        )

    for raw_line in read_lines(path):
        if raw_line.strip():
            block_lines.append(raw_line)
            continue
        flush_block(block_lines)
        block_lines = []

    flush_block(block_lines)
    return validation_map


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


def split_text_with_ranges(text: str, max_chars: int) -> list[tuple[int, int, str]]:
    normalized = text.strip()
    if not normalized:
        return [(0, 0, "")]
    if max_chars <= 0 or len(normalized) <= max_chars:
        return [(0, len(normalized), normalized)]

    parts: list[tuple[int, int, str]] = []
    start = 0

    while start < len(normalized):
        remaining = normalized[start:]
        if len(remaining) <= max_chars:
            end = len(normalized)
        else:
            end = start + find_split_position(remaining, max_chars)

        segment = normalized[start:end].strip()
        if not segment:
            end = min(start + max_chars, len(normalized))
            segment = normalized[start:end]

        parts.append((start, end, segment))
        start = end
        while start < len(normalized) and normalized[start].isspace():
            start += 1

    return parts


def allocate_chunk_ranges(
    current_start: int,
    duration: int,
    chunk_texts: list[str],
) -> list[tuple[int, int]]:
    current_end = current_start + duration
    weights = [max(len(chunk.strip()), 1) for chunk in chunk_texts]
    total_weight = sum(weights)
    accumulated_end = current_start
    chunk_ranges: list[tuple[int, int]] = []

    for chunk_index, weight in enumerate(weights):
        chunk_start = accumulated_end
        remaining_chunks = len(weights) - chunk_index - 1
        if chunk_index == len(weights) - 1:
            chunk_end = current_end
        else:
            allocated = max(duration * weight // total_weight, 1)
            chunk_end = min(chunk_start + allocated, current_end - remaining_chunks)
        chunk_ranges.append((chunk_start, chunk_end))
        accumulated_end = chunk_end

    return chunk_ranges


def build_continuous_entries(
    final_entries: list[FinalEntry],
    validation_map: dict[int, ValidationEntry],
    max_chars_per_caption: int,
) -> list[ContinuousEntry]:
    continuous_entries: list[ContinuousEntry] = []
    current_start = 0
    next_chunk_index = 1

    for entry in final_entries:
        validation = validation_map.get(entry.index)
        marked_text = validation.marked_text if validation else entry.text
        issue_text = validation.issue_text if validation else ""
        suggestion_text = validation.suggestion_text if validation else ""

        split_ranges = split_text_with_ranges(entry.text, max_chars_per_caption)
        original_chunks = [segment for _, _, segment in split_ranges]
        marked_source = marked_text.strip()
        marked_chunks = [marked_source[start:end].strip() for start, end, _ in split_ranges]

        duration = max(parse_timestamp(entry.original_end) - parse_timestamp(entry.original_start), 1)
        chunk_ranges = allocate_chunk_ranges(current_start, duration, original_chunks)
        chunk_indices = list(range(next_chunk_index, next_chunk_index + len(original_chunks)))

        continuous_entries.append(
            ContinuousEntry(
                index=entry.index,
                original_start=entry.original_start,
                original_end=entry.original_end,
                original_text=entry.text,
                marked_text=marked_text,
                issue_text=issue_text,
                suggestion_text=suggestion_text,
                chunk_indices=chunk_indices,
                chunk_ranges=chunk_ranges,
                original_chunks=original_chunks,
                marked_chunks=marked_chunks,
            )
        )

        next_chunk_index += len(original_chunks)
        current_start += duration

    return continuous_entries


def build_srt(entries: list[ContinuousEntry], use_marked_text: bool) -> str:
    blocks: list[str] = []

    for entry in entries:
        chunk_texts = entry.marked_chunks if use_marked_text else entry.original_chunks
        for chunk_index, chunk_text, (start_ms, end_ms) in zip(
            entry.chunk_indices,
            chunk_texts,
            entry.chunk_ranges,
        ):
            blocks.append(
                "\n".join(
                    [
                        str(chunk_index),
                        f"{format_timestamp(start_ms)} --> {format_timestamp(end_ms)}",
                        chunk_text,
                    ]
                )
            )

    return "\n\n".join(blocks) + ("\n" if blocks else "")


def build_validation_timestamp_report(entries: list[ContinuousEntry]) -> str:
    blocks: list[str] = []

    for entry in entries:
        if not entry.issue_text:
            continue

        chunk_index_text = " | ".join(str(item) for item in entry.chunk_indices)
        chunk_range_text = " | ".join(
            f"{format_timestamp(start_ms)} --> {format_timestamp(end_ms)}"
            for start_ms, end_ms in entry.chunk_ranges
        )

        block_lines = [
            f"{entry.index}. [{entry.original_start} - {entry.original_end}] {entry.original_text}",
            f"连续字幕序号: {chunk_index_text}",
            f"连续时间: {chunk_range_text}",
            f"标记后: {entry.marked_text}",
            f"问题: {entry.issue_text}",
        ]
        if entry.suggestion_text:
            block_lines.append(f"建议: {entry.suggestion_text}")

        blocks.append("\n".join(block_lines))

    return "\n\n".join(blocks) + ("\n" if blocks else "")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export manually finalized subtitles to continuous SRT outputs and validation timestamp mappings."
    )
    parser.add_argument("--input", required=True, help="Path to the manual-final txt file.")
    parser.add_argument("--validation-input", required=True, help="Path to the manual-validation txt file.")
    parser.add_argument("--output", required=True, help="Path to the plain continuous SRT file.")
    parser.add_argument(
        "--marked-output",
        required=True,
        help="Path to the marked continuous SRT file that keeps validation markers.",
    )
    parser.add_argument(
        "--validation-timestamp-output",
        required=True,
        help="Path to the continuous timestamp mapping file for validation issues.",
    )
    parser.add_argument(
        "--max-chars-per-caption",
        type=int,
        default=20,
        help="Maximum number of characters per continuous SRT caption block.",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    validation_input_path = Path(args.validation_input)
    output_path = Path(args.output)
    marked_output_path = Path(args.marked_output)
    validation_timestamp_output_path = Path(args.validation_timestamp_output)

    final_entries = parse_final_entries(input_path)
    validation_map = parse_validation_entries(validation_input_path)
    continuous_entries = build_continuous_entries(
        final_entries=final_entries,
        validation_map=validation_map,
        max_chars_per_caption=args.max_chars_per_caption,
    )

    plain_srt = build_srt(continuous_entries, use_marked_text=False)
    marked_srt = build_srt(continuous_entries, use_marked_text=True)
    validation_timestamp_report = build_validation_timestamp_report(continuous_entries)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    marked_output_path.parent.mkdir(parents=True, exist_ok=True)
    validation_timestamp_output_path.parent.mkdir(parents=True, exist_ok=True)

    output_path.write_text(plain_srt, encoding="utf-8")
    marked_output_path.write_text(marked_srt, encoding="utf-8")
    validation_timestamp_output_path.write_text(validation_timestamp_report, encoding="utf-8")


if __name__ == "__main__":
    main()
