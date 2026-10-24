import argparse
import re
from pathlib import Path


SRT_BLOCK_RE = re.compile(
    r"^\s*(\d+)\s*\n"
    r"(\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2},\d{3})\s*\n"
    r"(.+?)\s*$",
    re.MULTILINE | re.DOTALL,
)


def read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("unknown", b"", 0, 1, f"Unable to decode {path}")


def normalize_text(text: str) -> str:
    parts = [line.strip() for line in text.splitlines() if line.strip()]
    return " ".join(parts)


def parse_srt(content: str) -> list[tuple[int, str, str, str]]:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = re.split(r"\n{2,}", normalized)
    entries: list[tuple[int, str, str, str]] = []

    for block in blocks:
        lines = [line for line in block.split("\n") if line.strip()]
        if len(lines) < 3:
            continue
        if not lines[0].strip().isdigit():
            continue

        index = int(lines[0].strip())
        time_line = lines[1].strip()
        match = re.match(
            r"(\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2},\d{3})",
            time_line,
        )
        if not match:
            continue

        start, end = match.groups()
        text = normalize_text("\n".join(lines[2:]))
        entries.append((index, start, end, text))

    return entries


def format_entry(index: int, start: str, end: str, text: str) -> str:
    return f"{index}. [{start} - {end}] {text}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert SRT subtitles to the intermediate single-line target format."
    )
    parser.add_argument("--input", required=True, help="Path to the source SRT file.")
    parser.add_argument("--output", required=True, help="Path to the intermediate txt file.")
    args = parser.parse_args()

    source = Path(args.input)
    target = Path(args.output)

    content = read_text(source)
    entries = parse_srt(content)
    output = "\n".join(format_entry(*entry) for entry in entries)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(output + ("\n" if output else ""), encoding="utf-8")


if __name__ == "__main__":
    main()
