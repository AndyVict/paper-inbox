from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import sys

from .imap_reader import ImapError, ReadOnlyImap, load_config
from .preview import OutboxError, write_snapshot_previews
from .state import StateError, StateStore
from .sync import sync_mail


@contextmanager
def _run_lock(database: Path):
    database.parent.mkdir(parents=True, exist_ok=True)
    path = database.with_name(database.name + ".sync.lock")
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise StateError("sync_locked") from None
    try:
        os.close(descriptor)
        yield
    finally:
        path.unlink()  # Only this invocation's exact lock; no batch cleanup.


def main(argv: list[str] | None = None, *, project_root: Path | None = None) -> int:
    root = (project_root or Path.cwd()).resolve()
    parser = argparse.ArgumentParser(description="SQLite 接续现有索引并生成预览；只有 --fetch 才读取配置和联网，始终不发送微信")
    parser.add_argument("--db", type=Path, default=root / "data" / "state.sqlite3")
    parser.add_argument("--outbox-dir", type=Path, default=root / "data" / "outbox")
    parser.add_argument("--fetch", action="store_true", help="显式加载 IMAP 配置并只读收信")
    parser.add_argument("--check-config", action="store_true", help="仅检查本地 IMAP 配置格式，不连接服务")
    parser.add_argument("--env-file", type=Path, default=root / ".env")
    parser.add_argument("--max-mails", type=int, default=100, help="单次最多检查邮件数，含失败重试；默认 100")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    data = (root / "data").resolve()
    samples = (data / "samples").resolve()
    database, outbox = args.db.resolve(), args.outbox_dir.resolve()
    if (not database.is_relative_to(data) or not outbox.is_relative_to(data) or outbox == data or
        database.is_relative_to(samples) or outbox.is_relative_to(samples) or
        database.is_relative_to(outbox) or database == data or database.suffix not in {".sqlite3", ".sqlite", ".db"} or
        not 1 <= args.max_mails <= 1000):
        print("参数无效：数据库与预览须位于独立的 data/ 路径，不能写入 samples/；max-mails 为 1–1000。", file=sys.stderr)
        return 2
    config = None
    if args.fetch or args.check_config:
        env = args.env_file.resolve()
        if env.parent != root or not (env.name == ".env" or env.name.startswith("config.local.")):
            print("配置必须是项目根目录未跟踪的 .env 或 config.local.*。", file=sys.stderr)
            return 2
        try:
            config = load_config(env)
        except ImapError as exc:
            print(f"配置检查失败：{exc}", file=sys.stderr)
            return 2
    if args.check_config:
        print("IMAP 配置格式通过；未联网、未显示账号或凭据。")
        return 0
    print("运行模式：只读 IMAP + 本地预览；不发送微信。" if args.fetch else
          "运行模式：本地 SQLite 迁移及预览；不读取配置、不联网、不发送。")
    failed = 0
    try:
        with _run_lock(database), StateStore(database) as store:
            imported = store.import_legacy(outbox / "index.json")
            if imported is not None:
                print(f"首次索引导入：累计 {imported.total} 篇；合并 {imported.merged} 条。")
            if config is not None:
                with ReadOnlyImap(config) as reader:
                    stats = sync_mail(store, reader, max_mails=args.max_mails)
                failed = stats.failed
                print(f"邮件检查 {stats.examined}，解析 {stats.parsed}，忽略 {stats.ignored}，失败 {stats.failed}，已不存在 {stats.vanished}；本批候选 {stats.candidates} 篇，待检查 {stats.remaining} 封。")
            previews = write_snapshot_previews(store.articles(), outbox)
            print(f"累计 {previews.total} 篇；预览新增 {previews.created}，更新 {previews.updated}，未变 {previews.unchanged}，归档 {previews.archived}；未发送。")
    except ImapError as exc:
        print(f"只读收信失败：{exc}；已提交进度保留，可重跑恢复。", file=sys.stderr)
        return 5
    except (OutboxError, StateError) as exc:
        print(f"状态/预览失败：{exc}", file=sys.stderr)
        return 4
    except (OSError, UnicodeError, sqlite3.Error):
        print("状态/预览失败：state_io_failed", file=sys.stderr)
        return 4
    return 3 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
