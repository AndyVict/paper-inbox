"""A deterministic offline demo that never writes into the live state database."""
import argparse
from email import policy
from email.message import EmailMessage
from hashlib import sha256
from importlib.resources import files
import json
from pathlib import Path
import sys

from .dedup import deduplicate
from . import message_report
from .parsers import parse_eml
from .preview import OutboxError, _atomic_write, write_snapshot_previews

NOTICE = "合成演示 / SYNTHETIC DEMO · 固定示例摘要与译文 · 无联网、无推送、无真实 JCR 数据"


def demo_articles():
    fixture = json.loads(files("mail_literature").joinpath("resources/demo.json").read_text(encoding="utf-8"))
    candidates = []
    for message in fixture["messages"]:
        mail = EmailMessage(policy=policy.default)
        mail["From"] = message["source"] + " <demo@example.invalid>"
        mail["To"] = "reader@example.invalid"
        mail["Subject"] = message["source"] + " synthetic literature alert"
        mail["Date"] = "Thu, 01 Oct 2026 09:00:00 +0800"
        mail.set_content(message["html"], subtype="html", charset="utf-8")
        parsed = parse_eml(mail.as_bytes())
        if not parsed.articles:
            raise ValueError("invalid_bundled_demo")
        candidates.extend(parsed.articles)
    articles = deduplicate(candidates)
    for article in articles:
        fields = fixture["articles"][article.doi]
        article.abstract_en = fields["abstract_en"]
        for name in ("title", "abstract"):
            setattr(article, name + "_zh", fields[name + "_zh"])
            setattr(article, name + "_zh_source_hash", sha256(getattr(article, name + "_en").encode("utf-8")).hexdigest())
        # The demo's IF marker is a parser fixture, never a journal metric.
        article.impact_factor = None
        article.quartile_claimed = None
    stats = {"synthetic": True, "emails": len(fixture["messages"]), "candidates": len(candidates),
             "unique": len(articles), "duplicates": len(candidates) - len(articles),
             "network_requests": 0, "messages_sent": 0}
    return articles, stats


def render_html(articles, stats):
    return message_report.render_html(articles, title="两封订阅邮件，去重后得到三篇文献", notice=NOTICE, stats=stats)


def main(argv=None, *, project_root=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    root = (project_root or Path.cwd()).resolve()
    parser = argparse.ArgumentParser(description="无需配置的离线合成演示；不联网、不发送、不写入活动数据库")
    parser.add_argument("--output-dir", type=Path, default=root / "data/demo")
    args = parser.parse_args(argv)
    output = args.output_dir.resolve()
    if not output.is_relative_to(root / "data/demo"):
        print("输出须位于独立的 data/demo/ 下：invalid_demo_output", file=sys.stderr)
        return 2
    try:
        articles, stats = demo_articles()
        output.mkdir(parents=True, exist_ok=True)
        write_snapshot_previews(articles, output / "outbox")
        markdown = message_report.render_markdown(
            articles, title="合成邮件解析与去重演示", notice=NOTICE)
        _atomic_write(output / "report.html", render_html(articles, stats))
        _atomic_write(output / "report.md", markdown)
        _atomic_write(output / "report.json", json.dumps(stats, ensure_ascii=False, indent=2) + "\n")
        print(NOTICE)
        print(f"{stats['emails']} emails → {stats['candidates']} candidates → {stats['unique']} papers; merged {stats['duplicates']} duplicate.")
        print("Open: " + str(output / "report.html"))
        return 0
    except (OSError, UnicodeError, ValueError, KeyError, OutboxError):
        print("演示失败：demo_generation_failed", file=sys.stderr)
        return 4
