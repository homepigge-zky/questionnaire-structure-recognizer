from __future__ import annotations

import json
import re
from dataclasses import dataclass


NORMALIZATION_VERSION = "field-registry-v6-explicit-production-address"


@dataclass(frozen=True)
class FieldDefinition:
    key: str
    label: str
    entity_type: str
    patterns: tuple[str, ...]


# Ordered from specific to general.  This registry is shared by historical
# knowledge and new questionnaire fields; it does not contain answer text.
FIELD_REGISTRY = (
    FieldDefinition("person.position", "人员职位", "person", (r"^(?:position|job\s+title|title)(?:\s*[/,:]|$)", r"^(?:职位|职务|职称)(?:\s*[/,:：]|$)")),
    FieldDefinition("person.name", "人员姓名", "person", (r"^responsible\s+person.*name", r"^name\s+of\s+(?:the\s+)?responsible\s+person", r"^contact\s+person.*name", r"^name\s+of\s+(?:the\s+)?contact\s+person", r"负责人.*姓名", r"联系人.*姓名")),
    FieldDefinition("supplier.name", "供应商名称", "supplier", (r"^(?:name\s+of\s+(?:the\s+)?)?supplier(?:'s)?\s+name", r"^supplier\s+details.*name", r"供应商名称")),
    FieldDefinition("product.name", "产品或物料名称", "product", (r"^products?(?:\(s\))?\s+supplied$", r"^(?:product|material\s+brand|chemical|generic)\s+name(?:\s*[/,:]|$)", r"^name\s+of\s+(?:the\s+)?(?:product|material)", r"产品名称", r"物料名称")),
    FieldDefinition("site.name", "生产场所名称", "site", (r"^name\s+of\s+(?:the\s+)?manufacturing\s+site", r"^manufacturing\s+site\s+name", r"生产场所.*名称", r"工厂名称")),
    FieldDefinition("organization.name", "企业名称", "organization", (r"^company(?:'s)?\s+name", r"^name\s+of\s+(?:the\s+)?manufacturer", r"^manufacturer(?:'s)?\s+name", r"企业名称", r"公司名称", r"生产企业名称")),
    FieldDefinition("address.postal_code", "邮政编码", "organization", (r"^(?:zip|postal)\s*code(?:\s*[/,:]|$)", r"^(?:邮政编码|邮编)")),
    FieldDefinition("address.city", "城市", "organization", (r"^(?:city(?:/town)?|town)(?:\s*[/,:]|$)", r"^(?:城市|市区)(?:\s*[/,:：]|$)")),
    FieldDefinition("contact.phone", "联系电话", "organization", (r"^(?:telephone|phone)(?:\s+(?:number|no\.?))?(?:\s*[/,:]|$)", r"^(?:联系电话|电话号码|电话)(?:\s*[/,:：]|$)")),
    FieldDefinition("contact.fax", "传真", "organization", (r"^fax(?:\s+number)?(?:\s*[/,:]|$)", r"^传真(?:\s*[/,:：]|$)")),
    FieldDefinition("contact.email", "电子邮箱", "organization", (r"^e-?mail(?:\s+address)?(?:\s*[/,:]|$)", r"^(?:电子邮箱|邮箱)(?:\s*[/,:：]|$)")),
    FieldDefinition("organization.website", "企业网站", "organization", (r"^(?:website|web\s*site)(?:\s*[/,:]|$)", r"^(?:网址|网站)(?:\s*[/,:：]|$)")),
    FieldDefinition("address.full", "完整地址", "organization", (r"^(?:(?:registered|company|office|postal|mailing|(?:manufacturing|production)(?:\s+site)?|site)\s+)?address(?:\s*[,/:]|$)", r"^location(?:\s*[/,:]|$)", r"所在地", r"地址")),
)


@dataclass(frozen=True)
class TopicDefinition:
    key: str
    label: str
    parent: str
    patterns: tuple[str, ...]


# Topic classification and canonical-field identity are deliberately separate.
# A long question may safely belong to "质量管理/文件与记录" without being
# equivalent to any reusable standard field.  This prevents a useful UI
# category from accidentally becoming an answer-matching identity.
TOPIC_REGISTRY = (
    TopicDefinition("organization.basic", "企业基本信息", "基本信息", (r"^(?:联系单位|企业名称|公司名称|地\s*址|地址|电话|传真|web/email)", r"^(?:(?:name|address)\s+of\s+)?(?:supplier|company|manufacturer).{0,24}(?:name|address|telephone|fax|website|email)?$", r"^(?:company name|address|telephone|tel|fax|website|e-?mail)$")),
    TopicDefinition("organization.personnel", "人员与培训", "组织与人员", (r"员工|人员|培训|岗位|职责|staff|employee|personnel|training|responsib",)),
    TopicDefinition("quality.documents", "文件、程序与记录", "质量管理", (r"程序|文件|记录|sop|document|record|procedure|master production file",)),
    TopicDefinition("quality.audit", "审计与认证", "质量管理", (r"审计|认证|iso|gmp|audit|certif|regulatory authorit",)),
    TopicDefinition("quality.change", "变更、偏差与 CAPA", "质量管理", (r"变更|偏差|返工|oos|change control|deviation|rework|corrective|preventive",)),
    TopicDefinition("material.supplier", "物料与供应商", "供应链", (r"物料|原材料|供应商|material|raw material|supplier|coa|msds|cas no",)),
    TopicDefinition("warehouse.storage", "仓储与库存", "供应链", (r"仓库|储存|存货|隔离区域|warehouse|storage|fifo|quarantine",)),
    TopicDefinition("production.process", "生产工艺", "生产", (r"生产工艺|生产过程|批量|收率|物料平衡|返工|manufacturing process|production process|batch size|yield|reconciliation",)),
    TopicDefinition("facility.equipment", "厂房、设施与设备", "生产", (r"设备|设施|公用系统|纯化水|清洁|维护|校验|equipment|facility|utilities|water system|calibrat|maintenance|cleaning",)),
    TopicDefinition("quality.laboratory", "检验与实验室", "质量控制", (r"检验|实验室|试剂|分析|取样|测试|laboratory|lab |analytical|sampling|testing|reagent",)),
    TopicDefinition("quality.stability", "稳定性与有效期", "质量控制", (r"稳定性|有效期|复验期|stability|expiry|retest date",)),
    TopicDefinition("product.packaging", "包装与标签", "产品", (r"包装|标签|散装|packaging|label|bulk material",)),
    TopicDefinition("product.regulatory", "产品与法规状态", "产品", (r"产品|许可|批准|法规|product|license|marketing authorization|regulatory approval|gras",)),
    TopicDefinition("outsourcing.contract", "委托与外包", "合作方", (r"承包|委托|第三方|contract|third party|outsourc",)),
    TopicDefinition("data.electronic", "电子记录与计算机化系统", "数据完整性", (r"电子记录|电子签名|计算机化|electronic record|electronic signature|computerized system",)),
    TopicDefinition("review.issues", "问题与处理记录", "审核整改", (r"发现问题及处理结果|问题描述与处理结果|problem and handling|problem description\s*&\s*handling",)),
    TopicDefinition("review.signature", "审核与签署", "审批", (r"审核人|填表人|签名|盖章|reviewed by|completed by|signature|seal",)),
)


def classify_topic(question: str, section: str = "", context: str = "") -> dict:
    question_text = normalized_text(question)
    section_text = normalized_text(section)
    context_text = normalized_text(context)
    scored: list[tuple[int, int, TopicDefinition, list[str]]] = []
    for order, topic in enumerate(TOPIC_REGISTRY):
        evidence: list[str] = []
        score = 0
        for pattern in topic.patterns:
            if re.search(pattern, question_text, re.I):
                # Generic document/material wording is useful as a fallback,
                # but must not defeat a more specific stability, audit,
                # equipment or production signal in the same question.
                score += 3 if topic.key in {"quality.documents", "material.supplier"} else 5
                evidence.append("question:" + pattern)
            if section_text and re.search(pattern, section_text, re.I):
                score += 2; evidence.append("section:" + pattern)
            if context_text and re.search(pattern, context_text, re.I):
                score += 1; evidence.append("context:" + pattern)
        if score:
            scored.append((score, -order, topic, evidence))
    if not scored:
        return {"topic_key": "unclassified", "topic_label": "待归类", "topic_parent": "未归类", "topic_confidence": "low", "topic_evidence": []}
    score, _, topic, evidence = max(scored, key=lambda item: (item[0], item[1]))
    return {
        "topic_key": topic.key,
        "topic_label": topic.label,
        "topic_parent": topic.parent,
        "topic_confidence": "high" if score >= 8 else "medium" if score >= 4 else "low",
        "topic_evidence": evidence[:8],
    }


def normalized_text(value: str) -> str:
    value = (value or "").strip().lower()
    value = value.translate(str.maketrans({"：": ":", "？": "?", "（": "(", "）": ")", "　": " "}))
    # Remove display numbering only at the start.  Years, standards and other
    # meaningful numbers inside the field remain intact.
    value = re.sub(r"^\s*(?:[a-z]|\d+(?:\.\d+)*)[.)、:]?\s+", "", value, count=1, flags=re.I)
    return re.sub(r"\s+", " ", value).strip(" :\t\r\n")


def classify_field(question: str, section: str = "", context: str = "", task: dict | None = None, scope: dict | None = None) -> dict:
    clean = normalized_text(question)
    combined = " ".join((clean, normalized_text(section), normalized_text(context)))
    selected = next((definition for definition in FIELD_REGISTRY if any(re.search(pattern, clean, re.I) for pattern in definition.patterns)), None)
    # A bare "Name" is only meaningful with its local table/section context.
    # Keep it unclassified when context does not identify the named object.
    if not selected and clean in {"name", "名称"}:
        local_context, section_context = normalized_text(context), normalized_text(section)
        if re.search(r"position|signature|representative|contact|telephone|e-?mail|director|负责人|联系人|签名|职位", " ".join((local_context, section_context)), re.I):
            selected = next(x for x in FIELD_REGISTRY if x.key == "person.name")
        elif re.search(r'(?:details|information)\s*(?:of|on|about)?\s*(?:the\s*)?manufacturer\b',section_context,re.I):
            # A source heading about the manufacturer retains that object
            # even if its parenthesis mentions a different supplier.
            selected = next(x for x in FIELD_REGISTRY if x.key == 'organization.name')
        elif re.search(r"supplier|供应商", " ".join((local_context, section_context)), re.I):
            selected = next(x for x in FIELD_REGISTRY if x.key == "supplier.name")
        elif re.search(r"product|material|chemical|物料|产品", local_context, re.I) or re.search(r'(?:details|information|specification|identity).*?(?:product|material|chemical)|(?:product|material|chemical).*?(?:details|information|specification|identity)|(?:产品|物料)(?:资料|信息|详情)',section_context,re.I):
            selected = next(x for x in FIELD_REGISTRY if x.key == "product.name")
        elif re.search(r"manufacturing\s+site|factory|生产场所|製造所|工厂", " ".join((local_context, section_context)), re.I):
            selected = next(x for x in FIELD_REGISTRY if x.key == "site.name")
        elif re.search(r"company\s+information|organization\s+details|details of (?:the )?manufacturer|headquarters|企业信息|公司信息|总部", " ".join((local_context, section_context)), re.I):
            selected = next(x for x in FIELD_REGISTRY if x.key == "organization.name")
    scope, task = scope or {}, task or {}
    entity_type = selected.entity_type if selected else ""
    role, qualifiers = "", {}
    if selected and selected.key == "address.full":
        if re.search(r"manufacturing\s+(?:site|address)|production\s+(?:site|address)|factory|生产(?:场所|基地|厂区|区)?(?:英文)?地址|生产场所|製造所|工厂", combined, re.I):
            entity_type, role, qualifiers = "site", "manufacturing_site", {"address_use": "manufacturing_site"}
        elif re.search(r"registered|注册地址", combined, re.I):
            role, qualifiers = "organization", {"address_use": "registered"}
        elif re.search(r"headquarters|head office|本社|总部", combined, re.I):
            role, qualifiers = "organization", {"address_use": "headquarters"}
        else:
            qualifiers = {"address_use": "unspecified"}
    elif selected and selected.key == "organization.name":
        role = "manufacturer" if re.search(r"manufacturer|生产企业|制造商", combined, re.I) else "organization"
    elif selected and selected.key == "site.name": role = "manufacturing_site"
    elif selected and selected.key in {"person.name", "person.position"}:
        if re.search(r"site\s+operations\s+lead|site.*representative|生产场所.*负责人", combined, re.I): role = "site_representative"
        elif re.search(r"qa\s+office|quality\s+assurance", combined, re.I): role = "quality_contact"
        elif re.search(r"contact|telephone|e-?mail|联系人", combined, re.I): role = "contact_person"
        else: role = "person"
    elif selected and selected.key == "product.name": role = "product"

    # Explicit user-maintained scope takes precedence over wording context.
    if entity_type == "site":
        entity_id = str(scope.get("factory") or task.get("factory") or scope.get("text") or "")
        if selected and selected.key == "address.full" and entity_id: qualifiers["address_use"] = "manufacturing_site"
    elif entity_type == "organization": entity_id = str(scope.get("company") or task.get("company") or scope.get("text") or "")
    elif entity_type == "product": entity_id = str(scope.get("product") or task.get("product") or "")
    else: entity_id = ""
    if selected and selected.key in {"person.name", "person.position"} and role: qualifiers["person_role"] = role
    if selected and selected.key in {"contact.phone", "contact.fax", "contact.email"}:
        # Telephone alone is not proof of a contact-person field. Resolve the
        # owner from its local section/context, not from the label itself.
        owner_context = ' '.join((normalized_text(section), normalized_text(context)))
        if re.search(r"site\s+operations\s+lead|site.*representative|生产场所.*负责人", owner_context, re.I): contact_role = 'site_representative'
        elif re.search(r"qa\s+office|quality\s+assurance|质量.*(?:联系人|负责人)", owner_context, re.I): contact_role = 'quality_contact'
        elif re.search(r"contact\s+(?:information|person|details)|联系人", owner_context, re.I): contact_role = 'contact_person'
        else: contact_role = 'organization'
        qualifiers['contact_role'] = contact_role
    if selected and selected.key == "supplier.name": qualifiers["supplier_for_company"] = str(scope.get("company") or task.get("company") or "")
    topic = classify_topic(question, section, context)
    qualifiers.update(topic)
    evidence = {"question": question, "section": section, "context": context, "task_context": {k: task.get(k, "") for k in ("company", "factory", "product")}, "scope": scope, "registry_match": selected.key if selected else "", "topic": topic}
    return {"normalized_text": clean, "canonical_field_key": selected.key if selected else "", "canonical_label": selected.label if selected else "", "entity_type": entity_type, "entity_id": entity_id, "role": role, "qualifiers": qualifiers, "normalization_evidence": evidence, "normalization_version": NORMALIZATION_VERSION}


def upsert_annotation(db, owner_type: str, owner_id: str, question: str, section: str = "", context: str = "", task: dict | None = None, scope: dict | None = None) -> dict:
    value = classify_field(question, section, context, task, scope)
    db.execute(
        "INSERT INTO semantic_annotations(owner_type,owner_id,normalized_text,canonical_field_key,canonical_label,entity_type,entity_id,role,qualifiers_json,evidence_json,normalization_version) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(owner_type,owner_id) DO UPDATE SET normalized_text=excluded.normalized_text,canonical_field_key=excluded.canonical_field_key,canonical_label=excluded.canonical_label,entity_type=excluded.entity_type,entity_id=excluded.entity_id,role=excluded.role,qualifiers_json=excluded.qualifiers_json,evidence_json=excluded.evidence_json,normalization_version=excluded.normalization_version",
        (owner_type, owner_id, value["normalized_text"], value["canonical_field_key"], value["canonical_label"], value["entity_type"], value["entity_id"], value["role"], json.dumps(value["qualifiers"], ensure_ascii=False), json.dumps(value["normalization_evidence"], ensure_ascii=False), value["normalization_version"]),
    )
    return value


def annotation(row: dict | None) -> dict:
    if not row: return {}
    value = dict(row)
    value["qualifiers"] = json.loads(value.pop("qualifiers_json") or "{}")
    value["normalization_evidence"] = json.loads(value.pop("evidence_json") or "{}")
    return value
