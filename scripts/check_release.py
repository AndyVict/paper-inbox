"""Check publishable files without reading ignored runtime data or credentials."""
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", ".venv", "venv", "data", "logs", "__pycache__", "build", "dist", ".pytest_cache"}
FORBIDDEN_SUFFIXES = {".eml", ".db", ".sqlite", ".sqlite3", ".xlsx", ".pyc"}
SECRET = re.compile(r"(?:sk-proj-|sk-)[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|"
                    r"github_pat_[A-Za-z0-9_]{30,}|(?:AT_|UID_)[A-Za-z0-9_-]{24,}|"
                    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def public_files(root):
    if (root / ".git").exists():
        listing = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                                 cwd=root, check=True, capture_output=True).stdout
        return sorted({Path(name.decode("utf-8")) for name in listing.split(b"\0") if name})
    found = set()
    for folder, directories, names in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if name not in SKIP and not name.endswith(".egg-info")]
        for name in names:
            found.add((Path(folder) / name).relative_to(root))
    return sorted(found)


def check(root=ROOT):
    errors = []
    paths = public_files(root)
    for relative in paths:
        path = root / relative
        if (path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or
                any(part in {"data", "logs", ".venv", ".git"} for part in relative.parts) or
                path.suffix.lower() in FORBIDDEN_SUFFIXES or
                re.search(r"\.(?:sqlite3?|db)-(?:wal|shm|journal)$", path.name) or
                ".secret" in path.name or "secrets" in relative.parts or
                (path.name.startswith(".env") and path.name != ".env.example") or
                path.name.startswith("config.local.")):
            errors.append((str(relative), "private_or_unsafe_file"))
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            continue
        try:
            content = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            errors.append((str(relative), "unreadable_or_non_utf8"))
            continue
        for match in SECRET.finditer(content):
            if "synthetic" not in match.group().lower():
                errors.append((str(relative), "possible_credential"))
        if path.suffix == ".py":
            try:
                ast.parse(content, filename=str(relative), feature_version=(3, 11))
            except SyntaxError:
                errors.append((str(relative), "python_311_syntax"))
        if path.suffix == ".md":
            for target in LINK.findall(content):
                target = target.split("#", 1)[0].strip("<>")
                if not target or re.match(r"[a-z]+:", target, re.I):
                    continue
                resolved = (path.parent / target).resolve()
                if not resolved.is_relative_to(root.resolve()) or not resolved.exists():
                    errors.append((str(relative), "broken_local_link:" + target))
    required = ("LICENSE", "README.md", "README.en.md", "SECURITY.md", "CONTRIBUTING.md",
                ".env.example", ".github/workflows/ci.yml", "docs/assets/demo.png",
                "skills/paper-inbox/SKILL.md", "src/mail_literature/resources/demo.json")
    errors.extend((name, "required_file_missing") for name in required if not (root / name).is_file())
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    if metadata["project"].get("license") != "MIT":
        errors.append(("pyproject.toml", "license_mismatch"))
    return paths, errors


def main():
    paths, errors = check()
    print(json.dumps({"public_files": len(paths), "issues": len(errors)}, ensure_ascii=False))
    for path, code in errors:
        print(path + ": " + code, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
