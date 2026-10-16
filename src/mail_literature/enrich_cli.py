from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3
import sys

from .enrichment import CachedAbstracts, enrich_abstracts
from .preview import OutboxError, write_snapshot_previews
from .state import StateError, StateStore
from .sync_cli import _run_lock


def main(argv=None, *, project_root=None):
    root = (project_root or Path.cwd()).resolve()
    parser = argparse.ArgumentParser(description="公开摘要补全与缓存；显式 --fetch 才联网，始终不发送微信")
    parser.add_argument("--db", type=Path, default=root / "data" / "state.sqlite3")
    parser.add_argument("--outbox-dir", type=Path, default=root / "data" / "outbox")
    parser.add_argument("--fetch", action="store_true", help="查询官方公开摘要 API；不读取 .env")
    parser.add_argument("--max-articles", type=int, default=100)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    data, samples = (root / "data").resolve(), (root / "data" / "samples").resolve()
    database, outbox = args.db.resolve(), args.outbox_dir.resolve()
    if (not database.is_relative_to(data) or not outbox.is_relative_to(data) or database == data or outbox == data or
        database.is_relative_to(samples) or outbox.is_relative_to(samples) or database.is_relative_to(outbox) or
        database.suffix not in {".sqlite3", ".sqlite", ".db"} or not 1 <= args.max_articles <= 1000):
        print("参数无效：invalid_enrichment_paths_or_limit", file=sys.stderr)
        return 2
    if not database.is_file():
        print("请先运行 scripts/sync_mail.py 建立状态库：missing_state_database", file=sys.stderr)
        return 2
    print("运行模式：公开摘要查询 + 缓存 + 本地预览。" if args.fetch else "运行模式：只使用本地摘要缓存及预览，不联网。")
    print("不读取邮箱/微信配置，不发送微信；终端仅显示统计。")
    try:
        with _run_lock(database), StateStore(database) as store:
            cache = CachedAbstracts(store, online=args.fetch)

            def progress(stats):
                print(f"摘要进度：检查 {stats.examined}，补全 {stats.enriched}，无摘要 {stats.missing}，失败 {stats.failed}。", flush=True)

            stats = enrich_abstracts(store, cache, max_articles=args.max_articles, progress=progress)
            print(f"摘要检查 {stats.examined}，补全 {stats.enriched}，无公开摘要 {stats.missing}，失败 {stats.failed}，无标识符 {stats.no_identifier}，离线待查询 {stats.deferred}；此前已有 {stats.existing}，当前待补 {stats.remaining}。")
            print(f"HTTP 请求 {cache.client.requests}，缓存命中 {cache.hits}。")
            codes = store.connection.execute("""SELECT error_code,COUNT(*) FROM abstract_enrichment
                WHERE status IN ('failed','missing','no_identifier') GROUP BY error_code ORDER BY error_code""").fetchall()
            for code, count in codes:
                # Never emit arbitrary database content as an error message.
                if code in {"missing_public_identifier", "no_public_abstract", "abstract_identity_mismatch",
                            "abstract_rate_limited", "abstract_access_denied", "abstract_http_failed",
                            "abstract_network_failed", "abstract_response_too_large", "invalid_abstract_response",
                            "abstract_redirect_rejected"}:
                    print(f"待补原因：{code}，{count} 篇。")
            previews = write_snapshot_previews(store.articles(), outbox)
            print(f"累计 {previews.total} 篇；预览新增 {previews.created}，更新 {previews.updated}，未变 {previews.unchanged}；未发送。")
    except (OutboxError, StateError) as exc:
        print(f"状态/预览失败：{exc}；已提交进度保留。", file=sys.stderr)
        return 4
    except (OSError, UnicodeError, sqlite3.Error):
        print("状态/预览失败：enrichment_io_failed；已提交进度保留。", file=sys.stderr)
        return 4
    return 3 if stats.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
