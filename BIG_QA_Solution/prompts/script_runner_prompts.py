
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

def get_diagnose_locator_error_prompt(cmd: str, error_output: str) -> str:
    """
    Returns the prompt for diagnosing locator errors in sequential runner executions.
    """
    return f"""
    A test command failed. We want to check if it's due to an element locator failure (e.g. element not found, timeout waiting for element, element not interactable, click intercepted, etc.).
    
    Command: {cmd}
    Output: {error_output}
    
    Analyze the output logs carefully.
    Is this failure caused by an element locator issue?
    
    If yes, identify:
    1. The relative file path to the script or page object file containing the failing locator.
    2. The exact selector/locator value or variable that failed (e.g., "#user-name" or "loginButton").
    3. The reason for the failure.
    
    Return ONLY JSON in this format:
    {{
      "is_locator_error": true,
      "file_to_fix": "relative/path/to/file",
      "failed_locator": "selector or variable name",
      "reason": "short description of why it failed"
    }}
    If it is not a locator-related failure, return:
    {{
      "is_locator_error": false,
      "file_to_fix": "",
      "failed_locator": "",
      "reason": ""
    }}
    """

def get_heal_locator_prompt(file_path: str, file_content: str, failed_locator: str, error_output: str) -> str:
    """
    Returns the prompt for healing a failing element locator in a file.
    """
    return f"""
    The test failed because of an element locator/interaction issue.
    Failing Locator/Variable: {failed_locator}
    Error Output:
    {error_output}
    
    Here is the content of the file `{file_path}`:
    ```
    {file_content}
    ```
    
    Task:
    Heal the element locator or element waiting logic in the file to make it pass.
    Apply the best self-healing practices:
    - If the locator was too fragile (e.g. absolute XPath or too-specific class names), rewrite it to be more robust (e.g. using IDs, relative XPaths, text, placeholder, test-ids, parent-child relations).
    - If it's a timing issue, add appropriate wait conditions (e.g. wait for element to be visible/enabled, or use auto-wait).
    - Keep all imports, class structures, methods, and logic intact.
    
    Return ONLY JSON in this format:
    {{
      "healed_content": "the completely rewritten content of the file here",
      "explanation": "short explanation of what was changed and why"
    }}
    """

