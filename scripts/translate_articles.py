"""默认只用本地缓存；显式 --fetch 才读取本机密钥及调用小牛翻译。"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mail_literature.translate_cli import main


if __name__ == "__main__":
    raise SystemExit(main(project_root=ROOT))
