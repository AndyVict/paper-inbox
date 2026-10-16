"""Query journal names, ISSNs and metrics entirely offline."""
import argparse
import json
from pathlib import Path
import sys

from .jcr.bundled import DEFAULT_SNAPSHOT, resolve_snapshot
from .jcr.matching import JournalIndex, name_key
from .jcr.storage import JcrError
from .journal_fields import normalize_issn


def find_journals(index, query=None, issn=None):
    if issn:
        ids = index.by_issn.get(issn, set())
    else:
        key = name_key(query)
        ids = {identifier for name, identifiers in index.by_name.items() if key in name
               for identifier in identifiers}
    return sorted(ids, key=lambda identifier: name_key(index.journals[identifier]["journal_title"]))


def main(argv=None, *, project_root=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    root = (project_root or Path.cwd()).resolve()
    parser = argparse.ArgumentParser(description="离线查询期刊名称、ISSN、JIF/JCI 与全部学科分区；无需账号或联网")
    parser.add_argument("query", nargs="?", help="期刊英文名称或缩写，可使用部分名称")
    parser.add_argument("--issn", help="精确查询 ISSN 或 eISSN")
    parser.add_argument("--info", action="store_true", help="只显示快照信息与覆盖数量")
    parser.add_argument("--snapshot", default=DEFAULT_SNAPSHOT, help="默认使用随包期刊库；指定名称可查询自己的本地快照")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    issn = normalize_issn(args.issn) if args.issn is not None else None
    if (not 1 <= args.limit <= 100 or
            (args.info and (args.query is not None or args.issn is not None)) or
            (not args.info and (bool(args.query and args.query.strip()) == (args.issn is not None))) or
            (args.issn is not None and issn is None)):
        print("参数无效：invalid_journal_query", file=sys.stderr)
        return 2
    try:
        index = JournalIndex(resolve_snapshot(root, args.snapshot))
        ids = [] if args.info else find_journals(index, args.query, issn)
        results = [dict(index.journals[identifier], categories=index.categories[identifier])
                   for identifier in ids[:args.limit]]
        payload = {"snapshot_id": index.manifest["snapshot_id"], "metric_year": index.manifest["metric_year"],
                   "release_year": index.manifest["release_year"], "revision": index.manifest["revision"],
                   "journals": len(index.journals), "category_records": sum(map(len, index.categories.values())),
                   "subjects": len({category["category_name"] for group in index.categories.values() for category in group}),
                   "invalid_source_identifiers": index.invalid_identifiers,
                   "full_complete": index.full_complete, "source_verified": index.source_verified,
                   "version_confirmed": index.manifest.get("version_confirmed") is True,
                   "total_matches": len(ids), "results": results}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"本地快照：{payload['snapshot_id']}；{payload['metric_year']} 年指标 / {payload['release_year']} 发布年")
            print(f"覆盖 {payload['journals']} 刊 / {payload['category_records']} 条分类 / {payload['subjects']} 学科")
            if not payload["full_complete"]:
                print("来源、版本和官方全集尚未完成独立核验；缺失值按原样保留。")
            if not args.info:
                print(f"找到 {len(ids)} 刊，显示前 {len(results)} 刊。")
            for journal in results:
                print(f"\n{journal['journal_title']}\nISSN：{journal['issn']}；eISSN：{journal['eissn']}")
                print(f"JIF：{journal['jif']}；JCI：{journal['jci']}")
                for category in journal["categories"]:
                    print(f"  {category['category_name']}（{category['index_or_edition']}）："
                          f"JIF {category['jif_quartile']}；JCI {category['jci_quartile']}")
        return 0
    except (JcrError, OSError, ValueError, KeyError, TypeError):
        print("查询失败：journal_query_failed", file=sys.stderr)
        return 4
