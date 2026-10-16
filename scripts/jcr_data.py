"""独立 JCR CLI，默认本地；联网仅由 preflight/collect 显式触发。"""
import argparse
import csv
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from mail_literature.jcr.storage import (JcrError, initialize, locked, read_json,
                                        snapshot_path, write_json, read_csv, sanitize)
from mail_literature.jcr.report import export_tables
from mail_literature.jcr.workflow import import_web, import_mapped, validate, preflight, collect
from mail_literature.jcr.workbook import import_workbook


def key_from_env(file):
    key = os.environ.get('JCR_API_KEY', '')
    if file.exists():
        for line in file.read_text(encoding='utf-8-sig').splitlines():
            name, sep, value = line.strip().partition('=')
            if sep and name == 'JCR_API_KEY':
                key = value.strip().strip('\"\'')
    if not key:
        raise JcrError('jcr_dedicated_key_missing')
    return key


def main():
    p = argparse.ArgumentParser(description='JCR 独立快照建库；不读写邮箱状态、不发送微信。')
    p.add_argument('action', choices=('init', 'import-web', 'import-csv', 'import-xlsx', 'validate', 'preflight', 'collect', 'query'))
    p.add_argument('--snapshot', required=True)
    p.add_argument('--manifest', type=Path)
    p.add_argument('--file', type=Path)
    p.add_argument('--journal-file', type=Path)
    p.add_argument('--category-file', type=Path)
    p.add_argument('--mapping', type=Path)
    p.add_argument('--source-url', default='https://jcr.clarivate.com/jcr/browse-journals')
    p.add_argument('--reference', type=Path)
    p.add_argument('--samples', type=Path)
    p.add_argument('--config', type=Path, default=ROOT / 'config.local.jcr.json')
    p.add_argument('--env-file', type=Path, default=ROOT / 'config.local.jcr.env')
    p.add_argument('--id')
    p.add_argument('--issn')
    args = p.parse_args()
    try:
        path = snapshot_path(ROOT, args.snapshot)
        if args.action == 'init':
            if not args.manifest:
                raise JcrError('manifest_required')
            manifest = read_json(args.manifest)
            required = ('snapshot_id', 'metric_year', 'release_year', 'revision', 'source_kind', 'synthetic')
            if any(k not in manifest for k in required) or manifest['snapshot_id'] != args.snapshot:
                raise JcrError('invalid_manifest')
            initialize(path, manifest)
        manifest = read_json(path / 'snapshot.json')
        with locked(path):
            if args.reference:
                write_json(path / 'official_reference.json', sanitize(read_json(args.reference)))
            if args.samples:
                write_json(path / 'sample_checks.json', sanitize(read_json(args.samples)))
            if args.action == 'init':
                export_tables(path, manifest, [], []) if not (path / 'fields.json').exists() else None
                result = validate(path, manifest)
            elif args.action == 'import-web':
                if not args.file:
                    raise JcrError('import_file_required')
                result = import_web(path, manifest, args.file, args.source_url)
            elif args.action == 'import-csv':
                if not all((args.journal_file, args.category_file, args.mapping)):
                    raise JcrError('two_csvs_and_mapping_required')
                result = import_mapped(path, manifest, args.journal_file, args.category_file, read_json(args.mapping))
            elif args.action == 'import-xlsx':
                if not args.file:
                    raise JcrError('import_file_required')
                result, audit = import_workbook(path, manifest, args.file, args.source_url)
            elif args.action == 'validate':
                result = validate(path, manifest)
            elif args.action in ('preflight', 'collect'):
                config = read_json(args.config)
                key = key_from_env(args.env_file)
                if args.action == 'preflight':
                    result = preflight(path, manifest, config, key)
                else:
                    result, checkpoint = collect(path, manifest, config, key)
                    print(json.dumps(checkpoint, ensure_ascii=False))
            else:
                if not args.id and not args.issn:
                    raise JcrError('id_or_issn_required')
                rows = [j for j in read_csv(path / 'journals.csv') if
                        (args.id and j['journal_id'] == args.id) or
                        (args.issn and args.issn in (j['issn'], j['eissn']))]
                ids = {j['journal_id'] for j in rows}
                cs = [c for c in read_csv(path / 'journal_categories.csv') if c['journal_id'] in ids]
                print(json.dumps({'journals': rows, 'journal_categories': cs}, ensure_ascii=False, indent=2))
                return 0
            summary_keys = ('status', 'full_complete', 'real_unique_journals', 'journal_rows', 'category_rows',
                            'official_universe_verified', 'journal_coverage_percent', 'blockers', 'success',
                            'catalog_total', 'estimated_requests_including_catalog_recheck', 'requests', 'cache_hits')
            summary_keys += ('file_ingestion_complete', 'file_journal_row_coverage_percent', 'file_category_row_coverage_percent')
            print(json.dumps({k: result[k] for k in summary_keys if k in result}, ensure_ascii=False))
            return 0
    except JcrError as e:
        print('JCR 错误：' + str(e), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError, csv.Error):
        print('JCR 错误：local_file_or_schema_error', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
