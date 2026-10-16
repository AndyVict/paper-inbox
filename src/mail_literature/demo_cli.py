"""A deterministic offline demo that never writes into the live state database."""
import argparse
from email import policy
from email.message import EmailMessage
from hashlib import sha256
from html import escape
from importlib.resources import files
import json
from pathlib import Path
import sys

from .dedup import deduplicate
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
    cards = []
    for number, article in enumerate(articles, 1):
        sources = " + ".join(dict.fromkeys(ref.source for ref in article.sources))
        cards.append(f'''<article class="paper"><div class="eyebrow">PAPER {number:02d}<span>{escape(sources)}</span></div>
<h2>{escape(article.title_zh)}</h2><p class="english">{escape(article.title_en)}</p>
<div class="journal">{escape(article.journal or "Example Journal")} · {article.year or "2026"}</div>
<p class="abstract">{escape(article.abstract_zh)}</p><details><summary>English abstract / 英文摘要</summary><p>{escape(article.abstract_en)}</p></details>
<div class="paper-footer"><code>{escape(article.doi)}</code><span>示例标识 · 非真实论文</span></div></article>''')
    return '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; form-action 'none'">
<title>Paper Inbox · 合成演示</title><style>
*{box-sizing:border-box}body{margin:0;background:#f4f6f8;color:#192b3b;font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;line-height:1.7}
main{max-width:1060px;margin:0 auto;padding:42px 28px}.brand{font-size:15px;letter-spacing:2px;font-weight:750;color:#116d65}
.hero{padding:22px 0 28px}h1{font-size:clamp(28px,4vw,42px);letter-spacing:-1px;line-height:1.3;margin:12px 0}h1 small{display:block;font-size:18px;letter-spacing:0;color:#61717d;font-weight:400;margin-top:12px}
.notice{font-size:12px;color:#825e22;background:#fff5de;padding:10px 14px;border-radius:9px;margin:20px 0 0}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:0 0 24px}
.stat{background:#fff;border:1px solid #dfe5e9;border-radius:12px;padding:16px 20px}.stat strong{display:block;font-size:29px;line-height:1.4;color:#116d65}.stat span{font-size:12px;color:#657580}
.paper{background:#fff;border:1px solid #dfe5e9;border-radius:14px;padding:24px 28px;margin-bottom:18px}.eyebrow{font-size:11px;font-weight:700;letter-spacing:1.8px;color:#75838f;display:flex;justify-content:space-between}
.eyebrow span{color:#116d65;background:#eaf5f1;padding:2px 9px;border-radius:5px;letter-spacing:0}h2{font-size:22px;margin:10px 0 3px;line-height:1.5}.english{margin:0 0 12px;color:#687681;font-size:14px}.journal{font-size:12px;color:#71808b}.abstract{font-size:14px;margin:14px 0}
details{font-size:12px;color:#62717e}summary{cursor:pointer}.paper-footer{border-top:1px solid #eef1f3;margin-top:17px;padding-top:12px;font-size:10px;color:#88949e;display:flex;gap:12px;justify-content:space-between;flex-wrap:wrap}code{font-family:ui-monospace,monospace}
footer{font-size:12px;color:#71808b;margin:23px 2px}footer strong{color:#116d65}@media(max-width:640px){main{padding:25px 16px}.stats{grid-template-columns:repeat(2,1fr)}.paper{padding:19px}h2{font-size:19px}}
</style></head><body><main><div class="brand">PAPER INBOX</div><header class="hero"><h1>把文献订阅，变成阅读清单。<small>Literature alerts → one deduplicated reading list.</small></h1><div class="notice">''' + NOTICE + '''</div></header><section class="stats" aria-label="Demo statistics">''' + "".join(
        f'<div class="stat"><strong>{value}</strong><span>{label}</span></div>' for value, label in (
            (stats["emails"], "封订阅邮件 / emails"), (stats["candidates"], "条候选记录 / candidates"),
            (stats["unique"], "篇去重文献 / unique papers"), (stats["duplicates"], "条重复已合并 / duplicate merged"))) + '''</section>''' + "".join(cards) + '''<footer><strong>本地优先 · 按需启用</strong>　公开摘要 / 中文翻译 / 用户自备期刊指标 / WxPusher App 推送<br>演示仅复用解析、去重和预览逻辑；摘要与译文为固定合成数据。真实使用需另行配置相应服务。</footer></main></body></html>\n'''


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
        markdown = ["# Paper Inbox", "", NOTICE, "",
                    f"{stats['emails']} emails → {stats['candidates']} candidates → {stats['unique']} unique papers", ""]
        for article in articles:
            markdown.extend(["## " + article.title_zh, "", article.title_en, "", article.abstract_zh, ""])
        _atomic_write(output / "report.html", render_html(articles, stats))
        _atomic_write(output / "report.md", "\n".join(markdown))
        _atomic_write(output / "report.json", json.dumps(stats, ensure_ascii=False, indent=2) + "\n")
        print(NOTICE)
        print(f"{stats['emails']} emails → {stats['candidates']} candidates → {stats['unique']} papers; merged {stats['duplicates']} duplicate.")
        print("Open: " + str(output / "report.html"))
        return 0
    except (OSError, UnicodeError, ValueError, KeyError, OutboxError):
        print("演示失败：demo_generation_failed", file=sys.stderr)
        return 4
