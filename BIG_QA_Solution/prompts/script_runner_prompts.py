
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
    Returns the prompt that classifies a failed run before any self-healing happens.

    Self-healing in this product is deliberately narrow: it repairs element
    locators inside Page Object files and nothing else. This prompt is the gate
    that decides whether healing is allowed at all, so it must be strict -
    anything that is not clearly an element locator failure has to come back as
    OTHER so the runner fails the test instead of rewriting source code.

    # Used in file: BIG_QA_Solution/ScriptGenerator/ScriptRunnerEngine/runner.py
    # Used under function: _attempt_locator_heal
    """
    return f"""
    A test command failed. Classify the failure. Do NOT propose any fix.

    Command: {cmd}
    Output: {error_output}

    Choose exactly ONE category:

    "LOCATOR" - the failure is an element locator / element interaction problem in the UI automation, such as:
      - NoSuchElementException, ElementNotFoundError, "waiting for selector", "Unable to locate element"
      - TimeoutException / TimeoutError while waiting for an element to appear, be visible, or be clickable
      - StaleElementReferenceException, ElementNotInteractableException, ElementClickInterceptedException
      - Playwright "locator resolved to hidden/disabled element" or strict-mode violations

    "COMMAND" - the test framework never really ran the tests because the command or environment is wrong, such as:
      - "command not found", "is not recognized as an internal or external command", "No module named", "mvn/npm/pytest/behave not found"
      - missing dependency, wrong working directory, bad CLI flag, no tests collected because of a bad path/tag argument
      - browser driver/binary missing or version mismatch, unable to start the browser at all

    "OTHER" - anything else. This includes (non-exhaustive):
      - assertion failures / wrong expected value / verification mismatch
      - Python/Java/TypeScript/C# syntax, compile, import or NameError/AttributeError problems
      - undefined, ambiguous or unimplemented step definitions
      - application errors, HTTP/API errors, database or test-data problems
      - login/credential failures, network or DNS errors

    Rules:
    - If you are not certain the failure is an element locator problem, answer "OTHER". Never guess "LOCATOR".
    - An assertion that failed because the page content was wrong is "OTHER", not "LOCATOR", even if the assertion reads an element.
    - For "LOCATOR" only, identify the Page Object file that declares the failing locator.
      Page Object files live in folders such as `pages/`, `pageObjects/` or `PageObjects/`
      (e.g. `pages/login_page.py`, `test/pageObjects/loginPage.ts`, `src/main/java/pageObjects/LoginPage.java`, `PageObjects/LoginPage.cs`).
      NEVER return a step definition file, a `.feature` file, a hooks/config/utility/runner file, or a test data file.
      If a failing locator is not declared in a Page Object file, return "file_to_fix" as an empty string
      for that entry - do not substitute a different file.

    Report EVERY distinct element locator failure in the output, not just the first one. The runner
    repairs them all in a single pass and then re-runs the suite once, so a locator you leave out
    costs another full suite run:
    - One entry per DISTINCT locator. The same locator failing in several scenarios, or repeated
      across retries of the same run, is ONE entry.
    - Two different locators in the same Page Object file are two entries with the same "file_to_fix".
    - Only include entries that are genuinely element locator failures. Do not pad the list: a
      cascading failure caused by an earlier locator failure (a step that could not run, or a
      scenario skipped afterwards) is NOT its own entry.

    Return ONLY JSON in this format:
    {{
      "error_category": "LOCATOR" or "COMMAND" or "OTHER",
      "reason": "one short sentence describing the overall failure",
      "failures": [
        {{
          "file_to_fix": "relative/path/to/page_object_file",
          "failed_locator": "selector value or locator variable name",
          "element_description": "what the element is, e.g. 'username input on the login page'"
        }}
      ]
    }}
    For "COMMAND" and "OTHER", "failures" MUST be an empty list.
    """

def get_heal_locator_prompt(file_path: str, file_content: str, failed_locators, error_output: str) -> str:
    """
    Returns the prompt for healing the failing element locators inside ONE Page Object file.

    The runner only ever passes a validated Page Object file here, and the
    healed content is written straight back over it. Step definitions and
    feature files are the human-authored contract of the suite, so this prompt
    forbids any change that would alter the Page Object's public API and break
    the callers that this healing pass is not allowed to touch.

    `failed_locators` is the list of every locator in THIS file that the diagnosis pass found
    broken, so one rewrite repairs all of them and the suite is re-run once instead of once per
    locator. A bare string is still accepted for backwards compatibility.

    # Used in file: BIG_QA_Solution/ScriptGenerator/ScriptRunnerEngine/runner.py
    # Used under function: _attempt_locator_heal
    """
    if isinstance(failed_locators, str):
        failed_locators = [failed_locators] if failed_locators.strip() else []
    locator_lines = "\n".join(f"      - {locator}" for locator in failed_locators if str(locator).strip())
    if not locator_lines:
        locator_lines = "      - (not identified precisely; infer the failing locator(s) from the error output below)"

    return f"""
    A UI test failed because of element locator / element interaction issues.
    You are healing ONE Page Object file.

    Failing Locators/Variables in THIS file - repair EVERY one of them in this single rewrite:
{locator_lines}

    Error Output:
    {error_output}

    Here is the full content of the Page Object file `{file_path}`:
    ```
    {file_content}
    ```

    Task:
    Repair ONLY the element locator definition(s) listed above and, if needed, the element wait logic
    that caused those failures. The file is written back to disk exactly once, so a rewrite that fixes
    only the first locator leaves the remaining ones broken - address every locator in the list.
    - If the locator is fragile (absolute XPath, generated/obfuscated class names, index-based paths), replace it with a
      robust one: id, name, data-testid/test-id, accessible role or label, placeholder, visible text, or a short relative XPath.
    - If the evidence points at timing, add or strengthen an explicit wait (visible / clickable / enabled) using the wait
      helpers already present in this file or its base page. Do not add a fixed sleep.
    - Prefer the locator style already used in this file (same imports, same By/Locator/page.locator API).

    Hard constraints - the healed file MUST still be a drop-in replacement for the current one:
    - Do NOT rename, add, remove or re-order any class, method or method parameter. Step definitions call these methods
      by name and this healing pass is not permitted to modify step definition or feature files.
    - Do NOT change assertions, test data, business logic, control flow, or any locator unrelated to this failure.
    - Do NOT remove or reorder existing imports; add an import only if the healed locator genuinely needs it.
    - Keep the file's existing formatting, comments and docstrings intact.
    - Return the file complete and syntactically valid - it is written back to disk verbatim.

    If NONE of the listed failures can be fixed by changing a locator or its wait in THIS file, return an
    empty string for "healed_content" - the test will be failed instead. Never guess. If you can fix some
    but not all of them, fix those and say which ones you left alone in the explanation.

    Return ONLY JSON in this format:
    {{
      "healed_content": "the complete content of the healed file here",
      "explanation": "short explanation naming each locator you changed and why"
    }}
    """

