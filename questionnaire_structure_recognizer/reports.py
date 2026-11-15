from __future__ import annotations

import html
import json
from pathlib import Path


def html_report(result: dict) -> str:
    esc = lambda value: html.escape(str(value or ""))
    questions = []
    for index, item in enumerate(result.get("questions", []), 1):
        options = "".join(f"<li>{'☑' if x.get('selected') else '☐'} {esc(x.get('text_original'))}</li>" for x in item.get("options", []))
        reasons = "".join(f"<li>{esc(x)}</li>" for x in item.get("review_reasons", []))
        questions.append(f"<article><h3>{index}. {esc(item.get('question_text_original'))}</h3><p><b>答案状态：</b>{esc(item.get('answer_status'))}</p><pre>{esc(item.get('answer_text_original'))}</pre>{f'<ul>{options}</ul>' if options else ''}{f'<div class=warning><ul>{reasons}</ul></div>' if reasons else ''}<details><summary>原文定位</summary><pre>{esc(json.dumps(item.get('source_locations'), ensure_ascii=False, indent=2))}</pre></details></article>")
    unresolved = "".join(f"<li>{esc(x.get('kind'))}：{esc(x.get('reason'))} <code>{esc(json.dumps(x.get('refs'),ensure_ascii=False))}</code></li>" for x in result.get("unresolved_items", []))
    warnings = "".join(f"<li>{esc(x.get('message') if isinstance(x,dict) else x)}</li>" for x in result.get("warnings", []))
    return f"""<!doctype html><html lang=zh-CN><meta charset=utf-8><title>问卷结构核对报告</title><style>body{{font:15px system-ui;max-width:980px;margin:30px auto;color:#182235}}article{{border-top:1px solid #ddd;padding:14px 0}}pre{{white-space:pre-wrap;background:#f6f7f9;padding:10px}}.warning{{background:#fff6dc;padding:8px}}code{{word-break:break-all}}</style><h1>问卷结构核对报告</h1><p>状态：{esc(result.get('extraction_status'))}　问题：{len(result.get('questions',[]))}　待检查：{len(result.get('unresolved_items',[]))}</p><h2>警告与未读取内容</h2><ul>{warnings or '<li>无全局警告</li>'}</ul><h2>问题与已有答案</h2>{''.join(questions) or '<p>尚未生成经过模型与程序校验的问题结构。</p>'}<h2>未解决内容</h2><ul>{unresolved or '<li>无</li>'}</ul></html>"""


def save_html(result: dict, path: str | Path):
    Path(path).write_text(html_report(result), encoding="utf-8")
