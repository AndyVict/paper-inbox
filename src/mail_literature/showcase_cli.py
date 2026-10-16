"""Reproduce complete message examples from selected openly licensed papers."""
import argparse
from importlib.resources import files
import json
from pathlib import Path
import sys

from .journal_fields import validate_journal_fields
from .message_report import render_html, render_markdown
from .models import Article
from .preview import _atomic_write

TITLE = "文献整理后，你会读到什么"
NOTICE = ("以下两条来自已有本地消息预览，保留中文标题、英文标题、期刊、JIF/JCI 分区、中文摘要和论文链接。"
          "使用与正式推送相同的消息模板；这是本地消息预览。论文及原有机器译文按 CC BY 4.0 署名展示。"
          "期刊指标沿用消息生成时的 2025 年本地快照，尚未核验为官方最新数据；离线期刊查询可使用项目随包快照。")


def showcase_articles():
    bundle = json.loads(files("mail_literature").joinpath("resources/showcase.json").read_text(encoding="utf-8"))
    articles, credits = [], {}
    for item in bundle["papers"]:
        article = Article.from_dict(item["article"])
        validate_journal_fields(article)
        if not all(article.translation_current(field) for field in ("title", "abstract")):
            raise ValueError("invalid_showcase_translation")
        articles.append(article)
        credits[article.doi] = {key: item[key] for key in ("attribution", "source_url", "license", "license_url")}
    return articles, credits


def main(argv=None, *, project_root=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    root = (project_root or Path.cwd()).resolve()
    parser = argparse.ArgumentParser(description="离线生成完整消息展示；无需账号，不联网、不推送")
    parser.add_argument("--output-dir", type=Path, default=root / "data/showcase")
    args = parser.parse_args(argv)
    output = args.output_dir.resolve()
    if not output.is_relative_to(root / "data/showcase"):
        print("输出须位于独立的 data/showcase/ 下：invalid_showcase_output", file=sys.stderr)
        return 2
    try:
        articles, credits = showcase_articles()
        output.mkdir(parents=True, exist_ok=True)
        _atomic_write(output / "report.md", render_markdown(articles, title=TITLE, notice=NOTICE, credits=credits))
        _atomic_write(output / "report.html", render_html(articles, title=TITLE, notice=NOTICE, credits=credits))
        print(f"已生成 {len(articles)} 条完整消息预览；不联网、不推送。")
        print("Open: " + str(output / "report.html"))
        return 0
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        print("展示生成失败：showcase_generation_failed", file=sys.stderr)
        return 4
