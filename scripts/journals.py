"""Query the bundled journal snapshot without installing the package."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from mail_literature.journals_cli import main

if __name__ == "__main__":
    raise SystemExit(main(project_root=ROOT))
