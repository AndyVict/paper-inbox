"""UTF-8 快照、凭据清理和无损 CSV 字符串。"""
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit

JOURNAL_FIELDS = ('journal_id journal_title journal_abbreviation issn eissn publisher country_region '
                  'citation_indices metric_year release_year snapshot_id jif five_year_jif jci total_citations '
                  'eigenfactor article_influence_score record_status missing_reason source_url retrieved_at '
                  'identity_status field_missing_reasons').split()
CATEGORY_FIELDS = ('journal_id metric_year release_year snapshot_id category_id category_name index_or_edition '
                   'jif_quartile jif_rank jif_category_total jif_percentile jci_quartile jci_rank '
                   'jci_category_total data_status missing_reason source_url retrieved_at '
                   'field_missing_reasons').split()
JOURNAL_METRICS = 'jif five_year_jif jci total_citations eigenfactor article_influence_score'.split()
CATEGORY_METRICS = ('jif_quartile jif_rank jif_category_total jif_percentile '
                    'jci_quartile jci_rank jci_category_total').split()
SECRET_FIELD = re.compile(r'api.?key|authorization|password|cookie|token|secret', re.I)


class JcrError(Exception):
    """只携带固定错误码，禁止保存原始异常。"""


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def text_value(value):
    if value is None:
        return ''
    if isinstance(value, (dict, list, bool)):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return str(value)


def safe_url(value):
    p = urlsplit(value)
    # 删除所有查询参数、fragment 和用户信息，防止签名、APIKEY 和追踪参数泄漏。
    if p.scheme not in ('http', 'https'):
        return value.split('?', 1)[0].split('#', 1)[0]
    return urlunsplit((p.scheme, p.hostname or '', p.path, '', ''))


def sanitize(value, secrets=()):
    if isinstance(value, dict):
        return {k: sanitize(v, secrets) for k, v in value.items() if not SECRET_FIELD.search(k)}
    if isinstance(value, list):
        return [sanitize(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, '[REDACTED]')
        if value.startswith(('https://', 'http://', '/')):
            value = safe_url(value)
    return value


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    with temp.open('wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)


def write_json(path, value):
    atomic_write(path, (dumps(value) + '\n').encode('utf-8'))


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        raise JcrError('invalid_json_file') from None


def write_csv(path, rows, fields):
    import io
    columns = list(fields) + sorted({key for row in rows for key in row} - set(fields))
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    writer.writerows({k: text_value(v) for k, v in row.items()} for row in rows)
    atomic_write(path, buffer.getvalue().encode('utf-8-sig'))
    return columns


def read_csv(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def flatten(value, prefix='official', result=None):
    result = {} if result is None else result
    for key, item in value.items():
        name = prefix + '__' + key
        if isinstance(item, dict):
            flatten(item, name, result)
        else:
            result[name] = text_value(item)
    return result


def valid_issn(value):
    if not re.fullmatch(r'\d{4}-\d{3}[\dX]', value):
        return False
    digits = value.replace('-', '')
    return (sum(int(digits[i]) * (8-i) for i in range(7)) +
            (10 if digits[7] == 'X' else int(digits[7]))) % 11 == 0


@contextmanager
def locked(snapshot):
    lock = Path(snapshot) / '.jcr.lock'
    try:
        with lock.open('x', encoding='utf-8') as f:
            f.write(now())
    except FileExistsError:
        raise JcrError('snapshot_locked') from None
    try:
        yield
    finally:
        lock.unlink()  # 仅删除本进程创建的一个锁文件，不批量清理。


def snapshot_path(root, snapshot_id):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{2,160}', snapshot_id):
        raise JcrError('invalid_snapshot_id')
    base = (Path(root) / 'data/jcr').resolve()
    path = (base / snapshot_id).resolve()
    if path.parent != base:
        raise JcrError('snapshot_path_outside_jcr')
    return path


def initialize(path, manifest):
    path.mkdir(parents=True, exist_ok=True)
    with locked(path):
        manifest_file = path / 'snapshot.json'
        if manifest_file.exists():
            old = read_json(manifest_file)
            for key in ('snapshot_id', 'metric_year', 'release_year', 'revision', 'source_kind', 'synthetic'):
                if old.get(key) != manifest.get(key):
                    raise JcrError('snapshot_version_conflict')
            return old
        manifest = sanitize(manifest)
        manifest['created_at'] = now()
        write_json(manifest_file, manifest)
        write_csv(path / 'journals.csv', [], JOURNAL_FIELDS)
        write_csv(path / 'journal_categories.csv', [], CATEGORY_FIELDS)
        return manifest
