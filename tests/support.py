from email import policy
from email.message import EmailMessage
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def email_bytes(content: str, source: str = "X-MOL", subtype: str = "html",
                charset: str = "utf-8", cte: str = "quoted-printable") -> bytes:
    mail = EmailMessage(policy=policy.default)
    mail["From"] = f"{source} <notices@example.invalid>"
    mail["To"] = "synthetic-recipient@example.invalid"
    mail["Subject"] = f"{source} synthetic offline fixture"
    mail["Date"] = "Fri, 09 Oct 2026 08:00:00 +0800"
    mail.set_content(content, subtype=subtype, charset=charset, cte=cte)
    return mail.as_bytes()


class LocalFilesCase(unittest.TestCase):
    def local_dir(self) -> Path:
        # Deliberately retain these isolated, ignored artifacts: no recursive or
        # batch cleanup of any directory is performed by the test suite.
        root = ROOT / "data" / "test-runs"
        root.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=self._testMethodName + "-", dir=root))
