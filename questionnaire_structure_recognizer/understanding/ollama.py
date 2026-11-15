from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx


PROMPT_VERSION = "qsr-field-relations-v29-spatial-form-boundaries"
FIELD_HINT_PROMPT_VERSION = "qsr-field-hints-v4-issue-topic"
ROLES = ["title", "section", "question", "answer", "option", "instruction", "table_header", "note", "attachment_requirement", "signature_field", "non_qa", "unresolved"]
NON_FIELD_ROLES = ["title", "section", "option", "instruction", "table_header", "note", "attachment_requirement", "signature_field", "non_qa", "unresolved"]
RELATIONS = ["confirmed", "proposed", "unresolved"]
LAYOUT_ROLES = ["document_title", "section_heading", "row_header", "column_header", "field_label", "answer_region", "answer_value", "unit", "option", "instruction", "note", "other"]
ROLE_CODES = {"T": "title", "S": "section", "Q": "question", "A": "answer", "O": "option", "I": "instruction", "H": "table_header", "N": "note", "R": "attachment_requirement", "G": "signature_field", "X": "non_qa", "U": "unresolved"}
OUTPUT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["a", "g"], "properties": {
    "a": {"type": "array", "maxItems": 256, "items": {"type": "string", "pattern": "^[TSQAOIHNRGXU]$"}},
    "g": {"type": "array", "maxItems": 128, "items": {"type": "array", "maxItems": 64, "items": {"type": "integer", "minimum": 0, "maximum": 255}}},
}}
SYSTEM = """你只分析问卷结构，不回答、改写、翻译或猜测原文。输入blocks的i是从0开始的唯一索引，g必须使用i的原值，不加1；b=块ID，x=原文，pg=页码，t=表格，r=行，q=列，xy=页面坐标[x0,y0,x1,y1]，左上为原点。扫描的空线、空框也是填写位置，按同页局部几何关系与对应问题分组；没有已填写值时不得把下一题标签或选项文字当作答案。
结合章节、表格行列、合并和相邻内容，将原文范围标为 title、section、question、answer、option、instruction、table_header、note、attachment_requirement、signature_field、non_qa 或 unresolved。无问号的填写字段仍是 question；标题、说明、表头和示例不是问题。Yes/No/N/A 是 option，只有原文☑☒✓✔或控件状态才表示已选；位于明确回答位置的“无/否/N/A”是 answer。中外文对照同题使用同一g；不同章节同名字段不得合并。
横向表单一行可以包含多组“标签—答复区”，例如 Q,EMPTY,Q,EMPTY；每个Q必须建立独立g，空白块仍是对应答复区域。遇到下一个独立Q就结束前一字段，禁止把后一个标签作为前一字段答案。记录表首行列名标H，不得两两组成问答。
输出a为与blocks等长的角色代码数组，每个块只放一个代码：T标题 S章节 Q问题 A答案 O选项 I说明 H表头 N备注 R附件要求 G签署 X其他 U不确定。g是问答关系组数组，每组列出同一道题的Q/A/O块索引；没有答案的问题也要单独成组。一个块含多个角色或多个独立问题时标U，保留给人工检查。不要把章节层级写进g；g只表达一道可回答字段内部的关系。
不要输出解释。"""

FIELD_HINT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["t", "p", "r", "c", "g"], "properties": {
    "t": {"type": "array", "maxItems": 32, "items": {"type": "string", "enum": list("SLIDTACFRMPG")}},
    "p": {"type": "array", "maxItems": 32, "items": {"type": "string", "enum": list("OCVSU")}},
    "r": {"type": "array", "maxItems": 32, "items": {"type": "string", "enum": list("ROU")}},
    "c": {"type": "array", "maxItems": 32, "items": {"type": "string", "enum": ["0", "1"]}},
    "g": {"type": "array", "maxItems": 32, "items": {"type": "string", "enum": list("BPDACMWRELSKGOIVXU")}},
}}
FIELD_HINT_SYSTEM = """只根据fields中的原文标签、章节、选项和结构提示判断字段元数据，不回答问题。返回五个与fields顺序等长的代码数组；每个输入字段必须各返回一个元素。t类型：S短文本 L长文本 I整数 D小数 T日期 A地址 C联系方式 F附件 R重复记录 M复合 P单选 G多选；p责任：O我方 C客户 V审核人 S共同 U不明确；r必填：R必填 O可选 U不明确；c是否存在条件：1或0；g业务主题：B企业基本信息 P人员培训 D文件记录 A审计认证 C变更偏差 M物料供应商 W仓储库存 R生产工艺 E厂房设备 L检验实验室 S稳定性 K包装标签 G产品法规 O委托外包 I电子记录 V审核签署 X问题处理记录 U不明确。业务主题不表示问题等价，也不能用于生成答案。原文没有依据时责任、必填和主题必须用U，不得猜测。"""


class ModelUnavailable(RuntimeError):
    pass


def _config():
    return {
        "base_url": os.getenv("QSR_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/"),
        "model": os.getenv("QSR_OLLAMA_MODEL", "qwen3:4b-instruct-2507-q4_K_M"),
        "timeout": float(os.getenv("QSR_MODEL_TIMEOUT", "240")),
        "max_chars": int(os.getenv("QSR_BATCH_MAX_CHARS", "7000")),
        "context": int(os.getenv("QSR_MODEL_CONTEXT", "4096")),
    }


def _local(url):
    parsed = urlparse(url)
    try:
        return parsed.hostname and ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return parsed.hostname == "localhost"


def health():
    config = _config()
    if os.getenv("QSR_MODEL_ENABLED", "false").lower() != "true":
        return {"enabled": False, "ready": False, "message": "仅完成原文提取；管理员未启用本地结构模型。"}
    if not _local(config["base_url"]):
        return {"enabled": True, "ready": False, "message": "模型地址必须是本机回环地址。"}
    try:
        response = httpx.get(config["base_url"] + "/api/tags", timeout=5)
        response.raise_for_status()
        models = [x.get("name", "") for x in response.json().get("models", [])]
        ready = config["model"] in models
        return {"enabled": True, "ready": ready, "model": config["model"], "message": "本地模型可用" if ready else f"未安装 {config['model']}"}
    except Exception as exc:
        return {"enabled": True, "ready": False, "message": "无法连接本地 Ollama", "diagnostic": type(exc).__name__}


def _batch(blocks, max_chars):
    """Keep each table row intact and repeat its first-row header after a split."""
    ordered = sorted(blocks, key=lambda x: x["order"]); lookup = {x["block_id"]: x for x in ordered}
    groups, plain, plain_size, index = [], [], 0, 0

    def item_cost(item):
        # PDF table cells are serialized in a compact row/column form by
        # _call.  Charging them the old rich-block overhead caused large PDFs
        # to be split into one request for every few physical cells.
        if item.get("kind") == "table_cell" and item.get("location", {}).get("type") == "pdf":
            return len(item.get("text", "")) + (110 if not item.get("text", "").strip() else 145)
        return len(item.get("text", "")) + 220

    def cost(items):
        return sum(item_cost(x) for x in items)

    def flush_plain():
        nonlocal plain, plain_size
        if plain:
            groups.append(plain); plain, plain_size = [], 0

    while index < len(ordered):
        block = ordered[index]
        table_id = block.get("structure", {}).get("table_id")
        if not table_id:
            plain_item_cost = len(block.get("text", "")) + 220
            # Keep a short continuous question/answer group together. The model
            # receives a soft allowance here; very large documents are still
            # bounded, while a section boundary no longer strands the answer in
            # a separate request merely because of JSON/location overhead.
            if plain and plain_size + plain_item_cost > max_chars * 1.35:
                flush_plain()
            plain.append(block); plain_size += plain_item_cost; index += 1
            continue
        preceding_ids = block.get("structure", {}).get("preceding_block_ids") or ([block.get("structure", {}).get("preceding_block_id")] if block.get("structure", {}).get("preceding_block_id") else [])
        context = []
        while plain and plain[-1]["block_id"] in preceding_ids:
            context.insert(0, plain.pop())
        if not context: context = [lookup[x] for x in preceding_ids if x in lookup]
        plain_size = cost(plain)
        flush_plain()
        table = []
        while index < len(ordered) and ordered[index].get("structure", {}).get("table_id") == table_id:
            table.append(ordered[index]); index += 1
        rows = {}
        for item in table:
            rows.setdefault(item.get("structure", {}).get("row", 0), []).append(item)
        row_numbers = sorted(rows)
        header = rows[row_numbers[0]] if row_numbers else []
        current = list(context)
        has_data = False
        for row_number in row_numbers:
            row = rows[row_number]
            if has_data and cost(current) + cost(row) > max_chars:
                groups.append(current); current = list(context); has_data = False
            current.extend(row); has_data = True
        if current:
            groups.append(current)
    flush_plain()
    # PDF extractors often report several adjacent fragments as separate
    # tables. Pack small consecutive groups back together up to the same token
    # budget; table ids and coordinates still preserve their boundaries.
    packed = []
    for group in groups:
        if packed and cost(packed[-1]) + cost(group) <= max_chars:
            known = {item["block_id"] for item in packed[-1]}
            packed[-1].extend(item for item in group if item["block_id"] not in known)
        else:
            packed.append(list(group))
    return packed


def _prefix_relationship_ids(output, prefix):
    value = json.loads(json.dumps(output))
    for segment in value.get("classifications", value.get("segments", [])):
        for key in ("group_id", "parent_group_id", "section_group_id"):
            if segment.get(key):
                segment[key] = f"{prefix}:{segment[key]}"
    for field in value.get("fields", []):
        field["field_id"] = f"{prefix}:{field.get('field_id','field')}"
        for child in field.get("children", []):
            child["field_id"] = f"{field['field_id']}:{child.get('field_id','child')}"
    return value


def _namespace(output, batch_index):
    return _prefix_relationship_ids(output, f"b{batch_index}")


def _expand_compact_output(value, block_lengths=None, block_ids=None):
    """Expand the token-efficient Ollama schema to the stable internal API."""
    layout_for_role = {"title": "document_title", "section": "section_heading", "table_header": "column_header", "option": "option", "instruction": "instruction", "note": "note"}
    block_lengths = block_lengths or {}
    block_ids = block_ids or list(block_lengths)
    groups = value.get("g", [])
    group_for_block = {}
    duplicate_members = set()
    for group_index, members in enumerate(groups):
        for index in members:
            if isinstance(index, int) and 0 <= index < len(block_ids):
                if index in group_for_block:
                    duplicate_members.add(index)
                group_for_block[index] = group_index
    split_blocks = {item[0] for item in value.get("s", []) if isinstance(item, list) and len(item) == 5}
    items = []
    role_vector = value.get("a", [])
    for index, block_id in enumerate(block_ids):
        if index in split_blocks:
            continue
        code = role_vector[index] if index < len(role_vector) else "U"
        group_index = group_for_block.get(index)
        if index in duplicate_members:
            code, group_index = 'U', None
        elif code == 'Q' and group_index is None:
            group_index = len(groups) + index
        items.append((ROLE_CODES.get(code, "unresolved"), block_id, 0, block_lengths.get(block_id, 0), group_index, None))
    for index, start, end, code, group_index in value.get("s", []):
        if isinstance(index, int) and 0 <= index < len(block_ids):
            items.append((ROLE_CODES.get(code, "unresolved"), block_ids[index], start, end, group_index if group_index >= 0 else None, None))
    classifications = []
    for role, block_id, start, end, group_index, parent_index in items:
        group_id = f"q{group_index + 1}" if isinstance(group_index, int) else ""
        parent_id = f"q{parent_index + 1}" if isinstance(parent_index, int) else ""
        classification = {
            "semantic_role": role, "layout_role": layout_for_role.get(role, "other"),
            "block_id": block_id, "start": start, "end": end,
            "group_id": group_id, "parent_group_id": parent_id, "section_group_id": "",
            "relation_status": "unresolved" if role == "unresolved" else "proposed", "reason": "",
        }
        classifications.append(classification)
    return {"classifications": classifications, "fields": []}


def _call(config, batch):
    payload, block_reverse, slot_reverse = [], {}, {}
    table_ids = list(dict.fromkeys(x.get("structure", {}).get("table_id") for x in batch if x.get("structure", {}).get("table_id")))
    table_alias = {value: f"T{index+1}" for index, value in enumerate(table_ids)}
    cell_ids = list(dict.fromkeys(x.get("structure", {}).get("cell_id") for x in batch if x.get("structure", {}).get("cell_id")))
    cell_alias = {value: f"C{index+1}" for index, value in enumerate(cell_ids)}
    block_alias = {block["block_id"]: f"B{index}" for index, block in enumerate(batch)}
    block_reverse = {value: key for key, value in block_alias.items()}
    for block_index, block in enumerate(batch):
        structure = json.loads(json.dumps(block.get("structure", {})))
        if structure.get("table_id") in table_alias: structure["table_id"] = table_alias[structure["table_id"]]
        if structure.get("cell_id") in cell_alias: structure["cell_id"] = cell_alias[structure["cell_id"]]
        if structure.get("anchor_cell_id") in cell_alias: structure["anchor_cell_id"] = cell_alias[structure["anchor_cell_id"]]
        if structure.get("preceding_block_id") in block_alias: structure["preceding_block_id"] = block_alias[structure["preceding_block_id"]]
        structure["preceding_block_ids"] = [block_alias.get(x, x) for x in structure.get("preceding_block_ids", [])]
        for slot_index, slot in enumerate(structure.get("logical_slots", []), 1):
            real_slot = slot["slot_id"]; compact_slot = f"{structure.get('cell_id','C')}S{slot_index}"; slot_reverse[compact_slot] = real_slot; slot["slot_id"] = compact_slot
        location = block.get("location", {})
        # Give Qwen the physical evidence needed to understand a local form,
        # without repeating the full diagnostic/style dictionaries.  The full
        # blocks remain available to the deterministic validator and exporter.
        compact = {
            "i": block_index, "b": block_alias[block["block_id"]], "x": block.get("text", ""),
            "k": "cell" if block.get("kind") == "table_cell" else block.get("kind"),
            "o": block["order"], "part": location.get("part"), "pg": location.get("page"),
            "t": structure.get("table_id"), "c": structure.get("cell_id"),
            "r": structure.get("row"), "q": structure.get("grid_col_start", structure.get("column")),
            "w": structure.get("grid_span", structure.get("column_span")),
            "h": structure.get("row_span"), "blank": structure.get("blank_state") == "blank",
            "merge": structure.get("merge_anchor_id") or structure.get("anchor_cell_id"),
            "layout": structure.get("local_layout_type") or structure.get("table_layout_hint"),
            "before": structure.get("preceding_block_ids") or [],
        }
        slots = structure.get("logical_slots") or []
        if slots:
            compact["slots"] = [{"id": x.get("slot_id"), "s": x.get("start"), "e": x.get("end"), "kind": x.get("kind")} for x in slots]
        controls = block.get("controls") or []
        if controls:
            compact["controls"] = [{
                "id": x.get("control_id"), "type": x.get("control_type",x.get('type')), "placeholder": x.get("is_placeholder",x.get('placeholder')),
                "checked": x.get("checked",x.get('selected')), "bound": bool(x.get("data_binding")), "locked": x.get("locked",x.get('lock')),
            } for x in controls]
        if location.get("type") == "pdf":
            box = location.get("bbox")
            if isinstance(box, (list, tuple)) and len(box) == 4:
                compact["xy"] = [round(float(v), 1) for v in box]
            if structure.get("scan_region_kind"):
                compact["scan"] = structure["scan_region_kind"]
            if structure.get("scan_candidate"):
                compact["candidate"] = True
        payload.append({key: value for key, value in compact.items() if value not in (None, [], "")})
    started = time.perf_counter()
    # Return source spans and relationship ids only; repeating a second field
    # tree made table-heavy questionnaires need far more output than input.
    # Qwen pretty-prints schema constrained arrays, so even a short semantic
    # vector can exceed a token estimate based on raw JSON characters.
    output_budget = 900
    response = httpx.post(config["base_url"] + "/api/chat", json={
        "model": config["model"], "stream": False, "format": OUTPUT_SCHEMA, "think": False, "keep_alive": "15m",
        "options": {"temperature": 0, "num_ctx": config["context"], "num_predict": output_budget},
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps({"blocks": payload}, ensure_ascii=False)}],
    }, timeout=config["timeout"])
    response.raise_for_status(); envelope = response.json(); raw_output = json.loads(envelope['message']['content']); output = _expand_compact_output(
        raw_output,
        {block_alias[b["block_id"]]: len(b.get("text", "")) for b in batch},
        [block_alias[b["block_id"]] for b in batch],
    )
    def restore(ref):
        if isinstance(ref, dict):
            ref["block_id"] = block_reverse.get(ref.get("block_id"), ref.get("block_id"))
            if ref.get("slot_id"): ref["slot_id"] = slot_reverse.get(ref["slot_id"], ref["slot_id"])
    for item in output.get("classifications", []): restore(item)
    output['model_trace']={'input_blocks':payload,'raw_output':raw_output}
    for field in output.get("fields", []):
        if field.get("section_ref"): restore(field["section_ref"])
        for key in ("label_refs", "answer_region_refs", "answer_value_refs", "option_refs", "unit_refs"):
            for ref in field.get(key, []): restore(ref)
        for child in field.get("children", []):
            for key in ("label_refs", "answer_region_refs", "answer_value_refs", "option_refs", "unit_refs"):
                for ref in child.get(key, []): restore(ref)
    return output, {
        "seconds": round(time.perf_counter() - started, 4), "prompt_tokens": envelope.get("prompt_eval_count", 0),
        "output_tokens": envelope.get("eval_count", 0), "load_seconds": round(envelope.get("load_duration", 0) / 1e9, 4),
    }


def analyze_field_hints(fields: list[dict], source_hash: str, cache_dir: Path, progress=None):
    """Analyze bounded groups of fields; never issue one call per field.

    Long bilingual questionnaires easily exceed a 4096-token context when all
    labels are submitted at once.  Grouping by a real character budget keeps
    neighbouring fields together while allowing failed groups to retry alone.
    """
    config = _config(); cache_dir.mkdir(parents=True, exist_ok=True)
    payload = [{
        "i": index, "label": field.get("question_text_original", ""),
        "section": field.get("section_text_original", ""),
        "options": [x.get("text_original", "") for x in field.get("options") or []],
        "has_children": bool(field.get("children")),
        "layout": (field.get("form_definition") or {}).get("location_evidence", ""),
    } for index, field in enumerate(fields)]
    # Field metadata has its own complete context. Keep an exact-input cache
    # so inserting a recovered comment slot does not invalidate every later
    # batch merely because its array index changed. No answers or permissions
    # are cached here; provenance includes document, prompt and model options.
    field_cache_paths = {}
    restored = {}
    for item in payload:
        stable = {k:v for k,v in item.items() if k != 'i'}
        material = {'source':source_hash,'field_id':fields[item['i']].get('field_id'),
                    'model':config['model'],'context':config['context'],
                    'prompt':FIELD_HINT_PROMPT_VERSION,'input':stable}
        key = hashlib.sha256(json.dumps(material,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        path = cache_dir / f'field-metadata-{key}.json'
        field_cache_paths[item['i']] = path
        if path.exists():
            try: restored[item['i']] = json.loads(path.read_text(encoding='utf8'))['hint']
            except (ValueError,KeyError): pass
    type_codes = {"S":"short_text","L":"long_text","I":"integer","D":"decimal","T":"date","A":"address","C":"contact","F":"attachment_reference","R":"repeating_record","M":"composite","P":"single_select","G":"multi_select"}
    responsibility_codes = {"O":"our_side","C":"customer","V":"reviewer","S":"shared","U":"unknown"}; required_codes = {"R":"required","O":"optional","U":"unknown"}
    topic_codes = {"B":"organization.basic","P":"organization.personnel","D":"quality.documents","A":"quality.audit","C":"quality.change","M":"material.supplier","W":"warehouse.storage","R":"production.process","E":"facility.equipment","L":"quality.laboratory","S":"quality.stability","K":"product.packaging","G":"product.regulatory","O":"outsourcing.contract","I":"data.electronic","V":"review.signature","X":"review.issues","U":"unclassified"}

    max_chars = min(int(config.get("max_chars") or 7000), 7000)
    groups, current, size = [], [], 0
    for item in payload:
        if item['i'] in restored: continue
        cost = len(item["label"]) + len(item["section"]) + sum(len(x) for x in item["options"]) + 160
        if current and (len(current) >= 28 or size + cost > max_chars):
            groups.append(current); current, size = [], 0
        current.append(item); size += cost
    if current: groups.append(current)

    def decode(vectors, count):
        def code(key, index):
            value = vectors.get(key) or []
            return str(value[index]) if isinstance(value, list) and index < len(value) else str(value)[index:index+1]
        decoded = []
        for index in range(count):
            decoded.append({"field_type_hint": type_codes.get(code("t", index)), "responsibility_hint": responsibility_codes.get(code("p", index), "unknown"), "required_hint": required_codes.get(code("r", index), "unknown"), "conditional_hint": code("c", index) == "1", "topic_hint": topic_codes.get(code("g", index), "unclassified")})
        return decoded

    def call_group(group, depth=0):
        material = {"source": source_hash, "model": config["model"], "prompt": FIELD_HINT_PROMPT_VERSION, "fields": group}
        key = hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        path = cache_dir / f"field-hints-{key}.json"
        if path.exists():
            cached = json.loads(path.read_text(encoding="utf-8")); return cached["hints"], {**cached["metric"], "cached_model_seconds":cached['metric'].get('seconds',0), "seconds":0, "prompt_tokens":0, "output_tokens":0, "cache_hit": True}
        started = time.perf_counter()
        try:
            response = httpx.post(config["base_url"] + "/api/chat", json={
                "model": config["model"], "stream": False, "format": FIELD_HINT_SCHEMA, "think": False, "keep_alive": "15m",
                "options": {"temperature": 0, "num_ctx": config["context"], "num_predict": max(260, len(group) * 7 + 80)},
                "messages": [{"role": "system", "content": FIELD_HINT_SYSTEM}, {"role": "user", "content": json.dumps({"fields": group}, ensure_ascii=False)}],
            }, timeout=config["timeout"])
            response.raise_for_status(); envelope = response.json(); vectors = json.loads(envelope["message"]["content"])
            hints = decode(vectors, len(group))
            metric = {"seconds": round(time.perf_counter()-started,4), "prompt_tokens": envelope.get("prompt_eval_count",0), "output_tokens": envelope.get("eval_count",0), "cache_hit": False, "groups": 1}
            path.write_text(json.dumps({"hints": hints, "metric": metric}, ensure_ascii=False), encoding="utf-8")
            return hints, metric
        except (json.JSONDecodeError, httpx.TimeoutException, httpx.HTTPStatusError):
            if len(group) <= 1 or depth >= 3:
                raise
            middle = len(group) // 2
            left_hints, left_metric = call_group(group[:middle], depth + 1)
            right_hints, right_metric = call_group(group[middle:], depth + 1)
            return left_hints + right_hints, {"seconds": round(left_metric.get("seconds",0)+right_metric.get("seconds",0),4), "prompt_tokens": left_metric.get("prompt_tokens",0)+right_metric.get("prompt_tokens",0), "output_tokens": left_metric.get("output_tokens",0)+right_metric.get("output_tokens",0), "cache_hit": left_metric.get("cache_hit",False) and right_metric.get("cache_hit",False), "groups": left_metric.get("groups",1)+right_metric.get("groups",1)}

    hints, metrics = [], []
    if progress: progress(0,len(groups))
    for index, group in enumerate(groups,1):
        group_hints, group_metric = call_group(group)
        for item,hint in zip(group,group_hints):
            restored[item['i']] = hint
            field_cache_paths[item['i']].write_text(json.dumps({'hint':hint},ensure_ascii=False),encoding='utf8')
        metrics.append(group_metric)
        if progress: progress(index,len(groups))
    metric = {"seconds": round(sum(x.get("seconds",0) for x in metrics),4), "prompt_tokens": sum(x.get("prompt_tokens",0) for x in metrics), "output_tokens": sum(x.get("output_tokens",0) for x in metrics), "cache_hit": bool(metrics) and all(x.get("cache_hit",False) for x in metrics), "groups": sum(x.get("groups",1) for x in metrics)}
    metric['fields_from_cache'] = len(payload)-sum(len(g) for g in groups)
    if not groups: metric['cache_hit'] = True
    return [restored[item['i']] for item in payload], metric


def _split_failed_batch(batch):
    """Split at complete table-row boundaries and repeat the table header."""
    table_ids = {item.get("structure", {}).get("table_id") for item in batch}
    table_ids.discard(None)
    if len(table_ids) == 1:
        context = [item for item in batch if not item.get("structure", {}).get("table_id")]
        rows = {}
        for item in batch:
            if not item.get("structure", {}).get("table_id"): continue
            rows.setdefault(item.get("structure", {}).get("row", 0), []).append(item)
        numbers = sorted(rows)
        if len(numbers) >= 2:
            middle = max(1, len(numbers) // 2)
            return [context + [item for row in numbers[:middle] for item in rows[row]], context + [item for row in numbers[middle:] for item in rows[row]]]
    if len(batch) >= 4:
        middle = len(batch) // 2
        return [batch[:middle], batch[middle:]]
    return []


def _call_resilient(config, batch, depth=0):
    try:
        return _call(config, batch)
    except (json.JSONDecodeError, httpx.TimeoutException, httpx.HTTPStatusError):
        parts = _split_failed_batch(batch) if depth < 3 else []
        if not parts:
            raise
        segments, fields, child_metrics = [], [], []
        for part_index, part in enumerate(parts, 1):
            output, metric = _call_resilient(config, part, depth + 1)
            # Every compact response starts relation ids at q1.  Prefix each
            # recovered branch before concatenation or unrelated rows from
            # sibling sub-batches become one field during validation.
            output = _prefix_relationship_ids(output, f"s{part_index}")
            segments.extend(output.get("classifications", output.get("segments", [])))
            fields.extend(output.get("fields", []))
            child_metrics.append(metric)
        return {"classifications": segments, "fields": fields}, {
            "seconds": round(sum(x.get("seconds", 0) for x in child_metrics), 4),
            "prompt_tokens": sum(x.get("prompt_tokens", 0) for x in child_metrics),
            "output_tokens": sum(x.get("output_tokens", 0) for x in child_metrics),
            "load_seconds": round(sum(x.get("load_seconds", 0) for x in child_metrics), 4),
            "recovered_sub_batches": sum(x.get("recovered_sub_batches", 1) for x in child_metrics),
        }


def _batch_cache_diagnostics(cache_dir: Path, key: str, batch: list[dict], cache_hit: bool):
    """Report actual checkpoint identity without changing lookup or inference.

    Only hexadecimal keys inside this cache may be inspected. A missing or
    malformed historical link is reported as incomplete, never invented.
    Source text and customer values are deliberately excluded.
    """
    info = {"cache_key": key, "cache_read_key": key if cache_hit else None,
            "input_block_ids": [b["block_id"] for b in batch], "input_block_count": len(batch),
            "cache_source_chain": [], "cache_trace_status": "complete"}
    visited = set(); current = key
    for _ in range(16):
        if current in visited:
            info["cache_trace_status"] = "cyclic_predecessor"; break
        visited.add(current)
        path = cache_dir / (current + ".json")
        entry = {"cache_key": current, "exists": path.is_file()}
        info["cache_source_chain"].append(entry)
        if not entry["exists"]:
            info["cache_trace_status"] = "missing_predecessor" if current != key else "cache_not_written"; break
        try:
            raw = path.read_bytes(); entry["sha256"] = hashlib.sha256(raw).hexdigest()
            payload = json.loads(raw)
            provenance = payload.get("cache_provenance") or {}
            previous = provenance.get("from_cache_key")
        except (OSError, ValueError, AttributeError, TypeError):
            info["cache_trace_status"] = "unreadable_predecessor"; break
        if previous is None:
            break
        if not isinstance(previous, str) or len(previous) != 64 or any(c not in "0123456789abcdef" for c in previous):
            info["cache_trace_status"] = "invalid_predecessor"; break
        current = previous
    else:
        info["cache_trace_status"] = "predecessor_limit"
    return info


def analyze_blocks(blocks: list[dict], source_hash: str, parser_version: str, cache_dir: Path, progress=None):
    state = health()
    if not state.get("ready"):
        raise ModelUnavailable(state["message"])
    config = _config(); cache_dir.mkdir(parents=True, exist_ok=True)
    # Only real extraction checkpoints of this exact file supply candidate
    # parser versions. The batch key below is intentionally unchanged: every
    # semantic input, model, prompt and option must still hash identically.
    from ..parser_core.models import ExtractedDocument
    prior_native = []
    for native_path in sorted((cache_dir.parent / 'native').glob('*.json')):
        try:
            native = ExtractedDocument.model_validate_json(native_path.read_text(encoding='utf8'))
            if (native.source_hash == source_hash and native.parser_version != parser_version
                    and native.extraction_status != 'failed'):
                ledger = {b.block_id: b for b in native.source_blocks}
                if len(ledger) == len(native.source_blocks):
                    prior_native.append((native.parser_version, ledger))
        except (ValueError, KeyError, OSError):
            continue
    batches = _batch(blocks, config["max_chars"]); outputs, metrics = [], []
    for index, batch in enumerate(batches, 1):
        material = {"source": source_hash, "parser": parser_version, "model": config["model"],
            "prompt": PROMPT_VERSION, "context": config["context"], "max_chars": config["max_chars"],
            "batch": [{"block_id": b["block_id"], "text": b.get("text", ""), "kind": b.get("kind"),
                "table_id": b.get("structure", {}).get("table_id"),
                "cell_id": b.get("structure", {}).get("cell_id"),
                "row": b.get("structure", {}).get("row_start"),
                "column_start": b.get("structure", {}).get("column_start"),
                "column_end": b.get("structure", {}).get("column_end"),
                "blank_state": b.get("structure", {}).get("blank_state"),
                "row_layout_hint": b.get("structure", {}).get("row_layout_hint"),
                "structure": b.get('structure', {}), "style": b.get('style', {}),
                "controls": b.get("controls", [])} for b in batch]}
        key = hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        path = cache_dir / f"{key}.json"
        if not path.exists():
            for previous_parser, ledger in prior_native:
                # The legacy key encodes location through the stable block ID,
                # not as a separate property. Check physical location and order
                # explicitly rather than trusting an unchanged/corrupt ID.
                if any(b['block_id'] not in ledger
                       or ledger[b['block_id']].location != b.get('location', {})
                       or ledger[b['block_id']].order != b.get('order') for b in batch):
                    continue
                previous_material = {**material, 'parser': previous_parser}
                previous_key = hashlib.sha256(json.dumps(previous_material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                previous_path = cache_dir / f'{previous_key}.json'
                if not previous_path.exists():
                    continue
                try:
                    previous = json.loads(previous_path.read_text(encoding='utf8'))
                    if (not isinstance(previous.get('output'), dict) or not isinstance(previous.get('metric'), dict)
                            or previous['metric'].get('failed')):
                        continue
                    provenance = {'method': 'identical_batch_across_verified_native_parser_checkpoints',
                                  'source_hash': source_hash, 'from_parser': previous_parser,
                                  'to_parser': parser_version, 'from_cache_key': previous_key,
                                  'to_cache_key': key}
                    payload = {**previous, 'cache_provenance': provenance}
                    temporary = path.with_suffix('.' + str(time.time_ns()) + '.tmp')
                    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf8')
                    temporary.replace(path)
                    break
                except (ValueError, KeyError, OSError, TypeError):
                    continue
        if path.exists():
            cache_started=time.perf_counter()
            cached = json.loads(path.read_text(encoding="utf-8")); output, metric = cached["output"], {**cached["metric"], "cache_hit": True,
                "cached_model_seconds":cached['metric'].get('seconds',0),"seconds":round(time.perf_counter()-cache_started,4),"prompt_tokens":0,"output_tokens":0,"load_seconds":0}
            if cached.get('cache_provenance'):
                metric['cache_provenance'] = cached['cache_provenance']
        else:
            last = None
            # Resilient calls already split JSON/time-out failures. Repeating
            # the complete parent batch would duplicate minutes of work.
            for attempt in range(1):
                try:
                    output, metric = _call_resilient(config, batch); last = None; break
                except Exception as exc:
                    last = exc
            if last:
                outputs.append({"classifications": [], "fields": []}); metrics.append({"batch": index, "failed": type(last).__name__, "cache_hit": False, **_batch_cache_diagnostics(cache_dir, key, batch, False)})
                if progress: progress(index, len(batches), "failed")
                continue
            path.write_text(json.dumps({"output": output, "metric": metric}, ensure_ascii=False), encoding="utf-8")
            metric["cache_hit"] = False
        outputs.append(_namespace(output, index)); metrics.append({"batch": index, **metric, **_batch_cache_diagnostics(cache_dir, key, batch, bool(metric.get("cache_hit")))})
        if progress: progress(index, len(batches), "complete")
    info = {"provider": "ollama-local", "model": config["model"], "prompt_version": PROMPT_VERSION, "batches": len(batches), "metrics": metrics, "failed_batches": [x["batch"] for x in metrics if x.get("failed")], "cache_hits": sum(bool(x.get("cache_hit")) for x in metrics)}
    return outputs, info
