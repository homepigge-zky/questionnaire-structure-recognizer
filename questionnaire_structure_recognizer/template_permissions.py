"""Honor existing Word read-only exceptions without removing protection.

Only explicit `edGrp=everyone` ranges are authorized. Column ranges are resolved
against physical table cells, never expanded into a whole-row permission. Their
table/row/column boundaries and complete vertical-merge coverage are checked.
OOXML permission semantics: https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.wordprocessing.permstart
"""
from copy import deepcopy
import hashlib
import json

from docx.oxml.ns import qn
from lxml import etree


def _ancestor(node, tag):
    return next((p for p in node.iterancestors() if p.tag == qn(tag)), None)


def _grid_cells(row):
    before = row.find('./' + qn('w:trPr') + '/' + qn('w:gridBefore'))
    column = int(before.get(qn('w:val'), '0')) if before is not None else 0
    cells = []
    for cell in row.findall(qn('w:tc')):
        span = cell.find('./' + qn('w:tcPr') + '/' + qn('w:gridSpan'))
        width = int(span.get(qn('w:val'), '1')) if span is not None else 1
        cells.append((cell, column, column + width))
        column += width
    return cells


def _column_policy(start, end):
    """A conservative, same-table interpretation of an OOXML column grant.

    Word's colFirst/colLast index physical tc elements, not tblGrid columns
    (verified with independent merged-cell documents in native Word). Grid spans
    are used only to verify vertical-merge continuity. Invalid ranges, including
    Word's permissive out-of-bounds fallback, never authorize application writes.
    """
    try:
        first, last = int(start.get(qn('w:colFirst'))), int(start.get(qn('w:colLast')))
    except (TypeError, ValueError):
        return None
    if first < 0 or last < first:
        return None
    table, end_table = _ancestor(start, 'w:tbl'), _ancestor(end, 'w:tbl')
    row, cell = _ancestor(start, 'w:tr'), _ancestor(start, 'w:tc')
    if table is None or end_table is not table or row is None or row.getparent() is not table:
        return None
    rows = table.findall(qn('w:tr'))
    if not row.findall(qn('w:tc')) or cell is not row.findall(qn('w:tc'))[0]:
        return None
    # Native Word places permEnd directly after the last row. Also accept the
    # schema-equivalent row child after every physical cell; no end in mid-cell.
    if end.getparent() is table:
        preceding = list(table)[:list(table).index(end)]
        end_row = next((n for n in reversed(preceding) if n.tag == qn('w:tr')), None)
        if end_row is None or any(n.tag not in {qn('w:permStart'), qn('w:permEnd')}
                for n in preceding[preceding.index(end_row) + 1:]):
            return None
    elif end.getparent().tag == qn('w:tr') and end.getparent().getparent() is table:
        end_row = end.getparent()
        if any(n.tag == qn('w:tc') for n in list(end_row)[list(end_row).index(end) + 1:]):
            return None
    else:
        return None
    low, high = rows.index(row), rows.index(end_row)
    if high < low:
        return None
    row_cells = [r.findall(qn('w:tc')) for r in rows]
    if any(last >= len(row_cells[i]) for i in range(low, high + 1)):
        return None
    selected = {c for i in range(low, high + 1) for c in row_cells[i][first:last + 1]}
    authorized = set()
    try:
        grids = [_grid_cells(r) for r in rows]
        for i in range(low, high + 1):
            for candidate, left, right in grids[i]:
                if candidate not in selected:
                    continue
                merge = candidate.find('./' + qn('w:tcPr') + '/' + qn('w:vMerge'))
                if merge is not None and merge.get(qn('w:val'), 'continue') != 'restart':
                    continue  # a continuation is not an independently writable cell
                if merge is not None:
                    complete = True
                    for following in grids[i + 1:]:
                        continuation = next((c for c, a, b in following if (a, b) == (left, right)), None)
                        vm = continuation.find('./' + qn('w:tcPr') + '/' + qn('w:vMerge')) if continuation is not None else None
                        if vm is None or vm.get(qn('w:val'), 'continue') == 'restart':
                            break
                        if continuation not in selected:
                            complete = False
                    if not complete:
                        continue
                authorized.add(candidate)
    except (TypeError, ValueError):
        return None
    return {'start': start, 'end': end, 'cells': authorized, 'column_first': first,
            'column_last': last, 'row_first': low + 1, 'row_last': high + 1}


def _policies(root):
    starts={}; ends={}
    for node in root.iter(qn('w:permStart')):
        starts.setdefault(node.get(qn('w:id')),[]).append(node)
    for node in root.iter(qn('w:permEnd')):
        ends.setdefault(node.get(qn('w:id')),[]).append(node)
    order={node:i for i,node in enumerate(root.iter())}
    plain = {}; columns = {}
    for key, nodes in starts.items():
        if (key is None or len(nodes) != 1 or len(ends.get(key, [])) != 1 or
                order[nodes[0]] >= order[ends[key][0]] or
                nodes[0].get(qn('w:edGrp')) != 'everyone' or nodes[0].get(qn('w:ed'))):
            continue
        start, end = nodes[0], ends[key][0]
        if start.get(qn('w:colFirst')) is None and start.get(qn('w:colLast')) is None:
            plain[key] = (start, end)
        else:
            policy = _column_policy(start, end)
            if policy and policy['cells']:
                columns[key] = policy
    return plain, columns


def allowed_range_ids(root):
    plain, columns = _policies(root)
    return set(plain) | set(columns)


def protected_events(root, allowed):
    """Compare all XML outside authorized intervals, even across containers."""
    # python-docx CT_P.text is computed descendant text, not XML node.text.
    # Use plain XML nodes so permitted run edits do not appear as paragraph
    # edits, while real paragraph properties/attributes stay protected.
    root=etree.fromstring(etree.tostring(root))
    plain, columns = _policies(root)
    column_cells = {cell for key, policy in columns.items() if key in allowed for cell in policy['cells']}
    # Only direct paragraph runs are editable under a column grant. Table/row/
    # cell/paragraph properties and nested tables/controls remain protected.
    editable_runs = {run for cell in column_cells for paragraph in cell.findall(qn('w:p'))
                     for run in paragraph.findall(qn('w:r'))}
    editable_nodes = set(editable_runs)
    for run in editable_runs:
        for child in run:
            if child.tag == qn('w:rPr'):
                editable_nodes.update(child.iter())
            elif child.tag in {qn('w:t'), qn('w:br'), qn('w:cr'), qn('w:tab')}:
                editable_nodes.add(child)
    # Field delimiters/instructions, drawing/object payloads and control
    # metadata do not become editable merely because their run is in a cell.
    events=[];active=set()
    for event,node in etree.iterwalk(root,events=('start','end')):
        key=node.get(qn('w:id'))
        marker=node.tag in {qn('w:permStart'),qn('w:permEnd')}
        if marker:
            # Markers themselves, order and permissions never become editable.
            events.append((event,node.tag,tuple(sorted(node.attrib.items())),node.text,node.tail))
            if event=='end' and key in allowed and key in plain:
                if node.tag==qn('w:permStart'):active.add(key)
                else:active.discard(key)
        elif not active and node not in editable_nodes:
            events.append((event,node.tag,tuple(sorted(node.attrib.items())),node.text if event=='start' else node.tail))
    return events


class ReadonlyRangeGuard:
    def __init__(self,document):
        self.document=document
        self.allowed=allowed_range_ids(document._element)
        self.before=protected_events(document._element,self.allowed)
        self.settings=etree.tostring(document.settings._element,method='c14n')

    def evidence(self):
        """Hashes describe protection actually checked; they confer no new rights."""
        return {'policy': 'explicit-everyone-physical-columns-v1',
                'authorized_range_ids': sorted(self.allowed),
                'protected_xml_sha256': hashlib.sha256(json.dumps(self.before, ensure_ascii=False).encode()).hexdigest(),
                'settings_sha256': hashlib.sha256(self.settings).hexdigest()}

    def verify(self):
        from .exporter import TemplateFillError
        if (allowed_range_ids(self.document._element)!=self.allowed or
            protected_events(self.document._element,self.allowed)!=self.before or
            etree.tostring(self.document.settings._element,method='c14n')!=self.settings):
            raise TemplateFillError('回填计划触及受保护内容或改变了原权限范围，已停止导出；请使用标准 Word')

    def _append_permission(self, paragraph):
        from .exporter import TemplateFillError
        nodes=list(self.document._element.iter());index={n:i for i,n in enumerate(nodes)}
        plain, columns = _policies(self.document._element)
        pairs=[]
        for key, (start, end) in plain.items():
            if key not in self.allowed:continue
            if end.getparent()==paragraph._p:
                after=list(paragraph._p)[list(paragraph._p).index(end)+1:]
                if any(''.join(n.itertext()).strip() for n in after):continue
                # The permission starts in this paragraph or spans its tail.
                if index[start]<index[end] and (start.getparent()==paragraph._p or index[start]<index[paragraph._p]):
                    pairs.append((start,end,{'range_id':key,'kind':'text_interval'}))
            elif ((start.getparent()==paragraph._p or index[start]<index[paragraph._p])
                    and index[list(paragraph._p.iter())[-1]]<index[end]):
                pairs.append((start,end,{'range_id':key,'kind':'text_interval'}))
        parent = paragraph._p.getparent()
        for key, policy in columns.items():
            if key in self.allowed and parent in policy['cells']:
                pairs.append((policy['start'], policy['end'], {
                    'range_id': key, 'kind': 'physical_table_columns',
                    'column_first': policy['column_first'], 'column_last': policy['column_last'],
                    'row_first': policy['row_first'], 'row_last': policy['row_last'],
                    'physical_cell': list(parent.getparent().findall(qn('w:tc'))).index(parent) + 1}))
        if len(pairs)!=1:
            raise TemplateFillError('当前填写位置不在唯一、明确授权的编辑权限范围内；请使用标准 Word')
        return pairs[0]

    def authorize_append(self, paragraph):
        """Non-mutating preflight; export checks every append before any write."""
        return deepcopy(self._append_permission(paragraph)[2])

    def append(self,paragraph,value):
        """Append within the unique explicit permission, preserving its markers."""
        start,end,_=self._append_permission(paragraph)
        template=next((r for r in reversed(paragraph.runs)if r._r.rPr is not None),None)
        run=paragraph.add_run(value)
        if template is not None:run._r.insert(0,deepcopy(template._r.rPr))
        if end.getparent()==paragraph._p:
            paragraph._p.remove(run._r);end.addprevious(run._r)
        return run


def readonly_guard(document):
    p=document.settings._element.find(qn('w:documentProtection'))
    if p is not None and p.get(qn('w:enforcement'),'0').lower() in {'1','true','on'}:
        return ReadonlyRangeGuard(document)
    return None
