import argparse
import re
from pathlib import Path


LINE_RE = re.compile(
    r"^(?P<prefix>\d+\.\s+\[\d{2}:\d{2}:\d{2},\d{3}\s-\s\d{2}:\d{2}:\d{2},\d{3}\]\s+)(?P<text>.*)$"
)
DELETE_MARK = "×"

NON_COURSE_KEYWORDS = [
    "能看清",
    "能看到",
    "听得到",
    "最后一节课",
    "四位老师都在讲",
    "尽量快一点",
    "不给大家讲了",
    "没有成功案例",
    "列了一下",
    "我能想到的",
    "这能看清了",
    "这个比较熟",
]


def read_lines(path: Path) -> list[str]:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding).splitlines()
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("unknown", b"", 0, 1, f"Unable to decode {path}")


def should_mark(text: str) -> bool:
    if any(keyword in text for keyword in NON_COURSE_KEYWORDS):
        return True
    if len(text.strip()) <= 6:
        return True
    if text.count("我") >= 3 and len(text) < 24:
        return True
    if "成功案例" in text or "不给大家讲" in text:
        return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mark likely non-course subtitle lines for manual review."
    )
    parser.add_argument("--input", required=True, help="Path to the cleaned txt file.")
    parser.add_argument("--output", required=True, help="Path to the marked review file.")
    args = parser.parse_args()

    source = Path(args.input)
    target = Path(args.output)
    output_lines: list[str] = []

    for raw_line in read_lines(source):
        line = raw_line.rstrip("\n")
        if not line.strip():
            output_lines.append(line)
            continue

        match = LINE_RE.match(line)
        if not match:
            output_lines.append(line)
            continue

        prefix = match.group("prefix")
        text = match.group("text")

        if text.startswith(DELETE_MARK):
            output_lines.append(line)
            continue

        if should_mark(text):
            output_lines.append(f"{DELETE_MARK}{prefix}{text}")
        else:
            output_lines.append(line)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(output_lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
