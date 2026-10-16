"""Inspect built archives for omitted demo resources or accidentally packed private files."""
from pathlib import Path, PurePosixPath
import sys
import re
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    archives = sorted((ROOT / "dist").glob("*.whl")) + sorted((ROOT / "dist").glob("*.tar.gz"))
    if not archives:
        print("No built wheel or sdist found.", file=sys.stderr)
        return 1
    failures = []
    for archive in archives:
        if archive.suffix == ".whl":
            with zipfile.ZipFile(archive) as stream:
                names = stream.namelist()
        else:
            with tarfile.open(archive, "r:gz") as stream:
                names = stream.getnames()
        for name in names:
            path = PurePosixPath(name)
            if (path.is_absolute() or ".." in path.parts or
                    any(part in {"data", "logs", ".env", ".venv", ".git", "__pycache__"} for part in path.parts) or
                    path.name.startswith("config.local.") or
                    (path.name.startswith(".env") and path.name != ".env.example") or
                    re.search(r"\.(?:sqlite3?|db)-(?:wal|shm|journal)$", path.name) or
                    path.suffix in {".eml", ".db", ".sqlite", ".sqlite3", ".xlsx", ".pyc"}):
                failures.append(archive.name + ": private_or_unsafe_member")
        if not any(name.endswith("mail_literature/resources/demo.json") for name in names):
            failures.append(archive.name + ": missing_demo_resource")
        if not any(PurePosixPath(name).name == "LICENSE" for name in names):
            failures.append(archive.name + ": missing_license")
        print(archive.name + f": {len(names)} members inspected")
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
