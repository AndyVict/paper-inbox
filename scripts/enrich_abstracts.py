"""Python 3.11+：默认只读缓存，显式 --fetch 联网补全公开摘要。"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mail_literature.enrich_cli import main


if __name__ == "__main__":
    raise SystemExit(main(project_root=ROOT))
