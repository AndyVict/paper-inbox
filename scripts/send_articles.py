"""Python 3.11+：默认发送预览；显式 --send 才真实推送。"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from mail_literature.delivery_cli import main

if __name__ == "__main__":
    raise SystemExit(main(project_root=ROOT))
