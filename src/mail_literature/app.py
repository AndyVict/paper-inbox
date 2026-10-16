"""A single discoverable entry point; each operation keeps its own network flags."""
import argparse
import importlib
import sys

COMMANDS = {
    "demo": "demo_cli", "preview": "cli", "sync": "sync_cli",
    "abstracts": "enrich_cli", "translate": "translate_cli", "jcr": "jcr_match_cli",
    "send": "delivery_cli", "weekly": "weekly_cli",
}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in COMMANDS:
        return importlib.import_module("." + COMMANDS[argv[0]], __package__).main(argv[1:])
    parser = argparse.ArgumentParser(description="Paper Inbox: literature email → deduplicated reading list")
    parser.add_argument("command", choices=COMMANDS, help="Start with: paper-inbox demo")
    parser.parse_args(argv)
    return 0
