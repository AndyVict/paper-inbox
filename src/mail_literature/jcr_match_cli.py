"""现有本地 JCR 文件匹配入口；显式 --fetch 才补查公开期刊标识。"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys

from .jcr.matching import JournalIndex
from .jcr.storage import JcrError, snapshot_path
from .jcr_enrichment import enrich_journals, write_match_report
from .journal_metadata import CachedJournals
from .preview import OutboxError, write_snapshot_previews
from .state import StateError, StateStore
from .sync_cli import _run_lock

DEFAULT_SNAPSHOT = 'local-jcr'


def _backup_once(database, data):
    """首次写入期刊字段前备份；标记成功后重复运行不重复备份。"""
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as source:
        if source.execute("SELECT 1 FROM metadata WHERE key='journal-matching-v1:backup' ").fetchone():
            return None
        directory = data / 'backups'
        directory.mkdir(parents=True, exist_ok=True)
        name = 'pre-jcr-matching-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.sqlite3'
        path = directory / name
        with closing(sqlite3.connect(path)) as destination:
            source.backup(destination)
            if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise StateError('journal_backup_failed')
    return path


def main(argv=None, *, project_root=None):
    root = (project_root or Path.cwd()).resolve()
    parser = argparse.ArgumentParser(description='本地 JCR 精确匹配与预览；--fetch 查询公开 ISSN，不读取 .env、不发送')
    parser.add_argument('--db', type=Path, default=root / 'data/state.sqlite3')
    parser.add_argument('--snapshot', default=DEFAULT_SNAPSHOT)
    parser.add_argument('--outbox-dir', type=Path, default=root / 'data/outbox')
    parser.add_argument('--report-dir', type=Path, default=root / 'data/jcr-matching')
    parser.add_argument('--fetch', action='store_true')
    parser.add_argument('--max-articles', type=int, default=100)
    parser.add_argument('--max-requests', type=int, default=200)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    data = (root / 'data').resolve()
    database, outbox, reports = args.db.resolve(), args.outbox_dir.resolve(), args.report_dir.resolve()
    try:
        snapshot = snapshot_path(root, args.snapshot)
        protected = [(data / name).resolve() for name in ('samples', 'jcr', 'backups', 'private')]
        if (not database.is_file() or database.suffix not in {'.sqlite3', '.sqlite', '.db'} or
                not 1 <= args.max_articles <= 1000 or not 2 <= args.max_requests <= 1000 or
                any(path == data or not path.is_relative_to(data) or
                    any(path.is_relative_to(folder) for folder in protected) for path in (database, outbox, reports)) or
                any(left == right or left.is_relative_to(right) or right.is_relative_to(left)
                    for left, right in ((database, outbox), (database, reports), (outbox, reports))) or
                any(path.is_file() for path in (outbox, reports))):
            print('参数无效：invalid_journal_matching_paths_or_limits', file=sys.stderr)
            return 2
        # 先验证来源文件，坏快照不能触发数据库备份、缓存或文献更新。
        index = JournalIndex(snapshot)
        with _run_lock(database):
            backup = _backup_once(database, data)
            with StateStore(database) as store:
                if backup:
                    with store.transaction():
                        store.connection.execute('INSERT INTO metadata VALUES (?,?)',
                                                  ('journal-matching-v1:backup', backup.name))
                    print('首次期刊匹配前备份已保存到 data/backups/。')
                cache = CachedJournals(store, online=args.fetch, max_requests=args.max_requests)
                print('运行模式：公开期刊标识查询、缓存及本地 JCR 匹配。' if args.fetch else
                      '运行模式：只用已有期刊标识和缓存做本地 JCR 匹配，不联网。')

                def progress(stats):
                    if stats.examined % 10 == 0:
                        print(f'期刊标识进度：检查 {stats.examined}，补全 {stats.enriched}，失败 {stats.failed}。', flush=True)

                stats = enrich_journals(store, cache, index, max_articles=args.max_articles, progress=progress)
                articles = store.articles()
                summary = write_match_report(articles, index, reports)
                previews = write_snapshot_previews(articles, outbox)
                print(f"累计 {summary['articles']} 篇，公开期刊标识 {summary['with_public_issns']} 篇；状态 {summary['statuses']}。")
                print(f'HTTP 请求 {cache.client.requests}，缓存命中 {cache.hits}；失败 {stats.failed}，暂缓 {stats.deferred}。')
                print(f'预览新增 {previews.created}，更新 {previews.updated}，未变 {previews.unchanged}；报告已保存。')
                print('JCR 文件来源/版本核验状态保留；不读取邮箱/微信/翻译配置，不发送。')
        return 3 if stats.failed else 0
    except (JcrError, StateError, OutboxError) as exc:
        print(f'期刊匹配失败：{exc}；已提交进度保留。', file=sys.stderr)
        return 4
    except (OSError, UnicodeError, sqlite3.Error):
        print('期刊匹配失败：journal_matching_io_failed；已提交进度保留。', file=sys.stderr)
        return 4


if __name__ == '__main__':
    raise SystemExit(main())
