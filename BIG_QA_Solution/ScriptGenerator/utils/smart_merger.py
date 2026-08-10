"""
Structure-aware merging of newly generated code into an existing source file.

The merge keeps the existing file verbatim and splices in only the members the
generated file adds. Python is merged from a real ``ast`` parse, so a member is
always a complete source span (decorators + signature + body). Brace languages
(JS/TS/Java/C#) use a brace/paren balancing scanner instead of per-line regexes,
so a method body can never be split away from its signature.

The older line-classifying implementation is kept as ``_legacy_*`` and is only
used when a file cannot be parsed at all.
"""

import ast
import os
import re


# --------------------------------------------------------------------------- #
# Generic helpers
# --------------------------------------------------------------------------- #

def _normalize_ws(text: str) -> str:
    return re.sub(r'\s+', ' ', text.strip())


def _reindent(lines, delta):
    """Shift a block of lines by ``delta`` columns, leaving blank lines empty."""
    if not delta:
        return list(lines)
    out = []
    for line in lines:
        if not line.strip():
            out.append('')
        elif delta > 0:
            out.append(' ' * delta + line)
        else:
            drop = 0
            while drop < -delta and drop < len(line) and line[drop] in ' \t':
                drop += 1
            out.append(line[drop:])
    return out


def _strip_trailing_blanks(lines):
    out = list(lines)
    while out and not out[-1].strip():
        out.pop()
    return out


def _splice_after_code(member, groups, blank_lines):
    """
    Insert ``groups`` into ``member`` directly after its last line of code, i.e.
    ahead of the blank lines that separate it from whatever follows. Splicing
    there (instead of appending at the end of the block) is what keeps the
    original spacing of the existing file intact.
    """
    add = []
    for group in groups:
        group = _strip_trailing_blanks(group)
        if not group:
            continue
        if add or member['code_len'] > 0:
            add.extend([''] * blank_lines)
        add.extend(group)
    if not add:
        return
    pos = max(0, min(member['code_len'], len(member['lines'])))
    member['lines'] = member['lines'][:pos] + add + member['lines'][pos:]
    member['code_len'] = pos + len(add)


def _detect_newline(*sources):
    for src in sources:
        if '\r\n' in src:
            return '\r\n'
    return '\n'


# --------------------------------------------------------------------------- #
# Python
# --------------------------------------------------------------------------- #

def _py_is_docstring(stmt):
    return (isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str))


def _py_target_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _py_target_name(node.value)
        return '{}.{}'.format(base, node.attr) if base else node.attr
    if isinstance(node, (ast.Tuple, ast.List)):
        names = [_py_target_name(e) for e in node.elts]
        if all(names):
            return ', '.join(names)
    return None


def _py_unparse(stmt, seg_lines):
    try:
        return _normalize_ws(ast.unparse(stmt))
    except Exception:
        return _normalize_ws(' '.join(l for l in seg_lines if l.strip()))


def _py_stmt_start(stmt, lines, floor):
    """
    0-based index of the first line of ``stmt``, including its decorators and
    the contiguous comment block directly above it. ``floor`` is the first index
    the search may claim (the line after the previous statement).
    """
    candidates = [stmt.lineno]
    for dec in getattr(stmt, 'decorator_list', None) or []:
        candidates.append(dec.lineno)
    idx = min(candidates) - 1
    while idx - 1 >= floor and lines[idx - 1].strip().startswith('#'):
        idx -= 1
    return max(idx, floor)


def _py_stmt_key(stmt, seg_lines):
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return 'import', _py_unparse(stmt, seg_lines)
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return 'func', stmt.name
    if isinstance(stmt, ast.ClassDef):
        return 'class', stmt.name
    if isinstance(stmt, ast.Assign):
        for target in stmt.targets:
            name = _py_target_name(target)
            if name:
                return 'assign', name
    if isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        name = _py_target_name(stmt.target)
        if name:
            return 'assign', name
    if _py_is_docstring(stmt):
        return 'docstring', 'docstring'
    return 'other', _py_unparse(stmt, seg_lines)


def _py_segment_body(body, lines, region_end, floor):
    """
    Turn a list of statements into verbatim, gap-preserving spans. Segment *i*
    covers ``[start_i, start_i+1)`` so the blank lines between statements stay
    attached and the file round-trips unchanged when nothing is added.
    """
    starts = []
    cursor = floor
    for stmt in body:
        start = _py_stmt_start(stmt, lines, cursor)
        starts.append(start)
        cursor = stmt.end_lineno

    segments = []
    for i, stmt in enumerate(body):
        start = starts[i]
        end = starts[i + 1] if i + 1 < len(body) else region_end
        end = max(end, stmt.end_lineno)
        seg_lines = lines[start:end]
        kind, key = _py_stmt_key(stmt, seg_lines)
        first_code = min([stmt.lineno]
                         + [d.lineno for d in getattr(stmt, 'decorator_list', None) or []])
        segments.append({
            'kind': kind,
            'key': key,
            'lines': seg_lines,
            'node': stmt,
            'start': start,
            'end': end,
            'code_start': (first_code - 1) - start,
            'code_len': stmt.end_lineno - start,
        })
    return segments, (starts[0] if starts else region_end)


def _parse_py_module(content: str):
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, RecursionError):
        return None
    lines = content.split('\n')
    segments, first = _py_segment_body(tree.body, lines, len(lines), 0)
    return {'lines': lines, 'preamble': lines[:first], 'segments': segments}


def _parse_py_class(seg, lines):
    node = seg['node']
    body = list(node.body)
    doc = body[0] if body and _py_is_docstring(body[0]) else None
    rest = [s for s in body if s is not doc]

    floor = doc.end_lineno if doc is not None else node.lineno
    members, first_member = _py_segment_body(rest, lines, seg['end'], floor)

    indent = body[0].col_offset if body else node.col_offset + 4
    return {
        'header': lines[seg['start']:first_member],
        'members': members,
        'indent': indent,
        'node': node,
    }


def _py_self_assigned_names(node):
    names = set()
    for sub in ast.walk(node):
        targets = []
        if isinstance(sub, ast.Assign):
            targets = sub.targets
        elif isinstance(sub, (ast.AnnAssign, ast.AugAssign)):
            targets = [sub.target]
        for target in targets:
            name = _py_target_name(target)
            if name and name.startswith('self.'):
                names.add(name)
    return names


def _py_new_self_assignments(ex_node, gen_node, gen_lines, delta):
    """Locators/fields the generated ``__init__`` adds on top of the existing one."""
    known = _py_self_assigned_names(ex_node)
    extra = []
    for stmt in gen_node.body:
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
        elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
            targets = [stmt.target]
        else:
            continue
        names = [n for n in (_py_target_name(t) for t in targets)
                 if n and n.startswith('self.')]
        if not names or any(n in known for n in names):
            continue
        start = _py_stmt_start(stmt, gen_lines, gen_node.lineno)
        extra.extend(_reindent(gen_lines[start:stmt.end_lineno], delta))
        known.update(names)
    return extra


def _py_import_from_id(node):
    return (node.level, node.module or '')


def _py_alias_spec(alias):
    return '{} as {}'.format(alias.name, alias.asname) if alias.asname else alias.name


def _py_extend_import_from(seg, gen_node):
    """
    Fold the names of a generated ``from X import ...`` into the matching import
    already in the file. Emitting a second import of the same module would be
    merely redundant in Python but is a redeclaration error in JS/TS, so both
    languages take the same route: extend, never duplicate.
    """
    node = seg['node']
    spec = seg.get('import_spec') or [_py_alias_spec(a) for a in node.names]
    missing = [_py_alias_spec(a) for a in gen_node.names
               if _py_alias_spec(a) not in spec]
    if not missing:
        return
    spec = spec + missing
    seg['import_spec'] = spec

    first = seg['lines'][seg['code_start']]
    indent = first[:len(first) - len(first.lstrip())]
    text = '{}from {}{} import {}'.format(
        indent, '.' * node.level, node.module or '', ', '.join(spec))
    seg['lines'] = (seg['lines'][:seg['code_start']] + [text]
                    + seg['lines'][seg['code_len']:])
    seg['code_len'] = seg['code_start'] + 1


def _merge_py_class(ex_seg, ex_lines, gen_seg, gen_lines):
    ex_cls = _parse_py_class(ex_seg, ex_lines)
    gen_cls = _parse_py_class(gen_seg, gen_lines)
    delta = ex_cls['indent'] - gen_cls['indent']

    members = ex_cls['members']
    if not members:
        # Nothing but a header/docstring to anchor to - append every member.
        out = _strip_trailing_blanks(ex_cls['header'])
        for member in gen_cls['members']:
            out.append('')
            out.extend(_strip_trailing_blanks(_reindent(member['lines'], delta)))
        return out + ['']

    ex_keys = {(m['kind'], m['key']) for m in members}

    ex_init = next((m for m in members
                    if m['kind'] == 'func' and m['key'] == '__init__'), None)
    gen_init = next((m for m in gen_cls['members']
                     if m['kind'] == 'func' and m['key'] == '__init__'), None)
    if ex_init is not None and gen_init is not None:
        extra = _py_new_self_assignments(ex_init['node'], gen_init['node'],
                                         gen_lines, delta)
        if extra:
            _splice_after_code(ex_init, [extra], 0)

    new_fields, new_init, new_methods = [], [], []
    for member in gen_cls['members']:
        if (member['kind'], member['key']) in ex_keys:
            continue
        block = _reindent(member['lines'], delta)
        if member['kind'] == 'assign':
            new_fields.append(block)
        elif member['kind'] == 'func' and member['key'] == '__init__':
            new_init.append(block)
        else:
            new_methods.append(block)

    if new_fields:
        anchor = None
        for member in members:
            if member['kind'] == 'assign':
                anchor = member
        if anchor is not None:
            _splice_after_code(anchor, new_fields, 0)
        else:
            head = []
            for block in new_fields:
                head.extend(_strip_trailing_blanks(block))
            head.append('')
            members[0]['lines'] = head + members[0]['lines']
            members[0]['code_len'] += len(head)

    if new_init:
        _splice_after_code(members[0], new_init, 1)

    if new_methods:
        _splice_after_code(members[-1], new_methods, 1)

    out = list(ex_cls['header'])
    for member in members:
        out.extend(member['lines'])
    return out


def smart_merge_python(existing_content: str, generated_content: str) -> str:
    ex = _parse_py_module(existing_content)
    gen = _parse_py_module(generated_content)
    if ex is None or gen is None:
        return _legacy_smart_merge_python(existing_content, generated_content)

    if not ex['segments']:
        return generated_content
    if not gen['segments']:
        return existing_content

    ex_lines, gen_lines = ex['lines'], gen['lines']
    ex_keys = {(s['kind'], s['key']) for s in ex['segments']}

    ex_classes = [s for s in ex['segments'] if s['kind'] == 'class']
    gen_classes = [s for s in gen['segments'] if s['kind'] == 'class']
    by_name = {s['key']: s for s in gen_classes}
    # A single class on each side is the page-object case: merge into it even
    # when the generator picked a different class name.
    renamed = (len(ex_classes) == 1 and len(gen_classes) == 1
               and ex_classes[0]['key'] != gen_classes[0]['key'])
    if renamed:
        by_name = {ex_classes[0]['key']: gen_classes[0]}

    blocks = []
    last_import = None
    last_def = None
    import_from = {}
    for seg in ex['segments']:
        if seg['kind'] == 'class' and seg['key'] in by_name:
            lines = _merge_py_class(seg, ex_lines, by_name[seg['key']], gen_lines)
            code_len = len(_strip_trailing_blanks(lines))
            block = {'lines': lines, 'code_len': code_len}
        else:
            block = dict(seg, lines=list(seg['lines']))
        blocks.append(block)
        if seg['kind'] == 'import':
            last_import = block
            if isinstance(seg['node'], ast.ImportFrom):
                import_from.setdefault(_py_import_from_id(seg['node']), block)
        if seg['kind'] in ('func', 'class', 'assign'):
            last_def = block

    merged_class_keys = {gs['key'] for gs in by_name.values()}
    new_imports, new_defs = [], []
    for seg in gen['segments']:
        if seg['kind'] == 'import':
            target = (import_from.get(_py_import_from_id(seg['node']))
                      if isinstance(seg['node'], ast.ImportFrom) else None)
            if target is not None:
                _py_extend_import_from(target, seg['node'])
                continue
            if ('import', seg['key']) not in ex_keys:
                new_imports.append(seg['lines'])
        elif seg['kind'] in ('func', 'class', 'assign', 'other'):
            if seg['kind'] == 'class' and seg['key'] in merged_class_keys:
                continue
            if (seg['kind'], seg['key']) not in ex_keys:
                new_defs.append(seg['lines'])

    if new_imports:
        if last_import is not None:
            _splice_after_code(last_import, new_imports, 0)
        else:
            head = []
            for block in new_imports:
                head.extend(_strip_trailing_blanks(block))
            head.append('')
            blocks[0]['lines'] = head + blocks[0]['lines']
            blocks[0]['code_len'] += len(head)

    if new_defs:
        anchor = last_def if last_def is not None else blocks[-1]
        _splice_after_code(anchor, new_defs, 2)

    out = list(ex['preamble'])
    for block in blocks:
        out.extend(block['lines'])
    return '\n'.join(out).rstrip('\n') + '\n'


# --------------------------------------------------------------------------- #
# Brace languages (JS / TS / Java / C#)
# --------------------------------------------------------------------------- #

_CLASS_RE = re.compile(
    r'(?:^|\s)(?:abstract\s+|final\s+|sealed\s+|static\s+|partial\s+|'
    r'public\s+|private\s+|protected\s+|internal\s+|export\s+|default\s+)*'
    r'class\s+(?P<name>[A-Za-z_$][\w$]*)')

_MODIFIERS = {
    'public', 'private', 'protected', 'internal', 'static', 'final', 'abstract',
    'async', 'await', 'export', 'default', 'readonly', 'override', 'virtual',
    'sealed', 'partial', 'synchronized', 'native', 'transient', 'volatile',
    'const', 'let', 'var', 'declare', 'get', 'set', 'new', 'return',
}

# Trailing characters that mean the statement continues on the next line.
_CONTINUATION_CHARS = ('+', '-', '*', '/', '%', '=', '&', '|', '?', ':', ',',
                       '.', '<', '(', '[', '{')


# A '/' only starts a regex literal in a value position. Without this a regex
# such as /\}/ or /['"]/ throws the brace and bracket counters off, which used
# to truncate the enclosing method.
_REGEX_PREFIX = set('(,=:[!&|?{};+-*%~^<>')


def _scan_regex(line, start):
    """Index just past a regex literal beginning at ``start``, else ``None``."""
    i, n = start + 1, len(line)
    in_class = False
    while i < n:
        char = line[i]
        if char == '\\':
            i += 2
            continue
        if in_class:
            if char == ']':
                in_class = False
        elif char == '[':
            in_class = True
        elif char == '/':
            i += 1
            while i < n and line[i].isalpha():
                i += 1
            return i
        i += 1
    return None


def _scan_code_line(line, in_block, in_template, ignore):
    """
    Blank out comments, strings and regex literals, replacing them with spaces
    so column positions still line up with the raw source.
    Returns ``(masked, in_block, in_template, unterminated_quote_column)``.
    """
    out = list(line)
    i, n = 0, len(line)
    prev = None
    quote = '`' if in_template else None
    quote_col = None

    def blank(a, b):
        for k in range(max(a, 0), min(b, n)):
            out[k] = ' '

    while i < n:
        char = line[i]
        nxt = line[i + 1] if i + 1 < n else ''
        if in_block:
            if char == '*' and nxt == '/':
                blank(i, i + 2)
                in_block = False
                i += 2
                continue
            blank(i, i + 1)
            i += 1
            continue
        if quote:
            if char == '\\':
                blank(i, i + 2)
                i += 2
                continue
            blank(i, i + 1)
            if char == quote:
                quote = None
                quote_col = None
            i += 1
            continue
        if char == '/' and nxt == '*':
            blank(i, i + 2)
            in_block = True
            i += 2
            continue
        if char == '/' and nxt == '/':
            blank(i, n)
            break
        if char == '/' and (prev is None or prev in _REGEX_PREFIX):
            end = _scan_regex(line, i)
            if end is not None:
                blank(i, end)
                prev = '/'
                i = end
                continue
        if char in ('"', "'", '`') and i not in ignore:
            quote = char
            quote_col = i
            blank(i, i + 1)
            i += 1
            continue
        if not char.isspace():
            prev = char
        i += 1

    masked = ''.join(out)
    if quote == '`':
        return masked, in_block, True, None
    return masked, in_block, False, quote_col


def _strip_code_noise(line: str, state: dict) -> str:
    """
    Mask strings and comments so brace counting cannot be fooled. An
    unterminated ' or " is almost always a misread apostrophe rather than a real
    string, so the line is rescanned with that quote treated as an ordinary
    character; only backticks legitimately span lines.
    """
    ignore = set()
    masked, in_block, in_template, bad = _scan_code_line(
        line, state['block_comment'], state.get('template', False), ignore)
    attempts = 0
    while bad is not None and attempts < 6:
        ignore.add(bad)
        attempts += 1
        masked, in_block, in_template, bad = _scan_code_line(
            line, state['block_comment'], state.get('template', False), ignore)
    state['block_comment'] = in_block
    state['template'] = in_template
    return masked


def _is_lead_line(raw: str) -> bool:
    return raw.startswith(('//', '/*', '*', '@'))


def _continues(code: str) -> bool:
    return code.endswith(_CONTINUATION_CHARS)


def _brace_member_key(code_lines):
    """Classify a member from its first line(s) of real code."""
    text = _normalize_ws(' '.join(l for l in code_lines if l.strip()))
    if not text:
        return 'other', ''
    if text.startswith('@'):
        text = _normalize_ws(re.sub(r'^(?:@[\w.]+(?:\([^)]*\))?\s*)+', '', text))
    if not text:
        return 'other', ''

    eq = text.find('=')
    paren = text.find('(')
    if paren > 0 and (eq < 0 or eq > paren):
        head = text[:paren]
        names = [i for i in re.findall(r'[A-Za-z_$][\w$]*', head)
                 if i not in _MODIFIERS]
        if names:
            return 'method', names[-1]

    cuts = [p for p in (eq, text.find(':'), text.find(';')) if p > 0]
    head = text[:min(cuts)] if cuts else text
    names = [i for i in re.findall(r'[A-Za-z_$][\w$]*', head)
             if i not in _MODIFIERS]
    if names:
        return 'field', names[-1]
    return 'other', text


def _scan_brace_members(lines, code, open_idx):
    """
    Walk a class body, returning ``([(start, code_end), ...], close_idx)``.
    A member always spans from its leading comments/annotations through the line
    that brings the brace and paren depth back to class level.
    """
    depth = code[open_idx].count('{') - code[open_idx].count('}')
    members = []
    close_idx = len(lines)
    lead = None
    start = None
    paren = 0

    i = open_idx + 1
    while i < len(lines):
        stripped_code = code[i].strip()
        raw = lines[i].strip()

        if start is None:
            if not stripped_code or stripped_code.startswith('@'):
                if raw and _is_lead_line(raw):
                    if lead is None:
                        lead = i
                elif not raw:
                    lead = None
                i += 1
                continue
            if depth == 1 and stripped_code == '}':
                close_idx = i
                break
            start = lead if lead is not None else i
            lead = None

        depth += code[i].count('{') - code[i].count('}')
        paren += (code[i].count('(') - code[i].count(')')
                  + code[i].count('[') - code[i].count(']'))

        if depth < 1:
            # The class closes here. _normalize_class_close has already given
            # that brace its own line, so the member must end above it -
            # otherwise the closer would be duplicated and new methods would be
            # spliced inside the last method.
            close_idx = i
            if stripped_code != '}':
                members.append((start, i + 1))
                close_idx = i + 1
            elif start < i:
                members.append((start, i))
            start = None
            break
        if depth == 1 and paren <= 0 and not _continues(stripped_code):
            members.append((start, i + 1))
            start = None
            paren = 0
        i += 1

    if start is not None:
        members.append((start, min(i + 1, len(lines))))
    return members, close_idx


def _code_lines(lines):
    state = {'block_comment': False, 'template': False}
    return [_strip_code_noise(l, state) for l in lines]


def _normalize_class_close(lines, code, open_idx, indent):
    """
    Give the brace that closes the class a line of its own. When it shares a
    line with a member's closing brace (``  }}``) neither the member nor the
    class owns it, and new methods get spliced inside the last method.
    """
    depth = 0
    for i in range(open_idx, len(code)):
        for col, char in enumerate(code[i]):
            if char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
                if depth > 0:
                    continue
                if not code[i][:col].strip():
                    return lines, code
                split = (lines[:i] + [lines[i][:col].rstrip(),
                                      indent + lines[i][col:]] + lines[i + 1:])
                return split, _code_lines(split)
    return lines, code


def _parse_brace_file(content: str):
    lines = content.split('\n')
    code = _code_lines(lines)

    def no_class():
        return {'lines': lines, 'code': code, 'name': None, 'class_count': 0,
                'sound': True, 'class_start': None, 'close_idx': None,
                'prefix': lines, 'prefix_code': code,
                'header': [], 'members': [], 'closer': [], 'footer': [],
                'indent': 0}

    class_idx = None
    class_name = None
    class_count = 0
    for i, line in enumerate(code):
        match = _CLASS_RE.search(line)
        if match:
            class_count += 1
            if class_idx is None:
                class_idx = i
                class_name = match.group('name')

    if class_idx is None:
        return no_class()

    open_idx = None
    for i in range(class_idx, len(code)):
        if '{' in code[i]:
            open_idx = i
            break
    if open_idx is None:
        return no_class()

    class_indent = lines[class_idx][:len(lines[class_idx])
                                    - len(lines[class_idx].lstrip())]
    lines, code = _normalize_class_close(lines, code, open_idx, class_indent)

    class_start = class_idx
    while class_start > 0 and _is_lead_line(lines[class_start - 1].strip()):
        class_start -= 1

    spans, close_idx = _scan_brace_members(lines, code, open_idx)
    first_member = spans[0][0] if spans else close_idx

    members = []
    for idx, (start, code_end) in enumerate(spans):
        if idx + 1 < len(spans):
            end = max(spans[idx + 1][0], code_end)
        else:
            # never let the last member swallow the class's closing brace
            end = close_idx if close_idx >= code_end else code_end
        kind, key = _brace_member_key(code[start:code_end])
        members.append({
            'kind': kind,
            'key': key,
            'lines': lines[start:end],
            'code_lines': code[start:code_end],
            'code_len': code_end - start,
        })

    indent = 4
    if spans:
        first_code = next((l for l in lines[spans[0][0]:spans[0][1]] if l.strip()),
                          None)
        if first_code is not None:
            indent = len(first_code) - len(first_code.lstrip())

    # The member spans must tile the class exactly. If they do not, the brace
    # scan misread something and rebuilding the class would drop or duplicate
    # code, so callers keep the file as-is instead.
    rebuilt = list(lines[class_start:first_member])
    for member in members:
        rebuilt.extend(member['lines'])
    rebuilt.extend(lines[close_idx:close_idx + 1])
    sound = rebuilt == lines[class_start:close_idx + 1]

    return {
        'lines': lines,
        'code': code,
        'name': class_name,
        'sound': sound,
        'class_count': class_count,
        'class_start': class_start,
        'close_idx': close_idx,
        'prefix': lines[:class_start],
        'prefix_code': code[:class_start],
        'header': lines[class_start:first_member],
        'members': members,
        'closer': lines[close_idx:close_idx + 1],
        'footer': lines[close_idx:],
        'indent': indent,
    }


def _group_statements(lines, code):
    """Split free-standing (non class body) lines into whole statements."""
    groups = []
    lead = None
    start = None
    depth = paren = 0
    for i, raw_code in enumerate(code):
        stripped = raw_code.strip()
        raw = lines[i].strip()
        if start is None:
            if not stripped:
                if raw and _is_lead_line(raw):
                    if lead is None:
                        lead = i
                elif not raw:
                    lead = None
                continue
            if stripped.startswith('@'):
                # A decorator/annotation belongs to the statement below it; on
                # its own it would balance and split the class off from it.
                if lead is None:
                    lead = i
                paren += (raw_code.count('(') - raw_code.count(')')
                          + raw_code.count('[') - raw_code.count(']'))
                continue
            start = lead if lead is not None else i
            lead = None
        depth += raw_code.count('{') - raw_code.count('}')
        paren += (raw_code.count('(') - raw_code.count(')')
                  + raw_code.count('[') - raw_code.count(']'))
        if stripped and depth <= 0 and paren <= 0 and not _continues(stripped):
            groups.append((start, i + 1))
            start = None
            depth = paren = 0
    if start is not None:
        groups.append((start, len(lines)))
    return groups


_IMPORT_RE = re.compile(r'^\s*(import\b|using\b|package\b|(?:const|let|var)\s+'
                        r'[\w${},\s]+=\s*require\s*\()')


def _statement_text(raw_slice):
    """
    Flatten a statement to one line of raw source. Import handling needs the
    real text (the code-stripped copy has its module strings blanked out), so
    only comment lines are dropped here.
    """
    return _normalize_ws(' '.join(
        l for l in raw_slice
        if l.strip() and not l.strip().startswith(('//', '*', '/*'))))


_DECL_RE = re.compile(
    r'^(?:export\s+|default\s+|declare\s+|async\s+|abstract\s+|final\s+|'
    r'static\s+|sealed\s+|partial\s+|public\s+|private\s+|protected\s+|'
    r'internal\s+)*'
    r'(?:function|class|const|let|var|type|interface|enum|record)\s+'
    r'(?P<name>[A-Za-z_$][\w$]*)')

# Top level call such as When('...', fn), test('...', fn), test.describe('...')
_CALL_RE = re.compile(
    r'^(?P<callee>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\(\s*'
    r'(?:(?P<q>[\'"`])(?P<arg>(?:\\.|(?!(?P=q)).)*)(?P=q))?')


def _statement_key(text):
    if _IMPORT_RE.match(text):
        return 'import', re.sub(r'[;\'"]', '', text)

    # decorators/annotations are part of the statement, not its identity
    text = _normalize_ws(re.sub(r'^(?:@[\w.]+(?:\([^)]*\))?\s*)+', '', text))

    match = _DECL_RE.match(text)
    if match:
        return 'decl', match.group('name')

    # Cucumber steps and Playwright tests are top level calls whose identity is
    # the callee plus its first string argument, e.g.
    #   When('the user selects a valid value from the "Type" dropdown', fn)
    # Keying on the whole statement text instead would treat an edited body as
    # a different step and duplicate it.
    match = _CALL_RE.match(text)
    if match:
        if match.group('arg') is not None:
            return 'call', '{}({})'.format(match.group('callee'),
                                           _normalize_ws(match.group('arg')))
        return 'call', re.sub(r'[;\'"]', '', text)

    return 'other', re.sub(r'[;\'"]', '', text)


_ES_IMPORT_RE = re.compile(
    r'^import\s+(?P<clause>.*?)\s+from\s+[\'"](?P<module>[^\'"]+)[\'"]')


def _parse_es_import(text):
    """
    Split ``import Default, { a, b as c } from 'mod'`` into its parts. Returns
    ``None`` for anything that is not a name-binding ES import (side-effect
    imports, ``require``, Java/C# imports).
    """
    match = _ES_IMPORT_RE.match(text.strip())
    if not match:
        return None
    clause = match.group('clause').strip()
    named, default, namespace = [], None, None
    brace = re.search(r'\{(.*?)\}', clause)
    if brace:
        named = [_normalize_ws(p) for p in brace.group(1).split(',') if p.strip()]
        clause = (clause[:brace.start()] + clause[brace.end():])
    for part in clause.split(','):
        part = part.strip()
        if not part:
            continue
        if part.startswith('*'):
            namespace = _normalize_ws(part)
        else:
            default = part
    return {'module': match.group('module'), 'default': default,
            'namespace': namespace, 'named': named}


def _render_es_import(parsed, indent, semicolon):
    clause = []
    if parsed['default']:
        clause.append(parsed['default'])
    if parsed['namespace']:
        clause.append(parsed['namespace'])
    if parsed['named']:
        clause.append('{ %s }' % ', '.join(parsed['named']))
    return "{}import {} from '{}'{}".format(
        indent, ', '.join(clause), parsed['module'], ';' if semicolon else '')


def _find_body_span(code_slice):
    """
    Locate a statement's callback body: ``(open_line, close_line)`` where
    ``close_line`` holds the brace that closes it. ``None`` when the statement
    has no multi-line brace body.
    """
    depth = 0
    open_line = None
    for i, line in enumerate(code_slice):
        opens = line.count('{')
        closes = line.count('}')
        if open_line is None:
            if opens > closes:
                open_line = i
                depth = opens - closes
            continue
        depth += opens - closes
        if depth <= 0:
            return (open_line, i) if i > open_line + 1 else None
    return None


def _is_container_statement(lines, code):
    """
    True for statements that hold other statements - ``describe(...)`` blocks
    and function declarations. Only these get their bodies merged; splicing
    loose statements into, say, a step definition body would scramble it.
    """
    kind, _ = _statement_key(_statement_text(lines))
    if kind == 'decl':
        return True
    return kind == 'call' and _find_body_span(code) is not None


def _merge_nested_bodies(ex_lines, ex_code, gen_lines, gen_code, depth_left=4):
    """
    Merge two matching container statements, e.g. a ``describe`` block present
    in both files where the generated one has extra ``test`` cases. Without this
    the existing block would be kept verbatim and the new cases lost silently.
    """
    if depth_left <= 0:
        return ex_lines
    ex_body = _find_body_span(ex_code)
    gen_body = _find_body_span(gen_code)
    if ex_body is None or gen_body is None:
        return ex_lines

    ex_open, ex_close = ex_body
    gen_open, gen_close = gen_body
    inner = ex_lines[ex_open + 1:ex_close]
    inner_code = ex_code[ex_open + 1:ex_close]
    gen_inner = gen_lines[gen_open + 1:gen_close]
    gen_inner_code = gen_code[gen_open + 1:gen_close]

    ex_groups = _group_statements(inner, inner_code)
    gen_groups = _group_statements(gen_inner, gen_inner_code)
    if not ex_groups or not gen_groups:
        return ex_lines

    def indent_of(lines_):
        first = next((l for l in lines_ if l.strip()), '')
        return len(first) - len(first.lstrip())

    delta = indent_of(inner) - indent_of(gen_inner)

    gen_map = {}
    for start, end in gen_groups:
        key = _statement_key(_statement_text(gen_inner[start:end]))
        gen_map.setdefault(key, (start, end))

    ex_keys = set()
    for start, end in ex_groups:
        ex_keys.add(_statement_key(_statement_text(inner[start:end])))

    # Matched children first, back to front so earlier spans stay valid.
    changed = False
    for start, end in reversed(ex_groups):
        key = _statement_key(_statement_text(inner[start:end]))
        span = gen_map.get(key)
        if span is None or not _is_container_statement(inner[start:end],
                                                       inner_code[start:end]):
            continue
        gs, ge = span
        merged = _merge_nested_bodies(inner[start:end], inner_code[start:end],
                                      gen_inner[gs:ge], gen_inner_code[gs:ge],
                                      depth_left - 1)
        if merged != inner[start:end]:
            inner[start:end] = merged
            changed = True

    additions = []
    for start, end in gen_groups:
        key = _statement_key(_statement_text(gen_inner[start:end]))
        if key in ex_keys:
            continue
        if not _is_container_statement(gen_inner[start:end],
                                       gen_inner_code[start:end]):
            continue
        block = _strip_trailing_blanks(gen_inner[start:end])
        if block:
            additions.append(_reindent(block, delta))

    if additions:
        tail = len(inner)
        while tail > 0 and not inner[tail - 1].strip():
            tail -= 1
        add = []
        for block in additions:
            add.append('')
            add.extend(block)
        inner = inner[:tail] + add + inner[tail:]
        changed = True

    if not changed:
        return ex_lines
    return ex_lines[:ex_open + 1] + inner + ex_lines[ex_close:]


def _rewrite_es_import(block, parsed):
    """
    Replace an import statement in ``block`` with its re-rendered form.
    Returns the original lines it replaced.
    """
    lead = 0
    for idx, line in enumerate(block['lines'][:block['code_len']]):
        stripped = line.strip()
        if stripped and not stripped.startswith(('//', '/*', '*')):
            lead = idx
            break
    first = block['lines'][lead]
    indent = first[:len(first) - len(first.lstrip())]
    rendered = _render_es_import(parsed, indent, first.rstrip().endswith(';'))
    replaced = block['lines'][lead:block['code_len']]
    block['lines'] = (block['lines'][:lead] + [rendered]
                      + block['lines'][block['code_len']:])
    block['code_len'] = lead + 1
    return replaced


def _merge_brace_top_level(ex, gen, merged_class, gen_class_span):
    """
    Merge whole top level statements. Every statement - an import, a helper
    function, a class, or a ``When(...)`` / ``test(...)`` call - is kept as one
    complete brace-balanced span, so a step definition can never be reduced to
    its signature line.
    """
    ex_lines, gen_lines = ex['lines'], gen['lines']
    ex_groups = _group_statements(ex_lines, ex['code'])
    gen_groups = _group_statements(gen_lines, gen['code'])

    if not ex_groups:
        return list(gen_lines), []
    if not gen_groups:
        return list(ex_lines), []

    gen_map = {}
    for start, code_end in gen_groups:
        key = _statement_key(_statement_text(gen_lines[start:code_end]))
        gen_map.setdefault(key, (start, code_end))

    blocks = []
    last_import = None
    last_decl = None
    es_imports = {}          # module -> (block, parsed)
    for idx, (start, code_end) in enumerate(ex_groups):
        span_end = ex_groups[idx + 1][0] if idx + 1 < len(ex_groups) else len(ex_lines)
        span_end = max(span_end, code_end)
        text = _statement_text(ex_lines[start:code_end])
        kind, key = _statement_key(text)

        lines = ex_lines[start:span_end]
        code_len = code_end - start
        is_class = merged_class is not None and start <= ex['class_start'] < code_end
        if is_class:
            lines = (ex_lines[start:ex['class_start']] + merged_class
                     + ex_lines[code_end:span_end])
            code_len = (ex['class_start'] - start) + len(merged_class)
        elif kind == 'call' and (kind, key) in gen_map:
            # Same describe/suite on both sides: fold in its new children.
            gs, ge = gen_map[(kind, key)]
            merged = _merge_nested_bodies(
                ex_lines[start:code_end], ex['code'][start:code_end],
                gen_lines[gs:ge], gen['code'][gs:ge])
            if merged != ex_lines[start:code_end]:
                lines = merged + ex_lines[code_end:span_end]
                code_len = len(merged)

        block = {'kind': kind, 'key': key, 'lines': lines, 'code_len': code_len}
        blocks.append(block)
        if kind == 'import':
            last_import = block
            parsed = _parse_es_import(text)
            if parsed and parsed['module'] not in es_imports:
                es_imports[parsed['module']] = (block, parsed)
        if kind in ('decl', 'call') or is_class:
            last_decl = block

    ex_keys = {(b['kind'], b['key']) for b in blocks}
    new_imports, new_rest = [], []
    rewritten = []
    for start, code_end in gen_groups:
        if (gen_class_span is not None
                and start < gen_class_span[1] and code_end > gen_class_span[0]):
            continue         # already folded into the existing class
        text = _statement_text(gen_lines[start:code_end])
        kind, key = _statement_key(text)
        block = _strip_trailing_blanks(gen_lines[start:code_end])
        if not block:
            continue

        if kind == 'import':
            parsed = _parse_es_import(text)
            target = es_imports.get(parsed['module']) if parsed else None
            if target is not None:
                # Re-importing a module already imported would redeclare its
                # bindings, so fold the new names into the existing statement.
                t_block, t_parsed = target
                changed = False
                for name in parsed['named']:
                    if name not in t_parsed['named']:
                        t_parsed['named'].append(name)
                        changed = True
                for slot in ('default', 'namespace'):
                    if parsed[slot] and not t_parsed[slot]:
                        t_parsed[slot] = parsed[slot]
                        changed = True
                if changed and (t_block, t_parsed) not in rewritten:
                    rewritten.append((t_block, t_parsed))
                continue
            if (kind, key) not in ex_keys:
                new_imports.append(block)
                ex_keys.add((kind, key))
        elif (kind, key) not in ex_keys:
            new_rest.append(block)
            ex_keys.add((kind, key))

    replaced = []
    for block, parsed in rewritten:
        replaced.extend(_rewrite_es_import(block, parsed))

    if new_imports:
        if last_import is not None:
            _splice_after_code(last_import, new_imports, 0)
        else:
            head = []
            for block in new_imports:
                head.extend(block)
            head.append('')
            blocks[0]['lines'] = head + blocks[0]['lines']
            blocks[0]['code_len'] += len(head)

    if new_rest:
        # After the last declaration rather than the very last statement, so new
        # helpers land above a trailing `module.exports` / bootstrap call.
        _splice_after_code(last_decl if last_decl is not None else blocks[-1],
                           new_rest, 1)

    out = list(ex_lines[:ex_groups[0][0]])
    for block in blocks:
        out.extend(block['lines'])
    return out, replaced


def _brace_this_assignments(code_lines):
    return set(re.findall(r'this\.([\w$]+)\s*=(?!=)', ' '.join(code_lines)))


def _brace_new_this_statements(ex_ctor, gen_ctor, delta):
    known = _brace_this_assignments(ex_ctor['code_lines'])
    extra = []
    lines = gen_ctor['lines']
    code = gen_ctor['code_lines']
    i = 0
    while i < len(code):
        match = re.match(r'\s*this\.([\w$]+)\s*=(?!=)', code[i])
        if not match:
            i += 1
            continue
        start = i
        paren = 0
        while i < len(code):
            paren += (code[i].count('(') - code[i].count(')')
                      + code[i].count('[') - code[i].count(']')
                      + code[i].count('{') - code[i].count('}'))
            if paren <= 0 and not _continues(code[i].strip()):
                break
            i += 1
        end = min(i + 1, len(lines))
        name = match.group(1)
        if name not in known:
            known.add(name)
            extra.extend(_reindent(lines[start:end], delta))
        i = end
    return extra


def _merge_brace_class(ex, gen):
    delta = ex['indent'] - gen['indent']
    members = ex['members']
    ex_keys = {(m['kind'], m['key']) for m in members}

    ex_ctor = next((m for m in members if m['key'] == 'constructor'), None)
    gen_ctor = next((m for m in gen['members'] if m['key'] == 'constructor'), None)
    if ex_ctor is not None and gen_ctor is not None:
        extra = _brace_new_this_statements(ex_ctor, gen_ctor, delta)
        if extra:
            # Insert ahead of the constructor's own closing brace.
            pos = ex_ctor['code_len'] - 1
            while pos > 0 and not ex_ctor['lines'][pos].strip():
                pos -= 1
            ex_ctor['lines'] = (ex_ctor['lines'][:pos] + extra
                                + ex_ctor['lines'][pos:])
            ex_ctor['code_len'] += len(extra)

    new_fields, new_methods = [], []
    for member in gen['members']:
        if (member['kind'], member['key']) in ex_keys:
            continue
        block = _reindent(member['lines'], delta)
        if member['kind'] == 'field':
            new_fields.append(block)
        else:
            new_methods.append(block)

    if not members:
        out = _strip_trailing_blanks(ex['header'])
        for block in new_fields + new_methods:
            out.append('')
            out.extend(_strip_trailing_blanks(block))
        return out + [''] + list(ex['closer'])

    if new_fields:
        anchor = None
        for member in members:
            if member['kind'] == 'field':
                anchor = member
        if anchor is not None:
            _splice_after_code(anchor, new_fields, 0)
        else:
            head = []
            for block in new_fields:
                head.extend(_strip_trailing_blanks(block))
            head.append('')
            members[0]['lines'] = head + members[0]['lines']
            members[0]['code_len'] += len(head)

    if new_methods:
        _splice_after_code(members[-1], new_methods, 1)

    body = list(ex['header'])
    for member in members:
        body.extend(member['lines'])
    return _strip_trailing_blanks(body) + list(ex['closer'])


def _covers(original, merged):
    """True when every code line of ``original`` survives, in order, in ``merged``."""
    cursor = iter(merged)
    for line in original:
        if not line.strip():
            continue
        if not any(candidate.strip() == line.strip() for candidate in cursor):
            return False
    return True


def _smart_merge_brace(existing_content: str, generated_content: str,
                       legacy) -> str:
    ex = _parse_brace_file(existing_content)
    gen = _parse_brace_file(generated_content)

    merged_class = None
    gen_class_span = None
    # Merge class bodies when both sides have the same class, or when each side
    # has exactly one class and the generator simply renamed it.
    if (ex['name'] and gen['name'] and ex['sound'] and gen['sound']
            and (ex['name'] == gen['name']
                 or (ex['class_count'] == 1 and gen['class_count'] == 1))):
        merged_class = _merge_brace_class(ex, gen)
        gen_class_span = (gen['class_start'], gen['close_idx'] + 1)

    out, replaced = _merge_brace_top_level(ex, gen, merged_class, gen_class_span)

    # Last line of defence: if the structural scan misread the file badly enough
    # to drop something, fall back rather than hand back a mangled script.
    # Import statements we deliberately rewrote are expected to differ.
    rewritten = {line.strip() for line in replaced if line.strip()}
    kept = [line for line in ex['lines'] if line.strip() not in rewritten]
    if not out or not _covers(kept, out):
        return legacy(existing_content, generated_content)
    return '\n'.join(out).rstrip('\n') + '\n'


def smart_merge_jsts(existing_content: str, generated_content: str) -> str:
    return _smart_merge_brace(existing_content, generated_content,
                              _legacy_smart_merge_jsts)


def smart_merge_java(existing_content: str, generated_content: str) -> str:
    return _smart_merge_brace(existing_content, generated_content,
                              _legacy_smart_merge_java)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def smart_merge_code(existing_content: str, generated_content: str,
                     filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()

    newline = _detect_newline(existing_content, generated_content)
    existing = existing_content.replace('\r\n', '\n')
    generated = generated_content.replace('\r\n', '\n')

    if not existing.strip():
        res = generated
    elif not generated.strip():
        res = existing
    elif ext == '.py':
        res = smart_merge_python(existing, generated)
    elif ext in ('.js', '.ts', '.tsx', '.jsx', '.mjs', '.cjs'):
        res = smart_merge_jsts(existing, generated)
    elif ext in ('.java', '.cs'):
        res = smart_merge_java(existing, generated)
    else:
        # Unknown file type: append generated lines the existing file lacks.
        merged_lines = existing.split('\n')
        seen = set(merged_lines)
        for line in generated.split('\n'):
            if line.strip() and line not in seen:
                merged_lines.append(line)
                seen.add(line)
        res = '\n'.join(merged_lines)

    return res.replace('\n', newline) if newline != '\n' else res


# --------------------------------------------------------------------------- #
# Legacy line-based implementation (fallback for unparseable input)
# --------------------------------------------------------------------------- #

def extract_imports_robust(content: str, language: str):
    """
    Extracts import statements from the content. Handles multiline imports.
    Returns (import_blocks, remaining_content_text).
    """
    import_blocks = []
    remaining_lines = []

    lines = content.splitlines()
    in_multiline_import = False
    current_import = []

    lang = language.lower()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_multiline_import:
                current_import.append(line)
            else:
                remaining_lines.append(line)
            continue

        if in_multiline_import:
            current_import.append(line)
            # Check for end of multiline import
            if lang == 'python' and ')' in stripped:
                in_multiline_import = False
                import_blocks.append('\n'.join(current_import))
                current_import = []
            elif lang in ('js', 'ts', 'javascript', 'typescript') and ('}' in stripped or 'from' in stripped or ';' in stripped or stripped.endswith("'") or stripped.endswith('"')):
                # Check if it looks like the end of the import statement
                if ';' in stripped or "'" in stripped or '"' in stripped or 'from' in stripped:
                    in_multiline_import = False
                    import_blocks.append('\n'.join(current_import))
                    current_import = []
            continue

        # Detect start of import
        is_start_of_import = False
        if lang == 'python':
            if stripped.startswith('import ') or stripped.startswith('from '):
                is_start_of_import = True
                if '(' in stripped and ')' not in stripped:
                    in_multiline_import = True
        elif lang in ('js', 'ts', 'javascript', 'typescript'):
            if stripped.startswith('import ') or (('require(' in stripped) and any(stripped.startswith(p) for p in ('const ', 'var ', 'let '))):
                is_start_of_import = True
                if '{' in stripped and '}' not in stripped:
                    in_multiline_import = True
        elif lang in ('java', 'c#', 'cs'):
            if stripped.startswith('import ') or stripped.startswith('package ') or stripped.startswith('using '):
                is_start_of_import = True

        if is_start_of_import:
            if in_multiline_import:
                current_import.append(line)
            else:
                import_blocks.append(line)
        else:
            remaining_lines.append(line)

    if current_import:
        import_blocks.append('\n'.join(current_import))

    return import_blocks, '\n'.join(remaining_lines)


def merge_imports(existing_imports, new_imports):
    """
    Merges imports from existing and generated code uniquely.
    """
    merged = list(existing_imports)

    def normalize(imp):
        # strip spaces, tabs, semicolons, quotes, parentheses to normalize imports
        return re.sub(r'[\s;\'"\(\)]', '', imp).lower()

    existing_normalized = {normalize(imp) for imp in existing_imports}

    for imp in new_imports:
        if normalize(imp) not in existing_normalized:
            merged.append(imp)
            existing_normalized.add(normalize(imp))

    return merged


def parse_python_class(content: str) -> dict:
    """
    Parses Python content to identify class header, docstring, class-level locators,
    methods, and other helper/misc lines inside the class.
    """
    lines = content.splitlines()

    class_def = None
    class_indent = 0
    docstring_lines = []
    class_locators = []      # list of (name, full_line_text)
    methods = {}             # name -> list of lines
    other_class_lines = []   # comments or other class-level lines
    outside_lines = []       # lines outside class

    current_method = None
    current_method_indent = 0
    current_method_lines = []

    in_class = False
    in_docstring = False
    docstring_delim = None

    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        # Detect class definition
        if not in_class:
            if stripped.startswith("class ") and stripped.endswith(":"):
                in_class = True
                class_def = line
                class_indent = indent
                continue
            outside_lines.append(line)
            continue

        # If we are in class, check indentation
        if in_class:
            if stripped and indent <= class_indent:
                in_class = False
                if current_method:
                    methods[current_method] = current_method_lines
                    current_method = None
                outside_lines.append(line)
                continue

        # Handle docstrings immediately inside class
        if in_class and not class_locators and not methods and not current_method:
            if not in_docstring:
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    in_docstring = True
                    docstring_delim = '"""' if stripped.startswith('"""') else "'''"
                    docstring_lines.append(line)
                    if stripped.endswith(docstring_delim) and len(stripped) > 3:
                        in_docstring = False
                    continue
            else:
                docstring_lines.append(line)
                if stripped.endswith(docstring_delim):
                    in_docstring = False
                continue

        # Handle method blocks
        if stripped.startswith("def ") or stripped.startswith("async def "):
            if current_method:
                methods[current_method] = current_method_lines
                current_method = None

            method_name = stripped.split("(")[0].replace("async ", "").replace("def ", "").strip()
            current_method = method_name
            current_method_indent = indent
            current_method_lines = [line]
            continue

        if current_method:
            if not stripped or indent > current_method_indent or stripped.startswith("#"):
                current_method_lines.append(line)
                continue
            methods[current_method] = current_method_lines
            current_method = None

        # If we are not inside a method, handle class-level attributes
        if in_class and not current_method and not in_docstring:
            match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=', stripped)
            if match:
                loc_name = match.group(1)
                class_locators.append((loc_name, line))
            else:
                if stripped:
                    other_class_lines.append(line)

    if current_method:
        methods[current_method] = current_method_lines

    return {
        'class_def': class_def,
        'docstring': docstring_lines,
        'locators': class_locators,
        'methods': methods,
        'other_class_lines': other_class_lines,
        'outside_lines': outside_lines
    }


def _legacy_smart_merge_python(existing_content: str, generated_content: str) -> str:
    existing_imports, existing_body = extract_imports_robust(existing_content, 'python')
    generated_imports, generated_body = extract_imports_robust(generated_content, 'python')
    merged_imports = merge_imports(existing_imports, generated_imports)

    existing_class = parse_python_class(existing_body)
    generated_class = parse_python_class(generated_body)

    # Merge outside lines
    merged_outside = list(existing_class['outside_lines'])
    for line in generated_class['outside_lines']:
        if line.strip() and line not in merged_outside:
            merged_outside.append(line)

    if not existing_class['class_def'] and not generated_class['class_def']:
        return '\n'.join(merged_imports) + '\n\n' + '\n'.join(merged_outside)

    if not existing_class['class_def']:
        return '\n'.join(merged_imports) + '\n\n' + generated_body

    if not generated_class['class_def']:
        return '\n'.join(merged_imports) + '\n\n' + existing_body

    class_def = existing_class['class_def']
    docstring = existing_class['docstring'] if existing_class['docstring'] else generated_class['docstring']

    # Locators (Class-level)
    existing_loc_names = {name for name, _ in existing_class['locators']}
    merged_locators = list(existing_class['locators'])
    for name, line in generated_class['locators']:
        if name not in existing_loc_names:
            merged_locators.append((name, line))
            existing_loc_names.add(name)

    # Other class lines
    merged_other_class = list(existing_class['other_class_lines'])
    for line in generated_class['other_class_lines']:
        if line.strip() and line not in merged_other_class:
            merged_other_class.append(line)

    # Methods
    existing_methods = existing_class['methods']
    generated_methods = generated_class['methods']
    merged_methods = {}

    # Merge __init__ specifically if present
    if '__init__' in existing_methods and '__init__' in generated_methods:
        init_existing = existing_methods['__init__']
        init_generated = generated_methods['__init__']

        def parse_self_assignments(lines):
            assignments = []
            others = []
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("self.") and "=" in stripped:
                    name = stripped.split("=")[0].replace("self.", "").strip()
                    assignments.append((name, line))
                else:
                    if not stripped.startswith("def __init__"):
                        others.append(line)
            return assignments, others

        exist_assign, exist_other = parse_self_assignments(init_existing)
        gen_assign, gen_other = parse_self_assignments(init_generated)

        exist_assign_names = {name for name, _ in exist_assign}
        merged_assign = list(exist_assign)
        for name, line in gen_assign:
            if name not in exist_assign_names:
                merged_assign.append((name, line))
                exist_assign_names.add(name)

        merged_other = list(exist_other)
        for line in gen_other:
            if line not in merged_other and line.strip():
                merged_other.append(line)

        init_header = init_existing[0]
        reconstructed_init = [init_header]
        for line in merged_other:
            reconstructed_init.append(line)
        for _, line in merged_assign:
            reconstructed_init.append(line)

        merged_methods['__init__'] = reconstructed_init
    elif '__init__' in existing_methods:
        merged_methods['__init__'] = existing_methods['__init__']
    elif '__init__' in generated_methods:
        merged_methods['__init__'] = generated_methods['__init__']

    for name, lines in existing_methods.items():
        if name != '__init__':
            merged_methods[name] = lines

    for name, lines in generated_methods.items():
        if name != '__init__' and name not in merged_methods:
            merged_methods[name] = lines

    # Assemble class body
    class_body_lines = []
    if docstring:
        class_body_lines.extend(docstring)
        class_body_lines.append("")

    if merged_locators:
        for _, line in merged_locators:
            class_body_lines.append(line)
        class_body_lines.append("")

    if merged_other_class:
        for line in merged_other_class:
            class_body_lines.append(line)
        class_body_lines.append("")

    if '__init__' in merged_methods:
        class_body_lines.extend(merged_methods['__init__'])
        class_body_lines.append("")

    for name, lines in merged_methods.items():
        if name != '__init__':
            class_body_lines.extend(lines)
            class_body_lines.append("")

    final_class_lines = [class_def] + class_body_lines
    merged_class_content = '\n'.join(final_class_lines)

    result = '\n'.join(merged_imports)
    if result:
        result += '\n\n'
    result += merged_class_content

    if merged_outside:
        result += '\n\n' + '\n'.join(merged_outside)

    return result.strip() + '\n'


def parse_jsts_class(content: str) -> dict:
    """
    Parses JS/TS file to extract class header, properties (locators), constructor,
    methods, other class lines, and outside code.
    """
    parsed = _parse_brace_file(content)
    return {
        'class_def': parsed['header'][0] if parsed['header'] else None,
        'properties': [(m['key'], m['lines'][0]) for m in parsed['members']
                       if m['kind'] == 'field'],
        'constructor': next((m['lines'] for m in parsed['members']
                             if m['key'] == 'constructor'), []),
        'methods': {m['key']: m['lines'] for m in parsed['members']
                    if m['kind'] == 'method' and m['key'] != 'constructor'},
        'other_class_lines': [l for m in parsed['members'] if m['kind'] == 'other'
                              for l in m['lines']],
        'outside_lines': list(parsed['prefix']) + list(parsed['footer']),
    }


def parse_java_class(content: str) -> dict:
    """Parses Java/C# content into the same shape as :func:`parse_jsts_class`."""
    parsed = parse_jsts_class(content)
    parsed['fields'] = [(name, [line]) for name, line in parsed.pop('properties')]
    return parsed


def _legacy_line_merge(existing_content: str, generated_content: str) -> str:
    """Last-resort merge: append generated lines the existing file does not have."""
    merged_lines = existing_content.split('\n')
    seen = set(merged_lines)
    for line in generated_content.split('\n'):
        if line.strip() and line not in seen:
            merged_lines.append(line)
            seen.add(line)
    return '\n'.join(merged_lines).rstrip('\n') + '\n'


def _legacy_smart_merge_jsts(existing_content: str, generated_content: str) -> str:
    return _legacy_line_merge(existing_content, generated_content)


def _legacy_smart_merge_java(existing_content: str, generated_content: str) -> str:
    return _legacy_line_merge(existing_content, generated_content)
