import os
import re

def extract_imports_robust(content: str, language: str) -> tuple[list[str], str]:
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

def merge_imports(existing_imports: list[str], new_imports: list[str]) -> list[str]:
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
    class_locators = [] # list of (name, full_line_text)
    methods = {} # name -> list of lines
    other_class_lines = [] # comments or other class-level lines
    outside_lines = [] # lines outside class
    
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
        if stripped.startswith("def "):
            if current_method:
                methods[current_method] = current_method_lines
                current_method = None
            
            method_name = stripped.split("(")[0].replace("def ", "").strip()
            current_method = method_name
            current_method_indent = indent
            current_method_lines = [line]
            continue
            
        if current_method:
            if not stripped or indent > current_method_indent or stripped.startswith("#"):
                current_method_lines.append(line)
            else:
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

def smart_merge_python(existing_content: str, generated_content: str) -> str:
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
    lines = content.splitlines()
    
    class_def = None
    properties = [] # list of (name, full_line)
    constructor_lines = []
    methods = {} # name -> list of lines
    other_class_lines = []
    outside_lines = []
    
    current_method = None
    current_method_lines = []
    brace_count = 0
    
    in_class = False
    in_constructor = False
    
    for line in lines:
        stripped = line.strip()
        
        if not in_class:
            if "class " in line and "{" in line:
                in_class = True
                class_def = line
                continue
            outside_lines.append(line)
            continue
            
        open_braces = line.count('{')
        close_braces = line.count('}')
        
        if current_method:
            current_method_lines.append(line)
            brace_count += open_braces - close_braces
            if brace_count <= 0:
                methods[current_method] = current_method_lines
                current_method = None
            continue
            
        if in_constructor:
            constructor_lines.append(line)
            brace_count += open_braces - close_braces
            if brace_count <= 0:
                in_constructor = False
            continue
            
        if "constructor" in stripped and "{" in stripped:
            in_constructor = True
            constructor_lines = [line]
            brace_count = open_braces - close_braces
            if brace_count <= 0:
                in_constructor = False
            continue
            
        method_match = re.match(r'^(?:async\s+|public\s+|private\s+|protected\s+)*([A-Za-z0-9_]+)\s*\([^\)]*\)\s*\{', stripped)
        if method_match:
            method_name = method_match.group(1)
            current_method = method_name
            current_method_lines = [line]
            brace_count = open_braces - close_braces
            if brace_count <= 0:
                methods[current_method] = current_method_lines
                current_method = None
            continue
            
        if in_class and not current_method and not in_constructor:
            if stripped == "}":
                in_class = False
                continue
            prop_match = re.match(r'^(?:readonly\s+|public\s+|private\s+)?([A-Za-z0-9_]+)(?:\s*:\s*[A-Za-z0-9_<>\[\]]+)?(?:\s*=.*)?\s*;?$', stripped)
            if prop_match:
                prop_name = prop_match.group(1)
                if prop_name != "page":
                    properties.append((prop_name, line))
            else:
                if stripped:
                    other_class_lines.append(line)
                    
    return {
        'class_def': class_def,
        'properties': properties,
        'constructor': constructor_lines,
        'methods': methods,
        'other_class_lines': other_class_lines,
        'outside_lines': outside_lines
    }

def smart_merge_jsts(existing_content: str, generated_content: str) -> str:
    existing_imports, existing_body = extract_imports_robust(existing_content, 'ts')
    generated_imports, generated_body = extract_imports_robust(generated_content, 'ts')
    merged_imports = merge_imports(existing_imports, generated_imports)
    
    existing_class = parse_jsts_class(existing_body)
    generated_class = parse_jsts_class(generated_body)
    
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
    
    # Properties (Locators)
    existing_prop_names = {name for name, _ in existing_class['properties']}
    merged_properties = list(existing_class['properties'])
    for name, line in generated_class['properties']:
        if name not in existing_prop_names:
            merged_properties.append((name, line))
            existing_prop_names.add(name)
            
    # Other class lines
    merged_other_class = list(existing_class['other_class_lines'])
    for line in generated_class['other_class_lines']:
        if line.strip() and line not in merged_other_class:
            merged_other_class.append(line)
            
    # Constructor
    existing_constructor = existing_class['constructor']
    generated_constructor = generated_class['constructor']
    merged_constructor = []
    
    if existing_constructor and generated_constructor:
        def parse_this_assignments(lines):
            assignments = []
            others = []
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("this.") and "=" in stripped:
                    name = stripped.split("=")[0].replace("this.", "").strip()
                    assignments.append((name, line))
                else:
                    if "constructor" not in stripped and stripped != "}":
                        others.append(line)
            return assignments, others
            
        exist_assign, exist_other = parse_this_assignments(existing_constructor)
        gen_assign, gen_other = parse_this_assignments(generated_constructor)
        
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
                
        merged_constructor.append(existing_constructor[0])
        for line in merged_other:
            merged_constructor.append(line)
        for _, line in merged_assign:
            merged_constructor.append(line)
        merged_constructor.append(existing_constructor[-1])
    elif existing_constructor:
        merged_constructor = existing_constructor
    elif generated_constructor:
        merged_constructor = generated_constructor
        
    # Methods
    merged_methods = {}
    for name, lines in existing_class['methods'].items():
        merged_methods[name] = lines
    for name, lines in generated_class['methods'].items():
        if name not in merged_methods:
            merged_methods[name] = lines
            
    # Assemble Class Body
    class_body_lines = []
    for _, line in merged_properties:
        class_body_lines.append(line)
    if merged_properties:
        class_body_lines.append("")
        
    if merged_other_class:
        for line in merged_other_class:
            class_body_lines.append(line)
        class_body_lines.append("")
        
    if merged_constructor:
        class_body_lines.extend(merged_constructor)
        class_body_lines.append("")
        
    for name, lines in merged_methods.items():
        class_body_lines.extend(lines)
        class_body_lines.append("")
        
    final_class_lines = [class_def] + class_body_lines + ["}"]
    merged_class_content = '\n'.join(final_class_lines)
    
    result = '\n'.join(merged_imports)
    if result:
        result += '\n\n'
    result += merged_class_content
    
    if merged_outside:
        result += '\n\n' + '\n'.join(merged_outside)
        
    return result.strip() + '\n'

def parse_java_class(content: str) -> dict:
    """
    Parses Java/C# content to identify class definition, fields, constructor,
    methods, other class lines, and outside code.
    """
    lines = content.splitlines()
    
    class_def = None
    fields = [] # list of (name, lines including annotations)
    constructor_lines = []
    methods = {} # name -> list of lines
    other_class_lines = []
    outside_lines = []
    
    current_method = None
    current_method_lines = []
    brace_count = 0
    
    in_class = False
    in_constructor = False
    
    pending_annotations = []
    
    for line in lines:
        stripped = line.strip()
        
        if not in_class:
            if "class " in line and "{" in line:
                in_class = True
                class_def = line
                continue
            outside_lines.append(line)
            continue
            
        open_braces = line.count('{')
        close_braces = line.count('}')
        
        if current_method:
            current_method_lines.append(line)
            brace_count += open_braces - close_braces
            if brace_count <= 0:
                methods[current_method] = current_method_lines
                current_method = None
            continue
            
        if in_constructor:
            constructor_lines.append(line)
            brace_count += open_braces - close_braces
            if brace_count <= 0:
                in_constructor = False
            continue
            
        if stripped.startswith("@"):
            pending_annotations.append(line)
            continue
            
        if in_class and "{" in line:
            class_name = class_def.split("class ")[1].split()[0]
            if class_name in line:
                in_constructor = True
                constructor_lines = pending_annotations + [line]
                pending_annotations = []
                brace_count = open_braces - close_braces
                if brace_count <= 0:
                    in_constructor = False
                continue
            else:
                method_name_match = re.search(r'(\w+)\s*\(', stripped)
                if method_name_match:
                    method_name = method_name_match.group(1)
                    current_method = method_name
                    current_method_lines = pending_annotations + [line]
                    pending_annotations = []
                    brace_count = open_braces - close_braces
                    if brace_count <= 0:
                        methods[current_method] = current_method_lines
                        current_method = None
                    continue
                    
        if in_class and stripped.endswith(";"):
            field_match = re.search(r'(\w+)\s*;$', stripped)
            if field_match:
                field_name = field_match.group(1)
                fields.append((field_name, pending_annotations + [line]))
                pending_annotations = []
                continue
                
        if in_class and not current_method and not in_constructor:
            if stripped == "}":
                in_class = False
                continue
            if stripped:
                other_class_lines.append(line)
                
    return {
        'class_def': class_def,
        'fields': fields,
        'constructor': constructor_lines,
        'methods': methods,
        'other_class_lines': other_class_lines,
        'outside_lines': outside_lines
    }

def smart_merge_java(existing_content: str, generated_content: str) -> str:
    existing_imports, existing_body = extract_imports_robust(existing_content, 'java')
    generated_imports, generated_body = extract_imports_robust(generated_content, 'java')
    merged_imports = merge_imports(existing_imports, generated_imports)
    
    existing_class = parse_java_class(existing_body)
    generated_class = parse_java_class(generated_body)
    
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
    
    # Fields (Locators)
    existing_field_names = {name for name, _ in existing_class['fields']}
    merged_fields = list(existing_class['fields'])
    for name, lines in generated_class['fields']:
        if name not in existing_field_names:
            merged_fields.append((name, lines))
            existing_field_names.add(name)
            
    # Other class lines
    merged_other_class = list(existing_class['other_class_lines'])
    for line in generated_class['other_class_lines']:
        if line.strip() and line not in merged_other_class:
            merged_other_class.append(line)
            
    # Constructor
    merged_constructor = existing_class['constructor'] if existing_class['constructor'] else generated_class['constructor']
    
    # Methods
    merged_methods = {}
    for name, lines in existing_class['methods'].items():
        merged_methods[name] = lines
    for name, lines in generated_class['methods'].items():
        if name not in merged_methods:
            merged_methods[name] = lines
            
    # Assemble Class Body
    class_body_lines = []
    for _, lines in merged_fields:
        class_body_lines.extend(lines)
    if merged_fields:
        class_body_lines.append("")
        
    if merged_other_class:
        for line in merged_other_class:
            class_body_lines.append(line)
        class_body_lines.append("")
        
    if merged_constructor:
        class_body_lines.extend(merged_constructor)
        class_body_lines.append("")
        
    for name, lines in merged_methods.items():
        class_body_lines.extend(lines)
        class_body_lines.append("")
        
    final_class_lines = [class_def] + class_body_lines + ["}"]
    merged_class_content = '\n'.join(final_class_lines)
    
    result = '\n'.join(merged_imports)
    if result:
        result += '\n\n'
    result += merged_class_content
    
    if merged_outside:
        result += '\n\n' + '\n'.join(merged_outside)
        
    return result.strip() + '\n'

def smart_merge_code(existing_content: str, generated_content: str, filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    
    # Clean CRLF newlines to LF for parsing consistency
    existing = existing_content.replace('\r\n', '\n')
    generated = generated_content.replace('\r\n', '\n')
    
    if ext == '.py':
        res = smart_merge_python(existing, generated)
    elif ext in ('.js', '.ts', '.tsx', '.jsx'):
        res = smart_merge_jsts(existing, generated)
    elif ext in ('.java', '.cs'):
        res = smart_merge_java(existing, generated)
    else:
        # Fallback to general line merge for unknown file types
        existing_lines = existing.splitlines()
        generated_lines = generated.splitlines()
        
        merged_lines = list(existing_lines)
        for line in generated_lines:
            if line.strip() and line not in merged_lines:
                merged_lines.append(line)
        res = '\n'.join(merged_lines)
        
    # Re-apply OS-standard newlines if on Windows (but usually Python app deals with \n fine)
    return res.replace('\n', '\r\n') if os.name == 'nt' else res
