"""文献发送入口：默认只读库并生成发送预览，--send 才加载凭据及推送。"""
import argparse
from collections import Counter
from pathlib import Path
import sqlite3
import sys

from .delivery import confirm_received, prepare_delivery, reconcile, send_articles, write_send_previews
from .preview import ID_PATTERN, OutboxError
from .state import StateError, StateStore
from .sync_cli import _run_lock
from .wxpusher import PushError, WxPusherClient, load_config, recipient_key


def main(argv=None, *, project_root=None):
    root = (project_root or Path.cwd()).resolve()
    parser = argparse.ArgumentParser(description="文献发送预览与持久化 WxPusher；--send 才真实发送")
    parser.add_argument("--db", type=Path, default=root / "data/state.sqlite3")
    parser.add_argument("--preview-dir", type=Path, default=root / "data/send-preview")
    parser.add_argument("--env-file", type=Path, default=root / ".env")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--send", action="store_true")
    action.add_argument("--check-config", action="store_true")
    action.add_argument("--status", action="store_true")
    action.add_argument("--query-status", action="store_true", help="只查询已保存的发送编号，不重发")
    action.add_argument("--confirm-received", type=int, metavar="ATTEMPT_ID", help="仅在 WxPusher App 实际收到后记录人工确认")
    parser.add_argument("--max-messages", type=int, default=20)
    parser.add_argument("--daily-limit", type=int, default=200, help="每接收者滚动 24 小时本地发送上限；App 主接收")
    parser.add_argument("--max-abstract", type=int, default=1000)
    parser.add_argument("--language", choices=("en", "zh"), default="zh",
                        help="en 使用英文原文；zh 要求已有有效中文译文（默认）")
    parser.add_argument("--max-queries", type=int, default=10)
    parser.add_argument("--article-id", action="append", default=[])
    parser.add_argument("--retry-failed", action="store_true", help="显式重试已知业务拒绝；不重试 unknown")
    parser.add_argument("--require-receipt", action="store_true", help="发送前要求当前接收者已有人工送达确认")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    data = (root / "data").resolve()
    database, previews, env = args.db.resolve(), args.preview_dir.resolve(), args.env_file.resolve()
    protected = [(data / name).resolve() for name in ("samples", "jcr", "private", "backups", "outbox")]
    invalid = (not database.is_relative_to(data) or not previews.is_relative_to(data) or
               database == data or previews == data or database.is_relative_to(previews) or previews.is_relative_to(database) or
               any(database.is_relative_to(p) or previews.is_relative_to(p) for p in protected) or
               database.suffix not in {".sqlite3", ".sqlite", ".db"} or
               not 1 <= args.max_messages <= 1000 or not 1 <= args.daily_limit <= 1000 or
               not 100 <= args.max_abstract <= 10000 or not 1 <= args.max_queries <= 1000 or
               any(not ID_PATTERN.fullmatch(key) for key in args.article_id) or
               ((args.retry_failed or args.require_receipt) and not args.send))
    if invalid:
        print("参数无效：invalid_delivery_paths_or_limits", file=sys.stderr)
        return 2
    config = None
    uses_config = args.send or args.check_config or args.query_status or args.confirm_received is not None
    if uses_config:
        if env.parent != root or not (env.name == ".env" or env.name.startswith("config.local.")):
            print("配置路径无效：invalid_wxpusher_config_path", file=sys.stderr)
            return 2
        try:
            config = load_config(env)
        except PushError as exc:
            print(f"配置检查失败：{exc}", file=sys.stderr)
            return 2
    if args.check_config:
        print(f"WxPusher 配置格式通过；接收者 {len(config.uids)}；未联网、未显示凭据。")
        return 0
    if not database.is_file():
        print("状态库不存在：missing_state_database", file=sys.stderr)
        return 2
    writes = args.send or args.query_status or args.confirm_received is not None
    try:
        with _run_lock(database), StateStore(database, readonly=not writes) as store:
            articles = store.articles()
            if args.article_id:
                selected = set(args.article_id)
                articles = [a for a in articles if a.article_id in selected]
                if len(articles) != len(selected):
                    raise StateError("unknown_or_retired_article_id")
            if args.status:
                counts = dict(Counter(row[0] for row in store.connection.execute("SELECT status FROM delivery_attempts")))
                print(f"活动文献 {len(articles)}；发送尝试 {sum(counts.values())}；" +
                      "，".join(f"{status} {counts.get(status, 0)}" for status in ("accepted", "confirmed", "failed", "unknown", "inflight")))
                for row in store.connection.execute("SELECT attempt_id,article_id,status FROM delivery_attempts ORDER BY attempt_id DESC LIMIT 10"):
                    print(f"本地尝试 {row['attempt_id']}；文献 {row['article_id']}；状态 {row['status']}。")
                return 0
            if writes:
                prepare_delivery(store, data)
            if args.confirm_received is not None:
                confirm_received(store, args.confirm_received, {recipient_key(uid) for uid in config.uids})
                print(f"已记录 WxPusher App 实际收到；本地发送尝试编号 {args.confirm_received}；未联网、未重发。")
                return 0
            if args.query_status:
                client = WxPusherClient(config)
                queried, unavailable = reconcile(store, client, max_queries=args.max_queries)
                print(f"状态查询 {client.requests} 次；已记录 {queried}，不可用 {unavailable}；未重发，未推断人工送达。")
                return 3 if unavailable else 0
            written, oversized = write_send_previews(articles, previews, args.max_abstract, language=args.language)
            print(f"发送预览 {written} 篇；正文超限 {oversized}；中文摘要上限 {args.max_abstract} 字符。", flush=True)
            if not args.send:
                print("未读取配置、未联网、未修改业务库、未发送消息。")
                return 3 if oversized else 0
            client = WxPusherClient(config)
            stats = send_articles(store, client, articles, max_messages=args.max_messages,
                                  daily_limit=args.daily_limit, max_abstract=args.max_abstract,
                                  retry_failed=args.retry_failed, require_receipt=args.require_receipt,
                                  language=args.language)
            print(f"本轮尝试 {stats.attempted}，平台接收 {stats.accepted}，业务拒绝 {stats.failed}，结果不明 {stats.unknown}；" +
                  f"跳过 {stats.skipped}（待核对 {stats.blocked}），暂缓 {stats.deferred}；HTTP 请求 {client.requests}。")
            if stats.last_attempt_id is not None:
                print(f"最近本地发送尝试编号：{stats.last_attempt_id}；平台接收不等于 App 已收到。")
            return 5 if stats.unknown else 3 if stats.failed or stats.blocked or oversized else 0
    except (PushError, StateError, OutboxError) as exc:
        print(f"发送/状态失败：{exc}；已提交进度保留。", file=sys.stderr)
        return 4
    except (OSError, UnicodeError, sqlite3.Error):
        print("发送/状态失败：delivery_io_failed；未完成的 inflight 不会自动重发。", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
