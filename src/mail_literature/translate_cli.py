from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys

from .niutrans import NiuTransClient, SAFE_ERRORS, TranslationError, load_config
from .preview import OutboxError, write_snapshot_previews
from .state import StateError, StateStore
from .sync_cli import _run_lock
from .translation import CachedTranslations, translate_articles


def _backup_for_upgrade(database, data):
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source:
        if source.execute("PRAGMA user_version").fetchone()[0] not in {1, 2}:
            return False
        directory = data / "backups"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        with closing(sqlite3.connect(directory / f"pre-translation-{stamp}.sqlite3")) as destination:
            source.backup(destination)
            if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StateError("translation_backup_failed")
    return True


def main(argv=None, *, project_root=None):
    root = (project_root or Path.cwd()).resolve()
    parser = argparse.ArgumentParser(description="小牛翻译与缓存；显式 --fetch 才读取密钥及联网")
    parser.add_argument("--db", type=Path, default=root / "data" / "state.sqlite3")
    parser.add_argument("--outbox-dir", type=Path, default=root / "data" / "outbox")
    parser.add_argument("--env-file", type=Path, default=root / ".env")
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--check-config", action="store_true", help="只检查本地配置格式，不联网")
    parser.add_argument("--max-articles", type=int, default=100)
    parser.add_argument("--max-characters", type=int, default=20_000, help="本轮最多提交的原文字符数，不是费用承诺")
    parser.add_argument("--max-requests", type=int, default=200)
    parser.add_argument("--refresh", action="store_true", help="结合 --fetch，重新按当前缓存版本处理已有译文")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    database, outbox, env = args.db.resolve(), args.outbox_dir.resolve(), args.env_file.resolve()
    data, samples = (root / "data").resolve(), (root / "data" / "samples").resolve()
    if (not 1 <= args.max_articles <= 1000 or not 1 <= args.max_characters <= 1_000_000 or
            not 1 <= args.max_requests <= 1000 or (args.refresh and not args.fetch) or
            not database.is_relative_to(data) or not outbox.is_relative_to(data) or database == data or outbox == data or
            database.is_relative_to(samples) or outbox.is_relative_to(samples) or database.is_relative_to(outbox) or
            database.suffix not in {".sqlite3", ".sqlite", ".db"}):
        print("参数无效：invalid_translation_paths_or_limits", file=sys.stderr)
        return 2
    config = None
    if args.fetch or args.check_config:
        if env.parent != root or not (env.name == ".env" or env.name.startswith("config.local.")):
            print("配置须为项目根目录 .env 或 config.local.*：invalid_translation_config_path", file=sys.stderr)
            return 2
        try:
            config = load_config(env)
        except TranslationError as exc:
            print(f"配置检查失败：{exc}", file=sys.stderr)
            return 2
    if args.check_config:
        print("小牛翻译配置格式检查通过；未联网，未显示密钥。")
        return 0
    if not database.is_file():
        print("请先运行 scripts/sync_mail.py：missing_state_database", file=sys.stderr)
        return 2
    print("运行模式：小牛英文→中文翻译 + 缓存 + 本地预览。" if args.fetch else "运行模式：只使用本地译文缓存及预览，不读取配置、不联网。")
    print("终端仅显示统计；不发送微信。")
    try:
        with _run_lock(database):
            if _backup_for_upgrade(database, data):
                print("升级前 SQLite 备份已保存到 data/backups/。")
            with StateStore(database) as store:
                client = NiuTransClient(config) if config else None
                kwargs = {"options": config.options} if config else {}
                cache = CachedTranslations(store, client=client, online=args.fetch,
                                           max_characters=args.max_characters, max_requests=args.max_requests, **kwargs)

                def progress(stats):
                    if (stats.titles + stats.abstracts + stats.failed) % 10 == 0 or stats.failed:
                        print(f"翻译进度：检查 {stats.examined} 篇，标题 {stats.titles}，摘要 {stats.abstracts}，失败 {stats.failed}。", flush=True)

                stats = translate_articles(store, cache, max_articles=args.max_articles, refresh=args.refresh, progress=progress)
                print(f"本轮检查 {stats.examined} 篇；标题翻译 {stats.titles}，摘要翻译 {stats.abstracts}，失败字段 {stats.failed}，暂缓字段 {stats.deferred}。")
                print(f"累计中文标题 {stats.title_total}，中文摘要 {stats.abstract_total}；待译标题 {stats.remaining_titles}，待译摘要 {stats.remaining_abstracts}，原文摘要待补 {stats.missing_abstracts}。")
                print(f"HTTP 请求 {client.requests if client else 0}，提交原文字符 {client.characters if client else 0}，缓存命中 {cache.hits}。")
                if cache.deferred_reason in SAFE_ERRORS | {"translation_cache_miss", "translation_budget_reached"}:
                    print("暂缓原因：" + cache.deferred_reason + "；已提交进度保留，可接续。")
                if client and client.stopped in SAFE_ERRORS:
                    print("接口停止原因：" + client.stopped + "；失败缓存 1 小时，不自动重试。")
                previews = write_snapshot_previews(store.articles(), outbox)
                print(f"累计 {previews.total} 篇；预览新增 {previews.created}，更新 {previews.updated}，未变 {previews.unchanged}；未发送。")
    except (OutboxError, StateError, TranslationError) as exc:
        print(f"翻译/状态失败：{exc}；已提交进度保留。", file=sys.stderr)
        return 4
    except (OSError, UnicodeError, sqlite3.Error):
        print("翻译/状态失败：translation_io_failed；已提交进度保留。", file=sys.stderr)
        return 4
    return 3 if stats.failed or (client and client.stopped) else 0


if __name__ == "__main__":
    raise SystemExit(main())
