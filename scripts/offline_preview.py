"""无需安装依赖的 Python 3.11+ 离线入口。"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mail_literature.cli import main


if __name__ == "__main__":
    raise SystemExit(main(project_root=ROOT))
