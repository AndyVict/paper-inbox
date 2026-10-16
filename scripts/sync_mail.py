"""Python 3.11+：SQLite 迁移/预览，以及显式 --fetch 的只读收信入口。"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mail_literature.sync_cli import main


if __name__ == "__main__":
    raise SystemExit(main(project_root=ROOT))
