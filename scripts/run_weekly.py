"""Python 3.11+：按所选阶段整理文献并可选推送。"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from mail_literature.weekly_cli import main

if __name__ == "__main__":
    raise SystemExit(main(project_root=ROOT))
