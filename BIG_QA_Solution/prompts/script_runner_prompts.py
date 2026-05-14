
def get_execution_command_prompt(tool: str, language: str, framework: str, env: str, browser: str, tags: str) -> str:
    """
    Returns the prompt for generating a terminal execution command for tests.
    
    # Used in file: BIG_QA_Solution/ScriptGenerator/ScriptRunnerEngine/runner.py
    # Used under function: execute_with_streaming
    """
    return f"""
        The user wants to run an automated test.
        Tool: {tool}
        Language: {language}
        Framework: {framework}
        Environment: {env}
        Browser: {browser}
        Tags: {tags}
        
        Generate the terminal command to execute this test suite locally. Wait, for Python it's usually `pytest` or `behave`, for Node it's `npm run test` or `npx playwright test`, for Java it's `mvn test`. Include any tags/browser parameters as standard arguments for the given framework.
        Return only JSON like: {{"command": "the command string"}}
        """

def get_diagnose_error_prompt(cmd: str, error_output: str) -> str:
    """
    Returns the prompt for diagnosing an execution error.
    
    # Used in file: BIG_QA_Solution/ScriptGenerator/ScriptRunnerEngine/runner.py
    # Used under function: execute_with_streaming
    """
    return f"""
                    The test execution failed.
                    Command: {cmd}
                    Output: {error_output}
                    
                    Analyze the error. Return ONLY JSON exactly matching this format:
                    {{
                      "error_type": "COMMAND_ERROR" or "SCRIPT_ERROR",
                      "file_to_fix": "relative/path/to/failed_script.py", 
                      "reason": "short description"
                    }}
                    If the error is related to element not found, syntax error in test script, assertion failure, etc., classify as SCRIPT_ERROR and provide the correct relative path to the failing script file.
                    If it's an issue with the command itself (e.g. pytest not found), classify as COMMAND_ERROR.
                    If no specific file is found, use empty string for file_to_fix.
                    """

def get_fix_script_prompt(error_output: str, file_to_fix: str, file_content: str) -> str:
    """
    Returns the prompt for self-healing a failing test script.
    
    # Used in file: BIG_QA_Solution/ScriptGenerator/ScriptRunnerEngine/runner.py
    # Used under function: execute_with_streaming
    """
    return f"""
                            The test failed with this error:
                            {error_output}
                            
                            Here is the current content of {file_to_fix}:
                            ```
                            {file_content}
                            ```
                            
                            Fix the source code (e.g., self-heal the locator or assertion) so the test will pass. Keep imports and class structures intact.
                            Return ONLY JSON:
                            {{
                              "fixed_content": "the completely rewritten file content here"
                            }}
                            """

def get_correct_command_prompt(cmd: str, error_output: str, tool: str, framework: str) -> str:
    """
    Returns the prompt for correcting a failed terminal command.
    
    # Used in file: BIG_QA_Solution/ScriptGenerator/ScriptRunnerEngine/runner.py
    # Used under function: execute_with_streaming
    """
    return f"""
                        The command `{cmd}` failed with this error: {error_output}
                        For a project using {tool}/{framework}.
                        Provide a corrected command in JSON like {{"command": "new command string"}}.
                        """
