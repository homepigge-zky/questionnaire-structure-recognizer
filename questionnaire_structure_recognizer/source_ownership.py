"""Source-backed document defaults, independent of answers and model labels.

A repeated form title is still evidence of the respondent, even though it is
not a question. Conversely, a business question or example is not permission.
Local ownership and signing guards must take precedence over these defaults.
"""
from __future__ import annotations

import re

OWNER_POLICY_VERSION = "source-owner-v18-source-current-attachment-declarations"


def pdf_local_review_owner_scope(field: dict, lookup: dict[str, dict]):
    """Resolve an explicitly assigned QA panel from its closed native geometry.

    OCR may concatenate a complete section caption. A named party is not
    itself evidence: require both a review caption and a nearby completion
    instruction naming that same party, inside contiguous closed source rows.
    Scope never extends into another page, a separate column or another panel.
    """
    named_section=pdf_named_issuer_section_scope(field,lookup)
    if named_section:return named_section
    refs = field.get('label_refs') or field.get('question_refs') or []
    anchors = [lookup.get(ref.get('block_id'), {}) for ref in refs]
    anchors = [b for b in anchors if b.get('location', {}).get('type') == 'pdf'
               and len(b.get('location', {}).get('bbox') or []) == 4]
    if not anchors:
        return None
    pages = {b['location'].get('page') for b in anchors}
    if len(pages) != 1:
        return None
    page = next(iter(pages))
    local = [b for b in lookup.values() if b.get('location', {}).get('type') == 'pdf'
             and b.get('location', {}).get('page') == page
             and len(b.get('location', {}).get('bbox') or []) == 4]

    def box(b):
        return b['location']['bbox']

    def contains(outer, inner):
        # Small raster edge uncertainty is not permission to cross columns.
        return (outer[0] - 2 <= inner[0] and outer[2] + 2 >= inner[2]
                and outer[1] - 2 <= inner[1] and outer[3] + 2 >= inner[3])

    def compact(text):
        return re.sub(r'[^a-z0-9]', '', text.casefold())

    closed = [b for b in local if b.get('structure', {}).get('scan_region_kind') == 'text_box'
              and b.get('structure', {}).get('closed_region_evidence')]
    if not closed:
        return None
    for instruction in local:
        raw = str(instruction.get('text') or '').strip()
        if len(raw) > 180 or non_authorizing_statement(raw):
            continue
        match = re.fullmatch(r'(?:tobe)?(?:completed|filledin|filledout)by(?:the)?([a-z0-9]{1,70}qa)', compact(raw))
        if not match:
            continue
        party = match.group(1)
        headers = []
        for heading in local:
            caption = str(heading.get('text') or '').strip()
            if not caption or len(caption) > 180 or non_authorizing_statement(caption):
                continue
            header = re.fullmatch(r'(?:section[ivxlcdm0-9]+)?reviewby(?:the)?([a-z0-9]{1,70}qa)', compact(caption))
            if (header and header.group(1) == party and 0 <= box(instruction)[1] - box(heading)[1] <= 65
                    and abs(box(heading)[0] - box(instruction)[0]) <= 18):
                headers.append(heading)
        if len(headers) != 1:
            continue
        heading = headers[0]
        roots = [b for b in closed if contains(box(b), box(heading))]
        if not roots:
            continue
        root = min(roots, key=lambda b: (box(b)[2]-box(b)[0]) * (box(b)[3]-box(b)[1]))
        scope = [root]
        remaining = [b for b in closed if b is not root and box(b)[1] >= box(root)[1] - 2
                     and box(root)[0] - 2 <= box(b)[0] and box(b)[2] <= box(root)[2] + 2]
        changed = True
        while changed:
            changed = False
            for candidate in list(remaining):
                cb = box(candidate)
                if any(abs(cb[1] - box(previous)[3]) <= 3
                       and min(cb[2], box(previous)[2]) - max(cb[0], box(previous)[0]) > 3
                       for previous in scope):
                    scope.append(candidate)
                    remaining.remove(candidate)
                    changed = True
        if not any(contains(box(b), box(instruction)) for b in scope):
            continue
        if not all(any(contains(box(b), box(anchor)) for b in scope) for anchor in anchors):
            continue
        # A later explicit assignment is a real scope boundary even when
        # adjacent table borders happen to touch.
        anchor_top = max(box(b)[1] for b in anchors)
        intervening = [b for b in local if box(instruction)[3] < box(b)[1] < anchor_top
                       and contains([box(root)[0], box(b)[1], box(root)[2], box(b)[3]], box(b))
                       and re.fullmatch(r'(?:tobe)?(?:completed|filledin|filledout)by[a-z0-9]+', compact(str(b.get('text') or '')))
                       and not non_authorizing_statement(str(b.get('text') or ''))]
        if intervening:
            continue
        field['_pdf_owner_evidence_refs'] = [
            {'block_id': b['block_id'], 'start': 0, 'end': len(b['text'])} for b in (heading, instruction)]
        role = 'our_side' if party.startswith(('supplier', 'vendor', 'manufacturer')) else 'reviewer'
        return role, ['原文同一闭合表格区域的审核标题和填写指令指定同一 QA 填写方；仅限该局部区域']
    return None


def pdf_named_issuer_section_scope(field: dict, lookup: dict[str, dict]):
    """A named issuer-only section in a supplier questionnaire is local.

    Require the named party to equal the repeated source letterhead plus an
    actual supplier questionnaire title. A company name alone, or a later
    section, cannot take ownership of earlier supplier response/signing rows.
    """
    labels=field.get('label_refs') or field.get('question_refs') or []
    anchors=[lookup.get(r.get('block_id'),{}) for r in labels]
    if not anchors or any(b.get('location',{}).get('type')!='pdf' or len(b['location'].get('bbox',[]))!=4 for b in anchors):return None
    pages={b['location'].get('page') for b in anchors}
    if len(pages)!=1:return None
    page=next(iter(pages));top=min(b['location']['bbox'][1] for b in anchors)
    compact=lambda s:re.sub(r'[^a-z0-9\u4e00-\u9fff]','',str(s).casefold())
    titles=[b for b in lookup.values() if b.get('location',{}).get('type')=='pdf' and b['location'].get('page')==1
            and _supplier_title(str(b.get('text','')))]
    if not titles:return None
    pattern=re.compile(r'^\s*(?:[A-Z0-9]+[.)]\s*)?SECTION\s+FOR\s+(.+?)\s*[:：]?\s*$',re.I)
    headers=[]
    for b in lookup.values():
        loc=b.get('location',{});bb=loc.get('bbox',[]);raw=str(b.get('text','')).strip()
        match=pattern.fullmatch(raw)
        if loc.get('type')=='pdf' and loc.get('page')==page and len(bb)==4 and bb[3]<=top and match and not non_authorizing_statement(raw):
            headers.append((b,compact(match.group(1))))
    if not headers:return None
    heading,party=max(headers,key=lambda p:p[0]['location']['bbox'][1])
    if len(party)<8:return None
    letterheads=[b for b in lookup.values() if b.get('location',{}).get('type')=='pdf' and
                 len(b['location'].get('bbox',[]))==4 and b['location']['bbox'][3]<130 and compact(b.get('text'))==party]
    if len({b['location'].get('page') for b in letterheads})<2 or not any(b['location'].get('page')==1 for b in letterheads):return None
    local_stop=[b for b in lookup.values() if b.get('location',{}).get('page')==page and
                len(b['location'].get('bbox',[]))==4 and heading['location']['bbox'][3]<b['location']['bbox'][1]<top and
                re.fullmatch(r'\s*(?:questionnaire\s+)?(?:to be\s+)?(?:filled in|completed)\s+by\s*[:：]?\s*',str(b.get('text','')),re.I)]
    if local_stop:return None
    evidence=[heading,titles[0],next(b for b in letterheads if b['location']['page']==1)]
    field['_pdf_owner_evidence_refs']=[{'block_id':b['block_id'],'start':0,'end':len(b['text'])} for b in evidence]
    return 'customer',['原文指定本节由问卷发出单位填写；该单位与重复页眉一致，范围仅限本页该标题以下区域']


def responder_metadata_evidence(field: dict, lookup: dict[str, dict]) -> list[dict]:
    """This copy's responder details, bounded to an actual native table group.

    A supplier default does not authorize reusing a previous responder or
    submission deadline. Company contacts outside this group remain unaffected.
    Bilingual original captions are checked as separate source lines.
    """
    labels = field.get('label_refs') or field.get('question_refs') or []
    for ref in labels:
        anchor = lookup.get(ref.get('block_id'), {})
        native, loc = anchor.get('structure', {}), anchor.get('location', {})
        start, end = ref.get('start'), ref.get('end')
        raw = str(anchor.get('text') or '')
        if not (isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(raw)):
            continue
        captions = [re.sub(r'[^a-z\u4e00-\u9fff]', '', x.casefold()) for x in raw[start:end].splitlines()]
        if not captions or any(not re.fullmatch(
            r'(?:printed)?name|title|jobtitle|email(?:address)?|requestedreturndate'
            r'|姓名(?:正楷)?|职务|职位|电子邮件地址|要求的回复时间', x) for x in captions if x):
            continue
        if loc.get('type') != 'pdf' or not native.get('table_id') or not isinstance(native.get('row_start'), int):
            continue
        for source in lookup.values():
            group = source.get('structure', {})
            if (source.get('location', {}).get('page') != loc.get('page')
                    or group.get('table_id') != native['table_id']
                    or not isinstance(group.get('row_start'), int)
                    or group['row_start'] >= native['row_start']
                    or group.get('grid_col_start', 999) > native.get('grid_col_start', -1)
                    or group.get('grid_col_end', -1) < native.get('grid_col_end', 999)):
                continue
            lines = [re.sub(r'[^a-z\u4e00-\u9fff]', '', x.casefold()) for x in str(source.get('text') or '').splitlines()]
            if lines and all(re.fullmatch(r'responderinformation|respondentinformation|回复者信息|填表人信息', x) for x in lines if x):
                return [{'block_id': source['block_id'], 'start': 0, 'end': len(source['text'])}]
    return []


def non_authorizing_statement(text: str) -> bool:
    text = str(text or '').strip()
    return bool(
        '?' in text or '？' in text or
        # PDF/OCR line breaks commonly separate the question mark from the
        # opening line. A process question mentioning "reviewed by" is not a
        # declaration that the reviewer must fill this form or later pages.
        re.match(r'\s*(?:(?:\d+(?:\.\d+)*|[A-Za-z])[.)、:]?\s+)?'
                 r'(?:is|are|was|were|do|does|did|has|have|can|could|will|would|should|shall|'
                 r'what|which|who|whom|whose|when|where|why|how)\b', text, re.I) or
        re.search(r'是否|是不是|有无|有没有', text) or
        # "Example Buyer use only" is a named customer's restriction, not an
        # example instruction. Require an actual example/quotation introducer.
        re.search(r'\b(?:for example|example wording|sample wording|quoted|quotation)\b'
                  r'|\bexample(?:\s+\d+)?\s*[:：]|示例|举例|例如|样例', text, re.I) or
        re.search(r'\b(?:if|unless|in case of)\b|如果|若|当.+时', text, re.I) or
        re.search(r'\b(?:not|never)\s+(?:to\s+be\s+)?(?:filled|completed)|不得.{0,10}填写|不由', text, re.I) or
        re.match(r'\s*["“「]', text)
    )


def _document_instruction(text: str) -> bool:
    if non_authorizing_statement(text):
        return False
    compact = re.sub(r'[^a-z\u4e00-\u9fff]', '', text.casefold())
    # Explicitly this document, not a procedure about completing other forms.
    return bool(re.search(
        r'(?:this|the)(?:questionnaire|form|survey|document)'
        r'(?:is|shallbe|shouldbe|mustbe|istobe|tobe)?(?:filledin|filledout|filled|completed)'
        r'by(?:the|a)?(?:supplier|vendor|manufacturer|apimanufacturer)'
        r'|(?:the)?(?:supplier|vendor|manufacturer)(?:shall|should|must|isrequestedto)'
        r'(?:fillin|fillout|complete)(?:this|the)(?:questionnaire|form|survey|document)'
        r'|(?:本|此)(?:问卷|表格|调查表|表单)(?:应|请|须|必须)?(?:由|交由)'
        r'(?:供应商|供方|生产商|制造商|生产厂家)(?:负责)?(?:填写|填报)', compact))


def _supplier_title(text: str) -> bool:
    if non_authorizing_statement(text) or len(text) > 240:
        return False
    compact = re.sub(r'[^a-z\u4e00-\u9fff]', '', text.casefold())
    # A requalification form is addressed to the existing supplier too.
    # Require a complete form title, not a procedure/report mentioning it.
    # Some source headers omit "form" but name the raw-material scope.
    if re.fullmatch(
        r'(?:formtitle|title)?(?:vendor|supplier)requalification'
        r'(?:form(?:forrawmaterials?)?|forrawmaterials?)'
        r'(?:api(?:excipients?)?)?', compact):
        return True
    # A dual supplier/contractor respondent is an explicit questionnaire
    # title. Match the complete title (optionally its bilingual counterpart),
    # never a procedure or an audit decision merely mentioning these words.
    english=r'(?:supplier|vendor)contractorqualificationquestionnaire'
    chinese=r'供应商承包商(?:资质|资格)(?:确认|审核|评估)?问卷'
    if re.fullmatch(rf'(?:{english})(?:{chinese})?|(?:{chinese})(?:{english})?',compact):
        return True
    # Exact title starts; merely mentioning a questionnaire in prose is not a
    # title. OCR whitespace loss is used for comparison only, never rewritten.
    return bool(re.match(
        r'^(?:formtitle|title)?(?:(?:vendor|supplier)(?:qualification)?questionnaire'
        r'|questionnaireforsupplierqualification|selfchecksheetforapimanufacturer'
        r'|manufacturersevaluationquestionnaire|manufacturers?assessmentquestionnaire'
        r'|questionnaireforrawmaterialmanufacturerqualification'
        r'|供应商调查问卷|供应商问卷|供方调查表'
        r'|(?:生产商|制造商|生产厂家)(?:调查问卷|问卷调查表|问卷|调查表))', compact))


def _company_manufacturing_evidence(preamble: list[dict], blocks: list[dict]) -> list[dict]:
    """A generic company title needs two independent native manufacturing cues.

    The company name/customer logo alone is not permission. Restrict evidence
    to short field labels in the first table immediately following an actual
    introductory company-questionnaire title, never answers, filenames or
    model guesses. It establishes the respondent, not who owns local review
    columns; the normal local exclusions retain precedence.
    """
    for title in preamble:
        raw = str(title.get('text') or '').strip()
        if (title.get('kind') not in {'paragraph', 'heading', 'title'}
                or title.get('location', {}).get('type') != 'docx'
                or title.get('location', {}).get('part') != 'word/document.xml'
                or title not in blocks[:8] or non_authorizing_statement(raw)
                or not re.fullmatch(r'(?:[（(][^（）()]{1,24}[）)]\s*)?(?:公司问卷|企业问卷|Company\s+Questionnaire)', raw, re.I)):
            continue
        nearby = [b for b in blocks if title.get('order', 0) < b.get('order', 0)
                  and b.get('location', {}).get('part') == 'word/document.xml'][:35]
        first = next((b for b in nearby if b.get('kind') == 'table_cell' and b.get('structure', {}).get('table_id')), None)
        if first is None:
            continue
        table_id = first['structure']['table_id']
        labels = [b for b in nearby if b.get('kind') == 'table_cell'
                  and b.get('structure', {}).get('table_id') == table_id
                  and len(str(b.get('text') or '')) <= 140
                  and not non_authorizing_statement(str(b.get('text') or ''))]
        address = next((b for b in labels if re.fullmatch(
            r'(?:公司|企业)?(?:生产区|生产场所|生产基地|制造场所)(?:英文)?地址[:：]?'
            r'|(?:Company\s+)?(?:Manufacturing|Production)\s+(?:Site\s+)?(?:English\s+)?Address[:：]?',
            str(b.get('text') or '').strip(), re.I)), None)
        count = next((b for b in labels if re.fullmatch(
            r'(?:Total\s+)?Number\s+of\s+(?:Manufacturing|Production)\s+(?:blocks|sites|facilities)[:：]?'
            r'|(?:(?:总共)?(?:几个|多少个)生产区|生产区(?:数量|数目))[:：]?',
            str(b.get('text') or '').strip(), re.I)), None)
        if address and count and address['block_id'] != count['block_id']:
            return [{'block_id': b['block_id'], 'start': 0, 'end': len(b['text'])} for b in (title, address, count)]
    return []


def document_owner_evidence(field: dict, lookup: dict[str, dict]) -> list[dict]:
    """Return exact native references for an introductory supplier default.

    Ordering and page provenance, not a filename or a model role, select the
    preamble. Empty layout blocks don't truncate it. Repeated headers are not
    dropped. A future local instruction can never own an earlier field.
    """
    labels = field.get('label_refs') or field.get('question_refs') or []
    field.pop('_document_owner_evidence_kind', None)
    anchor = lookup.get(labels[0].get('block_id'), {}) if labels else {}
    if not anchor:
        return []
    loc = anchor.get('location', {})
    blocks = sorted((b for b in lookup.values() if str(b.get('text') or '').strip()),
                    key=lambda b: b.get('order', 0))
    preamble = []
    for block in blocks:
        location = block.get('location', {})
        if location.get('type') != loc.get('type'):
            continue
        header = str(location.get('part', '')).startswith('word/header')
        if loc.get('type') == 'pdf':
            if location.get('page') != 1 or loc.get('page', 0) < 1:
                continue
            if loc.get('page') == 1 and block.get('order', 0) >= anchor.get('order', 0):
                continue
        elif not header and (location.get('part') != loc.get('part') or block.get('order', 0) >= anchor.get('order', 0)):
            continue
        if header or block in blocks[:30]:
            preamble.append(block)
    for block in preamble:
        text = str(block.get('text') or '')
        if _document_instruction(text) or _supplier_title(text):
            return [{'block_id': block['block_id'], 'start': 0, 'end': len(text)}]
    company = _company_manufacturing_evidence(preamble, blocks)
    if company:
        field['_document_owner_evidence_kind'] = 'company_manufacturing_context'
    return company
