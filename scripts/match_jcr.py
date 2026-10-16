"""Python 3.11+：本地 JCR 分区精确匹配；--fetch 补查公开 ISSN。"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from mail_literature.jcr_match_cli import main


if __name__ == '__main__':
    raise SystemExit(main(project_root=ROOT))
