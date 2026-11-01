import re

class Preprocessor:
    def __init__(self):
        self.macros = {}
        self.eval_env = {}

    def remove_comments(self, text):
        """Strips out single-line and multi-line comments."""
        pattern = r'(/\*([^*]|\*+[^*/])*\*+/)|(//.*)'
        return re.sub(pattern, '', text)

    def handle_line_continuations(self, text):
        """Glues multi-line macros ending with a backslash into a single line."""
        lines = text.splitlines()
        combined_lines = []
        buffer = ""
        
        for line in lines:
            stripped = line.rstrip()
            if stripped.endswith('\\'):
                buffer += stripped[:-1]
            else:
                buffer += line
                combined_lines.append(buffer)
                buffer = ""
        if buffer:
            combined_lines.append(buffer)
        return '\n'.join(combined_lines)

    def register_macro(self, line):
        """Parses a unified single/multi-line #define block."""
        content = line.strip()[7:].strip()
        
        func_match = re.match(r'^([a-zA-Z_]\w*)\(([^)]*)\)(.*)', content)
        
        if func_match:
            macro_name = func_match.group(1)
            raw_params = func_match.group(2).split(',')
            replacement = func_match.group(3).strip()
            
            params = []
            is_variadic = False
            for p in raw_params:
                p_clean = p.strip()
                if p_clean == '...':
                    is_variadic = True
                    params.append('__VA_ARGS__')
                elif p_clean:
                    params.append(p_clean)
                    
            self.macros[macro_name] = (params, replacement, is_variadic)
            self.eval_env[macro_name] = True
        else:
            obj_match = re.match(r'^([a-zA-Z_]\w*)(.*)', content)
            if obj_match:
                macro_name = obj_match.group(1)
                replacement = obj_match.group(2).strip()
                self.macros[macro_name] = (None, replacement, False)
                try:
                    self.eval_env[macro_name] = int(replacement)
                except ValueError:
                    self.eval_env[macro_name] = True if replacement else False

    def eval_condition(self, condition_str):
        """Evaluates macro truth values, arithmetic, and defined(MACRO) expressions."""
        def replace_defined(match):
            macro = match.group(1) or match.group(2)
            return '1' if macro in self.eval_env else '0'
        
        condition_str = re.sub(r'\bdefined\s*\(\s*([a-zA-Z_]\w*)\s*\)', replace_defined, condition_str)
        condition_str = re.sub(r'\bdefined\s+([a-zA-Z_]\w*)', replace_defined, condition_str)
        
        for token in re.findall(r'\b[a-zA-Z_]\w*\b', condition_str):
            if token in self.eval_env:
                condition_str = re.sub(r'\b' + token + r'\b', str(self.eval_env[token]), condition_str)
            else:
                condition_str = re.sub(r'\b' + token + r'\b', '0', condition_str)
        try:
            return bool(eval(condition_str, {"__builtins__": None}, {}))
        except Exception:
            return False

    def process_conditionals(self, text):
        """Parses nested #if, #ifdef, #ifndef, #else, and #endif directives."""
        lines = text.splitlines()
        output_lines = []
        
        state_stack = [True] 
        executed_stack = [False]

        for line in lines:
            stripped = line.strip()
            
            if stripped.startswith('#if') and not stripped.startswith('#ifdef') and not stripped.startswith('#ifndef'):
                is_true = self.eval_condition(stripped[3:]) if state_stack[-1] else False
                state_stack.append(is_true and state_stack[-1])
                executed_stack.append(is_true)
                continue
            
            elif stripped.startswith('#ifdef'):
                macro_name = stripped[6:].strip()
                is_true = (macro_name in self.eval_env) if state_stack[-1] else False
                state_stack.append(is_true and state_stack[-1])
                executed_stack.append(is_true)
                continue
                
            elif stripped.startswith('#ifndef'):
                macro_name = stripped[7:].strip()
                is_true = (macro_name not in self.eval_env) if state_stack[-1] else False
                state_stack.append(is_true and state_stack[-1])
                executed_stack.append(is_true)
                continue
                
            elif stripped.startswith('#else'):
                parent_active = state_stack[-2] if len(state_stack) > 1 else True
                was_executed = executed_stack[-1]
                state_stack[-1] = parent_active and not was_executed
                continue
            
            elif stripped.startswith('#endif'):
                if len(state_stack) > 1:
                    state_stack.pop()
                    executed_stack.pop()
                continue
            
            # Append lines only within evaluated True scopes
            if state_stack[-1]:
                output_lines.append(line)
                
        return '\n'.join(output_lines)

    def apply_macros(self, text):
        """Repeatedly substitutes standard and variadic macros across the codebase."""
        while True:
            old_text = text
            for name, (params, replacement, is_variadic) in self.macros.items():
                if params is None:
                    text = re.sub(r'\b' + re.escape(name) + r'\b', replacement, text)
                else:
                    pattern = r'\b' + re.escape(name) + r'\(([^)]*)\)'
                    
                    def replace_func(match):
                        raw_args = match.group(1).split(',')
                        args = [a.strip() for a in raw_args if a.strip()] if raw_args != [''] else []
                        
                        local_replacement = replacement
                        
                        if is_variadic:
                            base_param_count = len(params) - 1
                            base_args = args[:base_param_count]
                            va_args = ", ".join(args[base_param_count:])
                            
                            for param, arg in zip(params[:-1], base_args):
                                local_replacement = re.sub(r'#\s*' + re.escape(param) + r'\b', f'"{arg}"', local_replacement)
                                local_replacement = re.sub(r'\b' + re.escape(param) + r'\b', arg, local_replacement)
                            
                            local_replacement = re.sub(r'#\s*__VA_ARGS__\b', f'"{va_args}"', local_replacement)
                            local_replacement = re.sub(r'\b__VA_ARGS__\b', va_args, local_replacement)
                        else:
                            if len(args) != len(params):
                                return match.group(0) # Mismatch safety fallback
                            
                            for param, arg in zip(params, args):
                                local_replacement = re.sub(r'#\s*' + re.escape(param) + r'\b', f'"{arg}"', local_replacement)
                                local_replacement = re.sub(r'\b' + re.escape(param) + r'\b', arg, local_replacement)
                        
                        local_replacement = re.sub(r'\s*##\s*', '', local_replacement)
                        return local_replacement
                    
                    text = re.sub(pattern, replace_func, text)
            
            if text == old_text:
                break
        return text

    def preprocess(self, source_code):
        clean_code = self.remove_comments(source_code)
        
        unified_code = self.handle_line_continuations(clean_code)
        program_lines = []
        for line in unified_code.splitlines():
            if line.strip().startswith('#define'):
                self.register_macro(line)
            else:
                program_lines.append(line)
        conditional_free = self.process_conditionals('\n'.join(program_lines))
        
        return self.apply_macros(conditional_free)

if __name__ == "__main__":
    source = """#define MULTI_LINE_MACRO(x) \
        do { \
            println("Value: %d\\n", x); \
        } while(0)
    #define LOG(format, ...) println(format, __VA_ARGS__)
    #ifndef TARGET_PLATFORM
        #define TARGET_PLATFORM 1
    #endif
    #ifdef TARGET_PLATFORM
        #if TARGET_PLATFORM == 1
            MULTI_LINE_MACRO(42);
            LOG("Logs: %s, %d", "Success", 200);
        #else
            LOG("Logs: %s, %d", "Failed", 400)
        #endif
    #endif
    fn main() -> void{
        LOG("%s", "Hello world!\\n");
    }"""
    preprocessor = Preprocessor()
    result = preprocessor.preprocess(source)
    print("Preprocessed Output:\n", result)
