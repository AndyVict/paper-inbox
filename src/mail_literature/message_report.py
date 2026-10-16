"""Readable HTML and Markdown views of the actual delivery message."""
from html import escape

from .delivery import render_message

LABELS = ("中文标题", "英文标题", "期刊名称", "JIF分区", "JCI 分区", "中文摘要", "链接")


def message_fields(article):
    content, _ = render_message(article)
    fields = []
    for line in content.rstrip("\n").split("\n"):
        label, separator, value = line.partition("：")
        if separator and label in LABELS:
            fields.append((label, value))
        elif fields:
            label, value = fields[-1]
            fields[-1] = (label, value + "\n" + line)
    return fields


def credit_text(credit):
    return (credit["attribution"] + " · " + credit["license"] +
            "；中文标题与摘要为已有机器译文，未另作摘要概述。")


def render_markdown(articles, *, title, notice, credits=None):
    lines = ["# " + title, "", notice, ""]
    for number, article in enumerate(articles, 1):
        lines.extend(["## " + str(number) + ". " + article.title_zh, ""])
        for label, value in message_fields(article):
            if label == "链接" and value.startswith("https://"):
                value = "[打开论文](" + value + ")"
            lines.extend(["**" + label + "：**" + value, ""])
        if credits:
            credit = credits[article.doi]
            lines.extend([credit_text(credit), "",
                          "[论文来源](" + credit["source_url"] + ") · [许可](" + credit["license_url"] + ")", ""])
    return "\n".join(lines)


def render_html(articles, *, title, notice, stats=None, credits=None):
    cards = []
    for number, article in enumerate(articles, 1):
        fields = dict(message_fields(article))
        metrics = "".join("<div><dt>" + escape(label) + "</dt><dd>" + escape(fields[label]) + "</dd></div>"
                          for label in ("期刊名称", "JIF分区", "JCI 分区"))
        link = fields["链接"]
        link_html = ('<a href="' + escape(link, quote=True) + '">' + escape(link) + '</a>'
                     if link.startswith("https://") else escape(link))
        credit_html = ""
        if credits:
            credit = credits[article.doi]
            credit_html = ('<footer class="credit">' + escape(credit_text(credit)) + ' '
                           '<a href="' + escape(credit["source_url"], quote=True) + '">论文来源</a> · '
                           '<a href="' + escape(credit["license_url"], quote=True) + '">许可</a></footer>')
        cards.append(f'''<article class="paper" aria-label="文献 {number}">
<div class="paper-number">文献 {number:02d} · 消息预览</div>
<div class="label">中文标题</div><h2>{escape(fields['中文标题'])}</h2>
<div class="label">英文标题</div><p class="english">{escape(fields['英文标题'])}</p>
<dl class="metrics">{metrics}</dl>
<div class="label abstract-label">中文摘要</div><p class="abstract">{escape(fields['中文摘要'])}</p>
<div class="paper-link"><span class="label">链接</span>{link_html}</div>{credit_html}</article>''')
    stats_html = ""
    if stats:
        stats_html = '<p class="stats">' + escape(
            f"{stats['emails']} 封邮件 → {stats['candidates']} 条候选 → {stats['unique']} 篇文献；合并 {stats['duplicates']} 条重复") + '</p>'
    return '''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; form-action 'none'">
<title>''' + escape(title) + '''</title><style>
*{box-sizing:border-box}body{margin:0;background:#f4f3ef;color:#202b31;font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;line-height:1.8}
main{max-width:980px;margin:auto;padding:36px 28px 52px}.brand{color:#176658;letter-spacing:2px;font-size:13px;font-weight:700}
h1{font-size:30px;line-height:1.4;margin:12px 0}header p{font-size:13px;color:#5b6569;max-width:850px;margin:12px 0 26px}
.stats{padding:12px 18px;background:#e7ede8;border-radius:6px;color:#245c4f;font-size:14px}
.paper{background:white;border:1px solid #dedfd7;border-top:3px solid #26705e;border-radius:8px;margin:20px 0;padding:26px 32px}
.paper-number{font-size:12px;color:#176658;letter-spacing:1px;margin-bottom:22px;font-weight:650}.label,dt{font-size:12px;color:#66726e;font-weight:600}
h2{font-size:23px;line-height:1.55;margin:4px 0 17px}.english{font-size:15px;line-height:1.7;margin:4px 0 20px;color:#4b565c}
.metrics{font-size:13px;margin:0;padding:16px 18px;background:#f4f6f2;border-radius:6px}.metrics div{display:grid;grid-template-columns:76px 1fr;gap:10px;margin:4px 0}dd{margin:0}
.abstract-label{margin-top:22px}.abstract{font-size:15px;text-align:justify;white-space:pre-line;margin:6px 0 22px;line-height:1.95}
.paper-link{display:flex;gap:16px;align-items:baseline;border-top:1px solid #eceee7;padding-top:15px;font-size:13px}
a{color:#176658;text-underline-offset:3px;overflow-wrap:anywhere}.credit{font-size:11px;color:#707b75;margin-top:18px;line-height:1.8}
@media(max-width:640px){main{padding:22px 12px 32px}h1{font-size:24px}.paper{padding:22px 18px}h2{font-size:21px}.metrics{padding:12px}.metrics div{grid-template-columns:1fr;gap:0;margin:8px 0}.abstract{font-size:15px;text-align:left}.paper-link{display:block}.paper-link .label{display:block}header p{margin-bottom:18px}}
</style></head><body><main><header><div class="brand">PAPER INBOX</div><h1>''' + escape(title) + '''</h1><p>''' + escape(notice) + '''</p></header>''' + stats_html + "".join(cards) + '</main></body></html>\n'
