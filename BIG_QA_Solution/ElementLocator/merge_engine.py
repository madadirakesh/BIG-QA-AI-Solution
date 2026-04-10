import re
import os
from code_generator import CodeGenerator

class MergeEngine:
    @staticmethod
    def merge_locators(target_content: str, new_locators: list, tool: str, lang: str) -> str:
        """
        Smart-merges new locators and their action methods into existing Page Object code.
        """
        if not target_content.strip():
            return target_content
        
        if lang == "Java":
            return MergeEngine._merge_java(target_content, new_locators, tool)
        elif lang == "Python":
            return MergeEngine._merge_python(target_content, new_locators, tool)
        elif lang in ["JavaScript", "TypeScript"]:
            return MergeEngine._merge_js_ts(target_content, new_locators, tool)
        elif lang == "C#":
            return MergeEngine._merge_csharp(target_content, new_locators, tool)
        
        return target_content

    @staticmethod
    def _merge_java(content, locs, tool):
        lines = content.splitlines()
        
        last_field_line = -1
        class_start_line = -1
        last_brace_line = -1
        
        for i, line in enumerate(lines):
            if "class " in line and "{" in line and class_start_line == -1:
                class_start_line = i
            if "@FindBy" in line or "[FindsBy]" in line or "WebElement " in line or "Locator " in line:
                last_field_line = i
            if "}" in line:
                last_brace_line = i
        
        field_lines = []
        method_lines = []
        for loc in locs:
            name = CodeGenerator.clean_name(loc.get('name', ''), 'element')
            l_type = loc.get('type', 'XPath').lower()
            val = CodeGenerator.escape_quotes(loc.get('value', ''))
            action = loc.get('action', 'Click')
            category = loc.get('category', 'Ok')
            
            # Check for duplicate variable
            if f" {name};" in content or f" {name} =" in content:
                pass
            else:
                if tool == "Selenium":
                    find_by_type = CodeGenerator._java_how(l_type)
                    field_lines.append(f"    // Priority: {category}")
                    field_lines.append(f"    @FindBy({find_by_type} = \"{val}\")")
                    field_lines.append(f"    private WebElement {name};")
                    field_lines.append("")
                else:
                    field_lines.append(f"    // Priority: {category}")
                    field_lines.append(f"    private Locator {name};")
                    field_lines.append("")
            
            # Check for duplicate method
            m_name = action.lower() + name[0].upper() + name[1:]
            if f" {m_name}(" in content:
                pass
            else:
                # Generate action method
                method_lines.append(CodeGenerator._java_action(tool, name, action))

        # Insert Fields
        field_insertion = last_field_line + 1 if last_field_line != -1 else class_start_line + 1
        if field_insertion != -1:
            for nl in reversed(field_lines):
                lines.insert(field_insertion, nl)
        
        # Recalculate last brace since indices changed
        # We find the last closing brace of the class
        for i in range(len(lines)-1, -1, -1):
            if "}" in lines[i]:
                last_brace_line = i
                break
        
        if last_brace_line != -1:
            for ml in reversed(method_lines):
                # Ensure spacing
                lines.insert(last_brace_line, ml)
            return "\n".join(lines)
        
        return content + "\n" + "\n".join(field_lines + method_lines)

    @staticmethod
    def _merge_python(content, locs, tool):
        lines = content.splitlines()
        
        class_line = -1
        init_line = -1
        last_attr_line = -1

        for i, line in enumerate(lines):
            if line.strip().startswith("class "):
                class_line = i
            if "def __init__" in line:
                init_line = i
            if "=" in line and ("(By." in line or "page.locator" in line or '"' in line):
                # A bit risky but looking for attributes
                if line.startswith("    ") and not line.startswith("        "):
                    last_attr_line = i

        loc_lines = []
        method_lines = []
        for loc in locs:
            name = CodeGenerator.clean_name(loc.get('name', ''), 'element', snake_case=True)
            val = CodeGenerator.escape_quotes(loc.get('value', ''))
            action = loc.get('action', 'Click')
            category = loc.get('category', 'Ok')
            
            # Check for duplicate duplicate variable
            if f" {name} =" in content or f"self.{name} =" in content:
                pass
            else:
                if tool == "Selenium":
                    # Class attribute
                    loc_lines.append(f"    # Priority: {category}")
                    loc_lines.append(f"    {name} = \"{val}\"")
                    loc_lines.append("")
                else:
                    # Playwright - usually self. assignments in __init__
                    loc_lines.append(f"        # Priority: {category}")
                    loc_lines.append(f"        self.{name} = page.locator(\"{val}\")")

            # Check for duplicate method
            m_name = f"{action.lower()}_{name.lower()}"
            if f"def {m_name}(" in content:
                pass
            else:
                # Generate action method
                method_lines.append(CodeGenerator._python_action(tool, name, action))

        # Insert Locators
        if tool == "Selenium":
            f_idx = last_attr_line + 1 if last_attr_line != -1 else class_line + 1
            for nl in reversed(loc_lines):
                lines.insert(f_idx, nl)
        else:
            f_idx = init_line + 1 if init_line != -1 else class_line + 1
            for nl in reversed(loc_lines):
                lines.insert(f_idx, nl)

        # Methods to end of class
        # (For simplicity we append, assuming no nested classes or after-class code)
        return "\n".join(lines) + "\n" + "\n".join(method_lines)

    @staticmethod
    def _merge_js_ts(content, locs, tool):
        lines = content.splitlines()
        
        class_start = -1
        constructor_start = -1
        last_prop = -1
        last_class_brace = -1
        
        for i, line in enumerate(lines):
            if "class " in line and "{" in line: class_start = i
            if "constructor" in line: constructor_start = i
            if "readonly " in line and ": Locator" in line: last_prop = i
            if line.strip() == "}": last_class_brace = i

        prop_lines = []
        init_lines = []
        method_lines = []
        for loc in locs:
            name = CodeGenerator.clean_name(loc.get('name', ''), 'element')
            val = CodeGenerator.escape_quotes(loc.get('value', ''))
            action = loc.get('action', 'Click')
            category = loc.get('category', 'Ok')
            
            # Check for duplicate property
            if f" {name}:" in content or f"this.{name}=" in content or f"this.{name} " in content:
                pass
            else:
                prop_lines.append(f"    // Priority: {category}")
                prop_lines.append(f"    readonly {name}: Locator;")
                init_lines.append(f"        this.{name} = page.locator('{val}');")
            
            # Check for duplicate method
            m_name = action.lower() + name[0].upper() + name[1:]
            if f"async {m_name}(" in content or f" {m_name}(" in content:
                pass
            else:
                method_lines.append(CodeGenerator._js_action(tool, name, action))

        # Re-calc braces logic... for simplicity let's find the last } before module.exports
        if constructor_start != -1:
            # We find where constructor ends
            # (Just a simple heuristic for now)
            for nl in reversed(init_lines):
                lines.insert(constructor_start + 1, nl)
            
            p_idx = last_prop + 1 if last_prop != -1 else class_start + 1
            for pl in reversed(prop_lines):
                lines.insert(p_idx, pl)
            
            # Find the index of the closing brace of the class
            # Heuristic: the last line that's just '}'
            m_idx = -1
            for i in range(len(lines)-1, -1, -1):
                if lines[i].strip() == "}":
                    m_idx = i
                    break
            
            if m_idx != -1:
                for ml in reversed(method_lines):
                    lines.insert(m_idx, ml)
            
            return "\n".join(lines)
        
        return content + "\n" + "\n".join(prop_lines + init_lines + method_lines)

    @staticmethod
    def _merge_csharp(content, locs, tool):
        lines = content.splitlines()
        
        last_field_line = -1
        class_start_line = -1
        last_brace_line = -1
        
        for i, line in enumerate(lines):
            if "class " in line and "{" in line and class_start_line == -1:
                class_start_line = i
            if "[FindsBy" in line or "IWebElement " in line or "ILocator " in line or "Locator =" in line:
                last_field_line = i
            if "}" in line:
                last_brace_line = i
                
        field_lines = []
        method_lines = []
        for loc in locs:
            name = CodeGenerator.clean_name(loc.get('name', ''), 'element')
            l_type = loc.get('type', 'XPath').lower()
            val = CodeGenerator.escape_quotes(loc.get('value', ''))
            action = loc.get('action', 'Click')
            category = loc.get('category', 'Ok')
            
            # Check for duplicate variable
            if f" {name} {{" in content or f" _{name};" in content or f" {name}Locator " in content:
                pass
            else:
                if tool.lower() == "selenium":
                    find_by_type = CodeGenerator._csharp_how(l_type)
                    field_lines.append(f"    // Priority: {category}")
                    field_lines.append(f"    [FindsBy({find_by_type} = \"{val}\")]")
                    field_lines.append(f"    public IWebElement {name} {{ get; set; }}")
                    field_lines.append("")
                elif tool.lower() == "playwright":
                    field_lines.append(f"    // Priority: {category}")
                    field_lines.append(f"    public readonly ILocator _{name};")
                    field_lines.append("")
                else:
                    field_lines.append(f"    // Priority: {category}")
                    field_lines.append(f"    public string {name}Locator = \"{val}\";")
                    field_lines.append("")
            
            # Check for duplicate method
            m_name = action + name[0].upper() + name[1:]
            if tool.lower() == "playwright":
                m_name += "Async"
                
            if f" {m_name}(" in content:
                pass
            else:
                method_lines.append(CodeGenerator._csharp_action(tool, name, action))

        # Insert Fields
        field_insertion = last_field_line + 1 if last_field_line != -1 else class_start_line + 1
        if field_insertion != -1:
            for nl in reversed(field_lines):
                lines.insert(field_insertion, nl)
                
        # Recalculate last brace
        for i in range(len(lines)-1, -1, -1):
            if "}" in lines[i]:
                last_brace_line = i
                break
                
        if last_brace_line != -1:
            for ml in reversed(method_lines):
                lines.insert(last_brace_line, ml)
            return "\n".join(lines)
            
        return content + "\n" + "\n".join(field_lines + method_lines)
