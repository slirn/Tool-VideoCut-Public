import argparse
import re
from pathlib import Path


LINE_RE = re.compile(
    r"^(?P<marked>[×脳])?(?P<index>\d+)\.\s+\[(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s-\s(?P<end>\d{2}:\d{2}:\d{2},\d{3})\]\s+(?P<text>.*)$"
)


def read_lines(path: Path) -> list[str]:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding).splitlines()
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("unknown", b"", 0, 1, f"Unable to decode {path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Split a manually reviewed marked subtitle file into kept and removed outputs."
    )
    parser.add_argument("--input", required=True, help="Path to the manually reviewed marked file.")
    parser.add_argument("--output", required=True, help="Path to the finalized kept-content file.")
    parser.add_argument("--removed-output", required=True, help="Path to the extracted removed-content file.")
    args = parser.parse_args()

    source = Path(args.input)
    kept_target = Path(args.output)
    removed_target = Path(args.removed_output)

    kept_lines: list[str] = []
    removed_lines: list[str] = []
    next_index = 1

    for raw_line in read_lines(source):
        line = raw_line.strip()
        if not line:
            continue

        match = LINE_RE.match(line)
        if not match:
            continue

        marked = match.group("marked")
        original_index = match.group("index")
        start = match.group("start")
        end = match.group("end")
        text = match.group("text")

        normalized_line = f"{original_index}. [{start} - {end}] {text}"

        if marked:
            removed_lines.append(normalized_line)
            continue

        kept_lines.append(f"{next_index}. [{start} - {end}] {text}")
        next_index += 1

    kept_target.parent.mkdir(parents=True, exist_ok=True)
    removed_target.parent.mkdir(parents=True, exist_ok=True)
    kept_target.write_text("\n".join(kept_lines) + ("\n" if kept_lines else ""), encoding="utf-8")
    removed_target.write_text(
        "\n".join(removed_lines) + ("\n" if removed_lines else ""),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
