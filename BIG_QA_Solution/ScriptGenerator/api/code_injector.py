import re
import os

class CodeInjector:
    @staticmethod
    def inject_methods_safely(existing_code: str, new_code: str, language: str) -> str:
        """
        Injects new code logic securely into an existing file's contents,
        respecting the class structures or brace definitions of target languages.
        """
        existing_code = existing_code.replace("\r\n", "\n").rstrip("\n")
        new_code = new_code.replace("\r\n", "\n").rstrip("\n")
        
        if not existing_code.strip():
            return new_code
            
        lang = language.lower()
        
        if lang in ['java', 'c#', 'ts', 'js', 'javascript', 'typescript']:
            # For C-like languages, we typically want to insert new methods right before the final closing brace
            # representing the class block.
            
            # Simple heuristic: find the very last '}' in the file
            last_brace_index = existing_code.rfind('}')
            
            if last_brace_index != -1:
                # Pre-brace injection
                top_part = existing_code[:last_brace_index]
                bottom_part = existing_code[last_brace_index:]
                
                # Check for standard imports to move to top
                existing_imports, new_imports, new_code_clean = CodeInjector._extract_imports(existing_code, new_code, lang)
                
                final_code = existing_imports
                if final_code:
                    final_code += "\n\n"
                    
                # rebuild the existing code excluding the imports we extracted
                # (to avoid duplicate imports if we want to be fancy). For simplicity, we just 
                # append new imports at the top
                
                if new_imports:
                    top_part = new_imports + "\n" + top_part
                
                return f"{top_part}\n\n    // Auto-injected:\n    {new_code_clean}\n{bottom_part}"
            else:
                # Fallback
                return existing_code + "\n\n" + new_code

        elif lang == 'python':
            # Python relies on indentation. Typically, files are simply functions or classes.
            # Look for the last line indentation to match, or just append since methods are fully defined.
            existing_imports, new_imports, new_code_clean = CodeInjector._extract_imports(existing_code, new_code, lang)
            
            top_part = existing_code
            if new_imports:
                top_part = new_imports + "\n" + top_part
                
            return f"{top_part}\n\n# Auto-injected:\n{new_code_clean}"
            
        return existing_code + "\n\n" + new_code

    @staticmethod
    def _extract_imports(existing: str, new_code: str, lang: str):
        existing_lines = existing.split('\n')
        new_lines = new_code.split('\n')
        
        existing_imports = set()
        new_imports = []
        new_code_clean = []
        
        import_prefixes = ['import ', 'from ', 'using '] if lang != 'python' else ['import ', 'from ']
        
        for line in existing_lines:
            if any(line.strip().startswith(prefix) for prefix in import_prefixes):
                existing_imports.add(line.strip())
                
        for line in new_lines:
            stripped = line.strip()
            is_import = any(stripped.startswith(prefix) for prefix in import_prefixes)
            if is_import:
                if stripped not in existing_imports:
                    new_imports.append(line)
            else:
                new_code_clean.append(line)
                
        return "", "\n".join(new_imports), "\n".join(new_code_clean)
