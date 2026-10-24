import argparse
import os
import subprocess
import sys
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def preferred_python(root: Path) -> str:
    venv_python = root / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def run_command(command: list[str], cwd: Path) -> None:
    print("Running:", " ".join(command))
    completed = subprocess.run(command, cwd=str(cwd))
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def build_paths(video_path: Path, output_dir: Path) -> dict[str, Path]:
    stem = video_path.stem
    return {
        "raw_srt": output_dir / f"{stem}.raw.srt",
        "intermediate": output_dir / f"{stem}.intermediate.txt",
        "cleaned": output_dir / f"{stem}.cleaned.txt",
        "removed": output_dir / f"{stem}.removed.txt",
        "review": output_dir / f"{stem}.review.md",
        "repeat_only": output_dir / f"{stem}.repeat-only.txt",
        "cut_video": output_dir / f"{stem}_cut.mp4",
        "continuous_srt": output_dir / f"{stem}.continuous.srt",
        "continuous_repeat": output_dir / f"{stem}.continuous.repeat.txt",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="End-to-end pipeline: video -> raw subtitles -> cleaned timestamps -> cut video -> continuous subtitles."
    )
    parser.add_argument("--input", required=True, help="Input video path.")
    parser.add_argument("--output-dir", required=True, help="Output directory.")
    parser.add_argument("--model", default="paraformer", choices=["paraformer", "fun-asr-nano", "sensevoice"])
    parser.add_argument("--lang", default="zh", choices=["zh", "en"])
    parser.add_argument("--hotwords", default="", help="Hotwords for subtitle extraction.")
    parser.add_argument("--sd-switch", default="no", choices=["yes", "no"])
    parser.add_argument("--repeat-marker", default="〿", help="Marker for repeated speech issues.")
    parser.add_argument("--max-chars-per-caption", type=int, default=20)
    args = parser.parse_args()

    root = repo_root()
    output_dir = Path(args.output_dir).resolve()
    video_path = Path(args.input).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = build_paths(video_path, output_dir)

    python = preferred_python(root)

    extract_script = root / "skill" / "video-subtitle-extractor" / "scripts" / "extract_subtitle.py"
    srt_to_intermediate_script = root / "skill" / "long-video-subtitle-cleaner" / "scripts" / "srt_to_intermediate.py"
    clean_script = root / "skill" / "long-video-subtitle-cleaner" / "scripts" / "clean_intermediate_subtitles.py"
    cut_script = root / "skill" / "video-timestamp-cutter" / "scripts" / "cut_by_srt.py"
    export_srt_script = root / "skill" / "long-video-subtitle-cleaner" / "scripts" / "export_continuous_srt.py"

    run_command(
        [
            python,
            str(extract_script),
            "--input",
            str(video_path),
            "--output",
            str(paths["raw_srt"]),
            "--model",
            args.model,
            "--lang",
            args.lang,
            "--hotwords",
            args.hotwords,
            "--sd-switch",
            args.sd_switch,
        ],
        cwd=root,
    )

    run_command(
        [
            python,
            str(srt_to_intermediate_script),
            "--input",
            str(paths["raw_srt"]),
            "--output",
            str(paths["intermediate"]),
        ],
        cwd=root,
    )

    run_command(
        [
            python,
            str(clean_script),
            "--input",
            str(paths["intermediate"]),
            "--output",
            str(paths["cleaned"]),
            "--removed-output",
            str(paths["removed"]),
            "--review-output",
            str(paths["review"]),
            "--repeat-output",
            str(paths["repeat_only"]),
            "--repeat-marker",
            args.repeat_marker,
        ],
        cwd=root,
    )

    run_command(
        [
            python,
            str(cut_script),
            "--input",
            str(video_path),
            "--srt",
            str(paths["cleaned"]),
            "--output",
            str(paths["cut_video"]),
        ],
        cwd=root,
    )

    run_command(
        [
            python,
            str(export_srt_script),
            "--input",
            str(paths["cleaned"]),
            "--output",
            str(paths["continuous_srt"]),
            "--repeat-output",
            str(paths["continuous_repeat"]),
            "--repeat-marker",
            args.repeat_marker,
            "--max-chars-per-caption",
            str(args.max_chars_per_caption),
        ],
        cwd=root,
    )

    print("")
    print("Pipeline complete.")
    print(f"Cut video: {paths['cut_video']}")
    print(f"Continuous subtitles: {paths['continuous_srt']}")


if __name__ == "__main__":
    main()
