from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .dedup import deduplicate
from .parsers import parse_eml
from .preview import OutboxError, write_previews

ROOT = Path.cwd()


def main(argv: list[str] | None = None, *, project_root: Path | None = None) -> int:
    for stream in [sys.stdout, sys.stderr]:
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="离线解析 X-MOL/Stork .eml 并生成逐篇预览；不加载 .env，不联网，不发送")
    root = project_root or ROOT
    parser.add_argument("--input-dir", type=Path, default=root / "data" / "samples")
    parser.add_argument("--outbox-dir", type=Path, default=root / "data" / "outbox")
    args = parser.parse_args(argv)
    input_dir, outbox = args.input_dir.resolve(), args.outbox_dir.resolve()
    data_dir = (root / "data").resolve()
    if not outbox.is_relative_to(data_dir) or outbox == data_dir or outbox == input_dir or outbox.is_relative_to(input_dir):
        print("输出目录必须位于项目 data/ 下，且与邮件输入目录分离。", file=sys.stderr)
        return 2
    if not input_dir.is_dir():
        print("邮件输入目录不存在。", file=sys.stderr)
        return 2
    samples = sorted(path for path in input_dir.iterdir() if path.is_file() and path.suffix.casefold() == ".eml")
    if not samples:
        print("输入目录没有 .eml 文件。", file=sys.stderr)
        return 2
    parsed = []
    failed = 0
    print("离线模式：不读取 .env，不访问网络，不发送消息。")
    for number, sample in enumerate(samples, 1):
        try:
            result = parse_eml(sample.read_bytes())
        except OSError:
            failed += 1
            print(f"邮件 {number}：读取失败（mail_read_failed）。", file=sys.stderr)
            continue
        failed += not bool(result.articles)
        parsed.extend(result.articles)
        print(f"邮件 {number}：来源 {result.source or '未识别'}；文献 {len(result.articles)} 篇；邮件警告 {len(result.warnings)} 项。")
    if not parsed:
        print("没有可预览的文献；未更新输出。", file=sys.stderr)
        return 3
    batch = deduplicate(parsed)
    try:
        stats = write_previews(batch, outbox)
    except OutboxError as exc:
        print(f"预览写入失败：{exc}", file=sys.stderr)
        return 4
    except (OSError, UnicodeError):
        print("预览写入失败：outbox_io_failed", file=sys.stderr)
        return 4
    print(f"本批候选 {len(parsed)} 篇，去重后 {len(batch)} 篇；累计 {stats.total} 篇。")
    print(f"预览新增 {stats.created}，更新 {stats.updated}，未变 {stats.unchanged}，归档 {stats.archived}；未发送。")
    return 3 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
