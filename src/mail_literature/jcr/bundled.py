"""Resolve the shipped journal snapshot or an explicitly selected local one."""
from importlib.resources import files
from pathlib import Path

from .storage import snapshot_path

DEFAULT_SNAPSHOT = "bundled-jcr-2025"


def resolve_snapshot(root, snapshot_id=DEFAULT_SNAPSHOT):
    if snapshot_id == DEFAULT_SNAPSHOT:
        return Path(str(files("mail_literature").joinpath("resources/jcr", DEFAULT_SNAPSHOT)))
    return snapshot_path(root, snapshot_id)
