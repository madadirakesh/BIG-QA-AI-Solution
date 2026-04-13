import uuid
import asyncio
import ast
import os
import json
import re
import hashlib
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
load_dotenv(dotenv_path=env_path)

#OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY", "")
#GEMINI_API_KEY      = os.getenv("GEMINI_API_KEY", "")
AI_TOOL             = os.getenv("AI_TOOL", "GEMINI").upper()
AI_MODEL            = os.getenv("AI_MODEL", "gemini-2.5-flash")
API_KEY      = os.getenv("API_KEY", "")

# if AI_TOOL in ["GEMINI", "GOOGLE"]:
#     AI_MODEL_API_KEY = API_KEY
# elif AI_TOOL in ["OPENAI", "COPILOT"]:
#     AI_MODEL_API_KEY = OPENAI_API_KEY
# elif AI_TOOL in ["CLAUDE", "ANTHROPIC"]:
#     AI_MODEL_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# else:
#     AI_MODEL_API_KEY = os.getenv("AI_MODEL_API_KEY", "")
#
DEFAULT_AI_PROVIDER = os.getenv("AI_PROVIDER", "openai").lower()

import openai as _openai_module

def _get_gemini_model():
    try:
        import google.generativeai as genai
        if not API_KEY:
            raise RuntimeError("AI_MODEL_API_KEY not set in .env")
        genai.configure(api_key=API_KEY)
        return genai.GenerativeModel(AI_MODEL)
    except ImportError:
        raise RuntimeError(
            "google-generativeai not installed. Run: pip install google-generativeai"
        )

app = FastAPI(title="AI QA Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

tasks_store = {}
dedup_index = {}


class GenerateCodeRequest(BaseModel):
    project_name:    str
    language:        str
    framework:       str
    project_path:    str
    bdd_content:     str
    support_content: str = ""
    file_content:    str = ""
    ai_provider:     str = ""


class GenerateBDDScenariosRequest(BaseModel):
    requirements: str
    ai_provider:  str = ""


class GenerateTestCasesRequest(BaseModel):
    requirements: str
    template:     str
    ai_provider:  str = ""


SELENIUM_STANDARDS_PYTHON = """
IMPORTANT - Follow Selenium 4 Python standards strictly:

Required imports in every file that uses Selenium:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.action_chains import ActionChains  (only if needed)
    from selenium.webdriver.common.keys import Keys  (only if needed)
    from webdriver_manager.chrome import ChromeDriverManager

Use ONLY these locator methods (Selenium 4 standard):
    driver.find_element(By.ID, "value")
    driver.find_element(By.NAME, "value")
    driver.find_element(By.XPATH, "value")
    driver.find_element(By.CSS_SELECTOR, "value")
    driver.find_element(By.CLASS_NAME, "value")
    driver.find_element(By.TAG_NAME, "value")
    driver.find_element(By.LINK_TEXT, "value")
    driver.find_element(By.PARTIAL_LINK_TEXT, "value")
    driver.find_elements(By.XPATH, "value")

Use WebDriverWait for all element interactions:
    WebDriverWait(browser, 10).until(EC.presence_of_element_located((By.ID, "value")))
    WebDriverWait(browser, 10).until(EC.element_to_be_clickable((By.XPATH, "value")))
    WebDriverWait(browser, 10).until(EC.visibility_of_element_located((By.NAME, "value")))

NEVER use any of these deprecated Selenium 3 methods:
    find_element_by_id()
    find_element_by_name()
    find_element_by_xpath()
    find_element_by_css_selector()
    find_element_by_class_name()
    find_element_by_tag_name()
    find_element_by_link_text()
    find_element_by_partial_link_text()
    find_elements_by_*()
    driver.find_element_by_*()

IMPORTANT - Always generate conftest.py with a 'browser' fixture:
    import pytest
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from webdriver_manager.chrome import ChromeDriverManager

    @pytest.fixture(scope="session")
    def browser():
        options = Options()
        options.add_argument("--start-maximized")
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=options
        )
        yield driver
        driver.quit()

    Rules:
    - Fixture name MUST always be 'browser' (never 'driver')
    - All step definition files must use 'browser' as parameter name
    - conftest.py must be placed at the project root level

IMPORTANT - Always generate a requirements.txt with ALL required packages:
    selenium>=4.0.0
    pytest
    pytest-bdd
    webdriver-manager
    behave
    python-dotenv
"""

SELENIUM_STANDARDS_JAVA = """
IMPORTANT - Follow Selenium 4 Java standards strictly:

Required imports in every file that uses Selenium:
    import org.openqa.selenium.By;
    import org.openqa.selenium.WebDriver;
    import org.openqa.selenium.WebElement;
    import org.openqa.selenium.support.ui.WebDriverWait;
    import org.openqa.selenium.support.ui.ExpectedConditions;
    import org.openqa.selenium.interactions.Actions;  (only if needed)
    import org.openqa.selenium.Keys;  (only if needed)
    import java.time.Duration;

Use ONLY these locator methods (Selenium 4 standard):
    driver.findElement(By.id("value"))
    driver.findElement(By.name("value"))
    driver.findElement(By.xpath("value"))
    driver.findElement(By.cssSelector("value"))
    driver.findElement(By.className("value"))
    driver.findElement(By.tagName("value"))
    driver.findElement(By.linkText("value"))
    driver.findElement(By.partialLinkText("value"))
    driver.findElements(By.xpath("value"))

Use WebDriverWait with Duration for all element interactions:
    new WebDriverWait(driver, Duration.ofSeconds(10))
        .until(ExpectedConditions.presenceOfElementLocated(By.id("value")));
    new WebDriverWait(driver, Duration.ofSeconds(10))
        .until(ExpectedConditions.elementToBeClickable(By.xpath("value")));
    new WebDriverWait(driver, Duration.ofSeconds(10))
        .until(ExpectedConditions.visibilityOfElementLocated(By.name("value")));

Use @FindBy annotations with PageFactory for Page Object Model:
    @FindBy(id = "username")
    private WebElement usernameField;
    PageFactory.initElements(driver, this);

NEVER use any of these deprecated Selenium 3 methods:
    driver.findElementById()
    driver.findElementByName()
    driver.findElementByXPath()
    driver.findElementByCssSelector()
    driver.findElementByClassName()
    new WebDriverWait(driver, 10)  (without Duration)
"""

PLAYWRIGHT_STANDARDS_TS = """
IMPORTANT - Follow Playwright TypeScript standards strictly:

Required imports in every file:
    import {{ test, expect }} from '@playwright/test';
    import {{ Page, Locator, Browser, BrowserContext }} from '@playwright/test';  (as needed)

Use ONLY these Playwright locator strategies:
    page.locator('css-or-xpath')
    page.getByRole('button', {{ name: 'Submit' }})
    page.getByLabel('Username')
    page.getByPlaceholder('Enter username')
    page.getByText('Welcome')
    page.getByTestId('login-btn')

Use await for ALL Playwright actions and assertions:
    await page.goto(url);
    await locator.click();
    await locator.fill('value');
    await locator.type('value');
    await expect(locator).toBeVisible();
    await expect(locator).toHaveText('value');
    await expect(locator).toHaveValue('value');
    await expect(page).toHaveURL('url');

Page Object Model pattern:
    export class LoginPage {{
        readonly page: Page;
        readonly usernameInput: Locator;

        constructor(page: Page) {{
            this.page = page;
            this.usernameInput = page.getByLabel('Username');
        }}
    }}

Always generate a package.json with ALL required dependencies:
    {{
        "dependencies": {{
            "@playwright/test": "^1.40.0",
            "typescript": "^5.0.0"
        }},
        "devDependencies": {{
            "@types/node": "^20.0.0"
        }},
        "scripts": {{
            "test": "playwright test",
            "test:headed": "playwright test --headed",
            "test:report": "playwright show-report"
        }}
    }}

NEVER use:
    Selenium-style locators or methods
    document.querySelector() or other DOM methods
    Non-async calls to Playwright methods
"""

LOCATOR_USAGE_STANDARDS = """
CRITICAL - Element Locators Usage:

The Supporting Information section contains element locators in this format:
    ElementName (Type: LOCATOR_TYPE): locator_value

You MUST use these EXACT locators in the generated code. DO NOT generate your own locators.

Locator Type mapping:
    XPATH       -> By.XPATH, "locator_value"
    ID          -> By.ID, "locator_value"
    NAME        -> By.NAME, "locator_value"
    CSS         -> By.CSS_SELECTOR, "locator_value"
    CLASS_NAME  -> By.CLASS_NAME, "locator_value"
    LINK_TEXT   -> By.LINK_TEXT, "locator_value"

Example - if Supporting Information contains:
    UsernameField (Type: XPATH): //input[@id='un']
    PasswordField (Type: XPATH): //input[@id='pw']
    SubmitButton  (Type: XPATH): //input[@id='jsLoginButton']

Then generated code MUST use:
    username_field = WebDriverWait(browser, 10).until(
        EC.presence_of_element_located((By.XPATH, "//input[@id='un']"))
    )
    password_field = WebDriverWait(browser, 10).until(
        EC.presence_of_element_located((By.XPATH, "//input[@id='pw']"))
    )
    submit_button = WebDriverWait(browser, 10).until(
        EC.element_to_be_clickable((By.XPATH, "//input[@id='jsLoginButton']"))
    )

NEVER substitute, modify or guess locators.
NEVER use By.ID, By.NAME or any other type unless it exactly matches the Type in Supporting Information.
If a locator is provided in Supporting Information, use it exactly as given - no exceptions.
"""

@app.get("/health")
async def health_check():
    return {
        "status":            "healthy",
        "service":           "AI QA Backend",
        "ai_provider":       AI_TOOL,
        "ai_configured": bool(API_KEY),
        #"gemini_configured": bool(GEMINI_API_KEY),
    }


async def call_openai(prompt: str, expect_json: bool = True) -> str:
    def _call():
        _openai_module.api_key = API_KEY
        
        system_content = "You are an expert QA automation engineer."
        if expect_json:
            system_content += (
                " You MUST respond with ONLY a valid raw JSON object. "
                "Do NOT include any explanation, markdown code fences (```), "
                "preamble, postamble, or 'Please note' text. "
                "The very first character of your response MUST be '{' "
                "and the very last character MUST be '}'."
            )
        response = _openai_module.ChatCompletion.create(
            model=AI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": system_content,
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0
        )
        return response.choices[0].message.content
    return await asyncio.to_thread(_call)


async def call_gemini(prompt: str, expect_json: bool = True) -> str:
    model = _get_gemini_model()

    def _call():
        system_preamble = "You are an expert QA automation engineer.\n\n"
        if expect_json:
            system_preamble += (
                "You MUST respond with ONLY a valid raw JSON object. "
                "Do NOT include any explanation, markdown code fences (```), "
                "preamble, postamble, or 'Please note' text. "
                "The very first character of your response MUST be '{' "
                "and the very last character MUST be '}'.\n\n"
            )
        response = model.generate_content(system_preamble + prompt)
        return response.text

    return await asyncio.to_thread(_call)


async def call_ai(prompt: str, provider: str = "", expect_json: bool = True) -> str:
    if AI_TOOL in ["GEMINI", "GOOGLE"]:
        if not API_KEY:
            raise HTTPException(
                status_code=500, detail="AI_MODEL_API_KEY not configured in .env"
            )
        return await call_gemini(prompt, expect_json)
    elif AI_TOOL in ["OPENAI", "COPILOT"]:
        if not API_KEY:
            raise HTTPException(
                status_code=500, detail="AI_MODEL_API_KEY not configured in .env"
            )
        return await call_openai(prompt, expect_json)
    else:
        # Fallback default
        if not API_KEY:
            raise HTTPException(
                status_code=500, detail=f"AI_MODEL_API_KEY not configured in .env for {AI_TOOL}"
            )
        return await call_gemini(prompt, expect_json)


def parse_json_result(result: str, fallback_key: str) -> dict:

    def _try_parse(s: str):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass
        return None

    parsed = _try_parse(result)
    if parsed:
        return parsed

    unescaped = result.replace("\\'", "'")
    if unescaped != result:
        parsed = _try_parse(unescaped)
        if parsed:
            return parsed

    no_fences = re.sub(r"```(?:json)?\s*", "", result)
    no_fences = no_fences.replace("```", "").strip()
    no_fences = no_fences.replace("\\'", "'")  # also fix escape after stripping fences
    parsed = _try_parse(no_fences)
    if parsed:
        return parsed

    first_brace = result.find("{")
    last_brace  = result.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        candidate = result[first_brace : last_brace + 1]
        parsed = _try_parse(candidate)
        if parsed:
            return parsed

    for block in re.finditer(r"```(?:json)?\s*(.*?)```", result, re.DOTALL):
        block_text = block.group(1).strip()
        parsed = _try_parse(block_text)
        if parsed:
            return parsed

    with open("FailedOperation.txt", "w", encoding="utf-8") as f:
        f.write("Code Generation Failed\n\n")
        f.write("Reason: Could not extract valid JSON from AI response.\n\n")
        f.write("Strategies attempted:\n")
        f.write("  1. Direct json.loads()\n")
        f.write("  2. Strip markdown fences\n")
        f.write("  3. Extract largest {…} block from raw text\n")
        f.write("  4. Parse individual code blocks\n\n")
        f.write("Raw AI Response:\n")
        f.write(result)

    return {fallback_key: result}

def sanitize_step_quoting(files: dict) -> dict:

    def fix_feature_line(line: str) -> str:
        if re.match(r'^\s*(Given|When|Then|And|But)\b', line, re.IGNORECASE):
            # Replace "quoted text" with 'quoted text' inside step text
            line = re.sub(r'"([^"]*)"', r"'\1'", line)
        return line

    def fix_decorator_line(line: str) -> str:
        m = re.match(
            r'^(\s*@(?:given|when|then|and|but))\(([\'"])(.*?)\2\)(.*)',
            line, re.IGNORECASE | re.DOTALL
        )
        if not m:
            return line
        prefix     = m.group(1)
        quote_char = m.group(2)
        step_text  = m.group(3)
        rest       = m.group(4)

        step_text = step_text.replace('"', "'")
        return f'{prefix}("{step_text}"){rest}'

    sanitized = {}
    for fname, code in files.items():
        if not isinstance(code, str):
            sanitized[fname] = code
            continue
        if fname.endswith(".feature"):
            sanitized[fname] = "\n".join(fix_feature_line(l) for l in code.splitlines())
        elif fname.endswith("_steps.py") or fname.endswith("steps.py"):
            sanitized[fname] = "\n".join(fix_decorator_line(l) for l in code.splitlines())
        else:
            sanitized[fname] = code
    return sanitized

def extract_methods(code: str) -> set:
    methods = set()
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.add(node.name)
    except SyntaxError:
        for match in re.finditer(r'^\s*def\s+(\w+)\s*\(', code, re.MULTILINE):
            methods.add(match.group(1))
    return methods


def extract_new_methods_only(existing_code: str, generated_code: str) -> str:
    existing_code   = existing_code.replace("\r\n", "\n")
    generated_code  = generated_code.replace("\r\n", "\n")
    existing_methods = extract_methods(existing_code)

    new_method_lines = []
    inside_new_method = False
    indent_level = None

    for line in generated_code.splitlines():
        stripped = line.strip()
        method_match = re.match(r'^(\s*)def\s+(\w+)\s*\(', line)
        if method_match:
            method_name = method_match.group(2)
            if method_name not in existing_methods:
                inside_new_method = True
                indent_level = len(method_match.group(1))
                new_method_lines.append(line)
            else:
                inside_new_method = False
            continue
        if inside_new_method:
            if stripped and not stripped.startswith("#"):
                current_indent = len(line) - len(line.lstrip())
                if current_indent <= indent_level and stripped.startswith("def "):
                    inside_new_method = False
                    continue
            new_method_lines.append(line)

    return "\n".join(new_method_lines).strip()


def extract_new_locators_only(existing_code: str, generated_code: str) -> str:
    existing_code  = existing_code.replace("\r\n", "\n")
    generated_code = generated_code.replace("\r\n", "\n")
    existing_locators = set(re.findall(r'^\s{4}([A-Z_]+)\s*=', existing_code, re.MULTILINE))

    new_lines = []
    for line in generated_code.splitlines():
        match = re.match(r'^\s{4}([A-Z_]+)\s*=', line)
        if match and match.group(1) not in existing_locators:
            new_lines.append(line)

    return "\n".join(new_lines).strip()


def extract_new_imports_only(existing_code: str, generated_code: str) -> str:
    existing_code  = existing_code.replace("\r\n", "\n")
    generated_code = generated_code.replace("\r\n", "\n")
    existing_imports = set(
        line.strip() for line in existing_code.splitlines()
        if line.strip().startswith("import ") or line.strip().startswith("from ")
    )
    new_imports = [
        line for line in generated_code.splitlines()
        if (line.strip().startswith("import ") or line.strip().startswith("from "))
        and line.strip() not in existing_imports
    ]
    return "\n".join(new_imports).strip()


def try_parse_to_dict(content, fallback_fname: str) -> dict:
    if isinstance(content, dict):
        return content
    try:
        cleaned = re.sub(r"```(?:json)?\s*", "", content).replace("```", "").strip()
        json_match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if json_match:
            parsed = json.loads(json_match.group())
            if isinstance(parsed, dict):
                return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return {fallback_fname: content}


def _infer_folder(filename: str) -> str:
    f = filename
    if f.endswith("_page.py"):           return "pages/"    + f
    if f.endswith("_steps.py"):          return "steps/"    + f
    if f.endswith("_locators.py"):       return "locators/" + f
    if f == "locators.py":               return "locators/" + f
    if f.startswith("test_"):            return "tests/"    + f
    if f.endswith("_test.py"):           return "tests/"    + f
    if f.endswith(".feature"):           return "features/" + f
    if f.endswith("_page.py"):           return "pages/"    + f
    return f   # no rule matched — return as-is


def normalize_keys(result_dict: dict) -> dict:
    known_folders = {"pages", "steps", "tests", "features", "locators", "config"}
    normalized = {}

    # ── Pass 1: strip junk prefix, keep from known folder onward ─────────────
    # A path segment is only treated as a folder when something comes AFTER it.
    # This prevents "locators.py" being matched as the folder "locators".
    for key, value in result_dict.items():
        clean_key = key.replace("\\", "/")
        parts = clean_key.split("/")
        matched = False
        for i, part in enumerate(parts):
            # Must be a directory segment (not the final filename)
            if part in known_folders and i < len(parts) - 1:
                remaining = "/".join(parts[i + 1:])
                new_key = part + ("/" + remaining if remaining else "")
                normalized[new_key] = value
                matched = True
                break
        if not matched:
            normalized[clean_key] = value

    final = {}
    for key, value in normalized.items():
        if "/" not in key:
            # Bare filename — infer the folder
            final[_infer_folder(key)] = value
        else:
            final[key] = value
    return final


def merge_into_existing(file_type: str, existing_code: str, parsed: dict) -> dict:
    existing_clean = existing_code.replace("\r\n", "\n").rstrip("\n")

    for key in parsed:
        generated_code = parsed[key].replace("\r\n", "\n")
        if not existing_clean.strip():
            parsed[key] = generated_code
            continue

        new_imports = extract_new_imports_only(existing_clean, generated_code)

        if file_type == "locators":
            new_content = extract_new_locators_only(existing_clean, generated_code)
        else:
            new_content = extract_new_methods_only(existing_clean, generated_code)

        base = existing_clean
        if new_imports:
            base = new_imports + "\n" + base
        if new_content:
            base = base + "\n" + new_content

        parsed[key] = base
    return parsed

async def generate_python_pytest(
    bdd_content: str, support_content: str, file_content: str, provider: str
) -> dict:
    is_existing_file = "File Mode: Existing" in support_content
    is_new_file      = "File Mode: New"      in support_content
    file_mode        = "Existing" if is_existing_file else "New" if is_new_file else "Unknown"

    project_path_match = re.search(r"Project Path:\s*(.+)", support_content)
    project_dir = project_path_match.group(1).strip() if project_path_match else ""

    new_file_name            = ""
    new_file_support_content = ""

    if file_mode == "New":
        feature_match = re.search(r"Feature:\s*(.+)", bdd_content)
        feature_name  = re.sub(r"[^a-z0-9]+", "_", feature_match.group(1).strip().lower()).strip("_") if feature_match else "login"

        file_templates = {
            "tests": f"""from pytest_bdd import scenarios
from {project_dir}.steps.{feature_name}_steps import *

scenarios('../features/{feature_name}.feature')
""",
            "locators": f"""from selenium.webdriver.common.by import By


class Locators:
    # TODO: add all locators below as class attributes
    pass
""",
            "page": f"""from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from {project_dir}.config.config import *
from {project_dir}.locators.locators import Locators


class LoginPage:
    def __init__(self, driver):
        self.driver = driver
        self.wait = WebDriverWait(driver, 10)

    def open_login_page(self):
        self.driver.get(BASE_URL)
""",
            "steps": f"""from pytest_bdd import given, when, then
from {project_dir}.pages.login_page import LoginPage


@given('I have navigated to the login page of the application')
def navigate_to_login_page(browser):
    LoginPage(browser).open_login_page()
""",
        }

        prompt = f"""
        You are an expert QA automation engineer specialized in Python Pytest-BDD framework with Selenium WebDriver.

        Your task: generate a COMPLETE, FULLY WORKING pytest-bdd test suite based on the BDD scenarios below.

        ── OUTPUT FORMAT ──────────────────────────────────────────────────────────────
        Return ONLY a single valid JSON object. Keys are relative file paths, values are
        the complete file contents as strings. No prose, no markdown fences.

        Required files:
          "tests/test_<name>.py"      — pytest-bdd test runner
          "locators/locators.py"      — all element locators as class attributes
          "features/<name>.feature"   — exact BDD feature file (from BDD Content below)
          "pages/<name>_page.py"      — Page Object with one method per BDD action
          "steps/<name>_steps.py"     — one step-def function per BDD step

        ── STRUCTURE TEMPLATES (use these as the STARTING SCAFFOLD, then complete them) ──

        tests/test_*.py MUST begin with:
        {file_templates["tests"]}
        Then add: @pytest.fixture references if needed. Nothing else.

        locators/locators.py MUST begin with:
        {file_templates["locators"]}
        Then add: one class attribute per locator from Supporting Information, e.g.:
            USERNAME  = (By.ID, "un")
            PASSWORD  = (By.ID, "pw")
            LOGIN_BTN = (By.XPATH, "//input[@id='jsLoginButton']")

        pages/*_page.py MUST begin with:
        {file_templates["page"]}
        Then add: one method per BDD action step (When/And/Then), e.g.:
            def enter_username(self):
                el = self.wait.until(EC.presence_of_element_located(LoginLocators.USERNAME))
                el.send_keys(USERNAME)

            def click_login(self):
                self.wait.until(EC.element_to_be_clickable(LoginLocators.LOGIN_BTN)).click()

        steps/*_steps.py MUST begin with:
        {file_templates["steps"]}
        Then add: one @when/@then decorated function per remaining BDD step, each calling
        the matching LoginPage method, e.g.:
            @when("I enter a valid username")
            def enter_username(browser):
                LoginPage(browser).enter_username()

            # QUOTING RULE: use double-quoted decorator string so single quotes
            # inside step text work correctly, e.g.:
            @when("I click on the 'Monitoring' tab")
            def click_monitoring_tab(browser):
                LoginPage(browser).click_monitoring_tab()

        ── FEATURE FILE RULES ─────────────────────────────────────────────────────────
        - Reproduce the BDD Content EXACTLY as the feature file — do not invent new steps.
        - NEVER use double quotes (") anywhere inside Gherkin step text.
          Double quotes inside a step break pytest-bdd's step matching regex.
        - Use single quotes (') for any element/button/field name references inside steps.
          CORRECT:   When I click on the 'Monitoring' tab
          CORRECT:   And I enter 'username' in the 'Email' field
          INCORRECT: When I click on the "Monitoring" tab   ← breaks regex matching
          INCORRECT: And I enter "username" in the "Email" field

        ── STEP DEFINITION RULES ──────────────────────────────────────────────────────
        - Step decorator strings MUST match the feature file step text CHARACTER-FOR-CHARACTER.
        - Use single-quoted decorator strings: @when('I click on the \'Monitoring\' tab')
          OR escape with curly-brace parsers if using parse/cfparse.
        - NEVER use double quotes inside the step decorator text.
        - Example mapping:
            Feature step:  When I click on the 'Monitoring' tab
            Step def:      @when("I click on the 'Monitoring' tab")
            Function:      def click_monitoring_tab(browser): ...

        ── GENERAL RULES ──────────────────────────────────────────────────────────────
        - Every BDD step MUST have a corresponding step-def function and page method.
        - Do NOT leave any step unimplemented.
        - Use ONLY the locators from Supporting Information — never invent new ones.
        - If a locator is missing for a step, add # TODO comment in page method.
        - conftest.py is assumed to exist with a 'browser' fixture — do NOT regenerate it.

        {SELENIUM_STANDARDS_PYTHON}

        {LOCATOR_USAGE_STANDARDS}

        Supporting Information:
        {support_content}

        BDD Content:
        {bdd_content}
        """

        result = await call_ai(prompt, provider)

        # ── Debug: write raw AI response to file so failures are diagnosable ──
        try:
            with open("LastAIResponse_New.txt", "w", encoding="utf-8") as _dbg:
                _dbg.write(f"Provider: {provider}\nFile Mode: New\n\n")
                _dbg.write("=== RAW AI RESPONSE ===\n")
                _dbg.write(result or "(empty)")
        except Exception:
            pass

        parsed = parse_json_result(result, "tests/generated_test.py")

        # Only warn when the fallback key is the ONLY key — meaning parse truly failed.
        if isinstance(parsed, dict) and list(parsed.keys()) == ["tests/generated_test.py"]:
            print("WARNING: parse_json_result fell back to generated_test.py key.")
            print(f"First 500 chars of response: {(result or '')[:500]}")
            return {}

        if not isinstance(parsed, dict):
            return {}
        return sanitize_step_quoting(parsed)

    elif file_mode == "Existing":
        print("Insideeeeeeeeeeeee")
        print(support_content)
        file_name = ""
        if support_content:
            for line in support_content.splitlines():
                if line.strip().startswith("File Name:"):
                    file_name = line.strip().replace("File Name:", "").strip()
                    break

            new_file_match = re.search(
                r'New File Name:\s*(.+?)\nNew File Support Content:\s*(.*?)(?=\n[A-Z]|\Z)',
                support_content,
                re.DOTALL
            )

            if new_file_match:
                new_file_name            = new_file_match.group(1).strip()
                new_file_support_content = new_file_match.group(2).strip()

        base_name = os.path.basename(file_name) if file_name else ""
        print(base_name)

        if new_file_name and base_name:
            # ── Only process if new_file_name has no extension and no folder yet ──
            stem = os.path.splitext(new_file_name)[0]   # "monitoring" (unchanged if no ext)
            has_ext    = bool(os.path.splitext(new_file_name)[1])
            has_folder = "/" in new_file_name

            if not has_ext:

                base_stem = os.path.splitext(base_name)[0]   # e.g. "test_login", "login_page"

                if base_name.startswith("test_"):
                    new_file_name = f"test_{stem}.py"

                elif base_stem.endswith("_test"):
                    new_file_name = f"{stem}_test.py"

                elif base_name.endswith("_page.py"):
                    new_file_name = f"{stem}_page.py"

                elif base_name.endswith("_steps.py"):
                    new_file_name = f"{stem}_steps.py"

                elif base_name.endswith("_locators.py") or base_name == "locators.py":
                    new_file_name = f"{stem}_locators.py"

                elif base_name.endswith(".feature"):
                    new_file_name = f"{stem}.feature"

                else:
                    _, ext = os.path.splitext(base_name)
                    new_file_name = f"{stem}{ext}" if ext else f"{stem}.py"

            if "/" not in new_file_name:
                if new_file_name.endswith("_page.py"):
                    new_file_name = "pages/"    + new_file_name
                elif new_file_name.endswith("_steps.py"):
                    new_file_name = "steps/"    + new_file_name
                elif new_file_name.endswith("_locators.py") or new_file_name == "locators.py":
                    new_file_name = "locators/" + new_file_name
                elif new_file_name.startswith("test_") or new_file_name.endswith("_test.py"):
                    new_file_name = "tests/"    + new_file_name
                elif new_file_name.endswith(".feature"):
                    new_file_name = "features/" + new_file_name
                # Fallback: use base_name's folder if resolved name has no clear folder signal
                elif "_page.py" in base_name:
                    new_file_name = "pages/"    + new_file_name
                elif "_steps.py" in base_name:
                    new_file_name = "steps/"    + new_file_name
                elif "locators.py" in base_name:
                    new_file_name = "locators/" + new_file_name
                elif base_name.startswith("test_") or base_name.endswith("_test.py"):
                    new_file_name = "tests/"    + new_file_name
                elif ".feature" in base_name:
                    new_file_name = "features/" + new_file_name

        print(new_file_name)
        print(new_file_support_content)

        if new_file_name:
            print("Inside NEw Fileeeeeeeeeeeeeeeeeeeee")
            new_base_name = os.path.basename(new_file_name)
            print(new_base_name)

            if base_name.endswith("_page.py"):
                file_type = "page"
                file_type_instructions = f"""
                    You are generating a BRAND NEW PAGE OBJECT file: {new_file_name}

                    STRUCTURE RULES (learn from the existing file's style, do NOT copy its content):
                    - Class name must be derived from {new_file_name} (PascalCase, e.g. monitoring_page.py -> MonitoringPage).
                    - Include __init__(self, driver) with self.driver and self.wait = WebDriverWait(driver, 10).
                    - Use the SAME import pattern as the existing file (WebDriverWait, By, EC, config, locators).
                    - Import ONLY the locator class that belongs to this new page — never import locator classes from other pages.
                    - Every method must follow snake_case action naming: enter_*, click_*, get_*, is_*, wait_for_*.
                    - Each method does exactly ONE action (single responsibility).
                    - Use self.wait.until(EC.presence_of_element_located(...)) for inputs/text.
                    - Use self.wait.until(EC.element_to_be_clickable(...)).click() for buttons/links.
                    - Reference locators as ClassName.LOCATOR_NAME — never hard-code selector strings inline.
                    - If a required locator is missing add: # TODO: add <element_name> to locators file

                    BDD-STRICT METHOD GENERATION:
                    - Generate ONE method per unique action mentioned in the BDD steps.
                    - Count the BDD steps carefully. Generate EXACTLY that many methods — no more, no less.
                    - Do NOT invent methods for actions that are not in the BDD steps.
                    - Do NOT add helper utilities, assertions, or test logic.

                    New File Supporting Content (additional context if provided):
                    {new_file_support_content}
                """
                default_fallback_file = "pages/" + new_base_name

            elif base_name.endswith("locators.py"):
                file_type = "locators"
                file_type_instructions = f"""
                    You are generating a BRAND NEW LOCATORS file: {new_file_name}

                    STRUCTURE RULES (follow the naming convention of the existing file):
                    - Create a NEW class with a PascalCase name derived from {new_file_name}
                      (e.g. monitoring_locators.py -> MonitoringPageLocators).
                    - Use only By.* strategies already present in the existing file.
                    - Each locator is a class attribute: NAME = (By.STRATEGY, "value").
                    - UPPER_SNAKE_CASE for all attribute names.
                    - Add a brief inline comment only if the name alone is not self-explanatory.
                    - Do NOT add methods, test logic, or extra imports.

                    BDD-STRICT LOCATOR GENERATION:
                    - Add ONLY the locators that are referenced by the BDD steps.
                    - Use ONLY the locators from the Supporting Information / Element Locators section.
                    - Do NOT invent locators for elements that are not in the BDD steps.

                    New File Supporting Content (additional context if provided):
                    {new_file_support_content}
                """
                default_fallback_file = "locators/" + new_base_name

            elif base_name.endswith("_steps.py"):
                file_type = "steps"
                file_type_instructions = f"""
                    You are generating a BRAND NEW STEP DEFINITIONS file: {new_file_name}

                    STRUCTURE RULES (learn from the existing file's style, do NOT copy its content):
                    - Use the SAME decorator style as the existing file (@given, @when, @then from pytest-bdd or behave).
                    - Use the SAME fixture/context injection pattern (e.g. browser fixture or context.driver).
                    - Each step function maps 1-to-1 to a BDD step — no multi-step functions.
                    - Import ONLY the page class(es) that correspond to this new file's steps.
                    - Do NOT import page classes that are not needed for these steps.
                    - Step function body must be a single page-object method call — no logic or assertions.

                    BDD-STRICT STEP GENERATION:
                    - Generate EXACTLY one @given/@when/@then function per BDD step in the feature file.
                    - Do NOT add steps that are not in the BDD content.
                    - Do NOT duplicate steps that are already covered elsewhere.
                    - Step decorator text must match the BDD step text character-for-character.

                    New File Supporting Content (additional context if provided):
                    {new_file_support_content}
                """
                default_fallback_file = "steps/" + new_base_name

            else:
                file_type = "generic"
                file_type_instructions = f"""
                    You are generating a BRAND NEW general Python test/utility file: {new_file_name}

                    STRUCTURE RULES (learn from the existing file's style, do NOT copy its content):
                    - Follow the same import ordering, naming conventions, and structure.
                    - Preserve the same fixture/helper pattern.
                    - Only add code that directly relates to the BDD steps provided.
                    - Do NOT add extra helper functions or utilities not needed by the BDD steps.

                    New File Supporting Content (additional context if provided):
                    {new_file_support_content}
                """
                default_fallback_file = new_base_name if new_base_name else "tests/generated_test.py"

            new_file_base = os.path.splitext(os.path.basename(new_file_name))[0] if new_file_name else "generated"
            print(new_file_base)

            bdd_lines = [
                l.strip() for l in bdd_content.splitlines()
                if re.match(r"^\s*(Given|When|Then|And|But)\s+", l, re.IGNORECASE)
            ]
            bdd_steps_text = "\n".join(f"  - {s}" for s in bdd_lines) if bdd_lines else "(see BDD Content below)"

            prompt = f"""
            You are an expert QA automation engineer specialized in Python Pytest framework with Selenium WebDriver.
            You are creating a BRAND NEW {file_type.upper()} file: {new_file_name}

            ══ GOLDEN RULES (read before generating anything) ══════════════════════════
            1. SCOPE IS BDD-ONLY.
               Generate code ONLY for the actions/elements mentioned in the BDD steps below.
               Every method/step/locator you add must map to a specific BDD step.
               Do NOT invent, anticipate, or include anything not in the BDD steps.

            2. EXACT BDD STEPS IN SCOPE:
            {bdd_steps_text}

            3. STYLE FROM EXISTING FILE — CONTENT FROM BDD.
               Read the existing file to learn: imports, class structure, method naming,
               indentation, wait pattern, locator reference style.
               Use that style for the new file.
               Do NOT copy any method bodies, class names, or locator names from the existing file.

            4. LOCATORS.
               Use ONLY the locators listed under "Element Locators" in the Supporting Information.
               Do NOT import or reference locator classes that belong to other pages.
               Do NOT invent locator names or selector values.

            5. IMPORTS.
               Import only what the new file actually uses.
               Do NOT carry over unused imports from the existing file.

            6. ONE METHOD = ONE BDD ACTION.
               No utility methods. No assertion helpers. No combined multi-step methods.

            7. CONSISTENT WAIT PATTERN.
               Every element interaction must use self.wait.until(...).
               Use EC.presence_of_element_located for inputs/text reads.
               Use EC.element_to_be_clickable for buttons, links, tabs.
               Never use time.sleep or implicit waits.

            ══ FILE-TYPE-SPECIFIC INSTRUCTIONS ═════════════════════════════════════════
            {file_type_instructions}

            ══ OUTPUT FORMAT ════════════════════════════════════════════════════════════
            Return a single valid JSON object.
            The key MUST be exactly: "{new_file_name}"
            The value is the complete file content as a string.
            {{
                "{new_file_name}": "... complete file content ..."
            }}

            {SELENIUM_STANDARDS_PYTHON}

            {LOCATOR_USAGE_STANDARDS}

            ══ INPUTS ═══════════════════════════════════════════════════════════════════
            Supporting Information (credentials, locators, project path):
            {support_content}

            BDD Content (ONLY implement what is in these steps):
            {bdd_content}

            Existing File (STYLE REFERENCE ONLY — do NOT copy names, bodies, or imports):
            {file_content}
            """

        else:
            if base_name.endswith("_page.py"):
                file_type = "page"
                file_type_instructions = """
                    You are EXTENDING an existing PAGE OBJECT file.

                    SCOPE RULES:
                    - Add ONLY the methods required by the NEW BDD steps provided.
                    - Do NOT add, rename, or remove any existing methods.
                    - Do NOT add methods for steps that already exist in the file.
                    - Every new method must correspond to exactly one BDD step action.
                    - Do NOT invent methods for actions not present in the BDD steps.

                    STYLE RULES (match the existing file exactly):
                    - snake_case method names: enter_*, click_*, get_*, is_*, wait_for_*
                    - Use self.wait.until(EC.presence_of_element_located(...)) for inputs.
                    - Use self.wait.until(EC.element_to_be_clickable(...)).click() for buttons.
                    - Reference locators as ClassName.LOCATOR_NAME — never inline selector strings.
                    - Use consistent 4-space indentation inside the class.
                    - If a required locator is missing: # TODO: add <element_name> to locators file

                    IMPORT RULES:
                    - Only add imports that are strictly needed for the new methods.
                    - Do NOT import locator classes from other pages.

                    OUTPUT: Return the COMPLETE file — all existing code preserved first,
                    then new methods appended inside the existing class.
                """
                default_fallback_file = _infer_folder(base_name)

            elif base_name.endswith("locators.py"):
                file_type = "locators"
                file_type_instructions = """
                    You are EXTENDING an existing LOCATORS file.

                    SCOPE RULES:
                    - Add ONLY the locators referenced by the NEW BDD steps.
                    - Use ONLY locators from the Supporting Information / Element Locators section.
                    - Do NOT add locators for elements that are not in the BDD steps.
                    - Do NOT modify, rename, or remove any existing locator.

                    STYLE RULES (match the existing file exactly):
                    - UPPER_SNAKE_CASE attribute names inside the existing class.
                    - Each locator: NAME = (By.STRATEGY, "value")
                    - Use only By.* strategies already present in the file.
                    - Brief inline comment only when the name alone is unclear.

                    OUTPUT: Return the COMPLETE file — all existing code preserved first,
                    then new locator attributes appended at the end of the existing class.
                """
                default_fallback_file = _infer_folder(base_name)

            elif base_name.endswith("_steps.py"):
                file_type = "steps"
                file_type_instructions = """
                    You are EXTENDING an existing STEP DEFINITIONS file.

                    SCOPE RULES:
                    - Add ONLY the step functions for NEW BDD steps not already present.
                    - Do NOT duplicate or modify any existing step decorator or function.
                    - Each new step function maps 1-to-1 to one BDD step.
                    - Do NOT add steps for actions not in the BDD content.

                    STYLE RULES (match the existing file exactly):
                    - Same decorator style: @given/@when/@then from pytest-bdd or behave.
                    - Same fixture/context injection pattern.
                    - Step decorator text must match the BDD step text exactly.
                    - Function body: a single page-object method call — no logic or assertions.
                    - Only add new page imports if a new page class is needed.

                    OUTPUT: Return the COMPLETE file — all existing code preserved first,
                    then new step functions appended at the end.
                """
                default_fallback_file = _infer_folder(base_name)

            else:
                file_type = "generic"
                file_type_instructions = """
                    You are EXTENDING an existing general Python test file.

                    SCOPE RULES:
                    - Add ONLY the code required by the new BDD steps.
                    - Do NOT modify or remove existing code.

                    STYLE RULES (match the existing file exactly):
                    - Same import ordering, naming conventions, fixture patterns.
                    - No extra helpers or utilities unless directly needed.

                    OUTPUT: Return the COMPLETE file — existing code first, new code appended.
                """
                default_fallback_file = _infer_folder(base_name) if base_name else "tests/generated_test.py"

            bdd_lines = [
                l.strip() for l in bdd_content.splitlines()
                if re.match(r"^\s*(Given|When|Then|And|But)\s+", l, re.IGNORECASE)
            ]
            bdd_steps_text = "\n".join(f"  - {s}" for s in bdd_lines) if bdd_lines else "(see BDD Content below)"

            prompt = f"""
                    You are an expert QA automation engineer extending an EXISTING {file_type.upper()} file.

                    ══ GOLDEN RULES ════════════════════════════════════════════════════════════
                    1. SCOPE IS BDD-ONLY.
                       Add code ONLY for the NEW BDD steps listed below.
                       Do NOT add anything that is not directly required by those steps.

                    2. NEW BDD STEPS TO IMPLEMENT:
                    {bdd_steps_text}

                    3. PRESERVE EVERYTHING.
                       Do NOT modify, rename, remove, or reformat any existing code.
                       Return the COMPLETE file: all existing code first, new code appended.

                    4. CONSISTENT STYLE.
                       New code must be indistinguishable in style from the existing code.
                       Copy the exact wait pattern, locator reference style, and naming convention.

                    5. LOCATOR DISCIPLINE.
                       Use ONLY locators listed under "Element Locators" in Supporting Information.
                       Do NOT reference locator classes that belong to other page objects.
                       Do NOT invent locator names.

                    6. IMPORT DISCIPLINE.
                       Only add imports strictly required for the new code.
                       Do NOT import unused modules or locator classes from other pages.

                    7. STEP DECORATOR QUOTING — CRITICAL FOR pytest-bdd.
                       Step decorator text must EXACTLY match the feature file step text.
                       NEVER use double quotes (") inside step decorator strings — they break
                       pytest-bdd's regex step matcher.
                       Use single quotes inside decorators for any named references:
                         CORRECT:   @when("I click on the 'Monitoring' tab")
                         INCORRECT: @when('I click on the "Monitoring" tab')
                       If the feature file step already uses double quotes, you MUST rewrite
                       both the feature file step AND the decorator to use single quotes.

                    ══ FILE-TYPE-SPECIFIC INSTRUCTIONS ═════════════════════════════════════════
                    {file_type_instructions}

                    ══ OUTPUT FORMAT ════════════════════════════════════════════════════════════
                    Return a JSON object with one key per modified file.
                    Each value is the COMPLETE file content (existing + new additions).
                    {{
                        "{base_name}": "... complete updated file content ..."
                    }}

                    {SELENIUM_STANDARDS_PYTHON}

                    {LOCATOR_USAGE_STANDARDS}

                    ══ INPUTS ═══════════════════════════════════════════════════════════════════
                    Supporting Information (credentials, locators, project path):
                    {support_content}

                    BDD Content (implement ONLY the new steps):
                    {bdd_content}

                    Existing File Content (preserve this exactly, extend it below):
                    {file_content}
                    """

        result = await call_ai(prompt, provider)

        try:
            branch = "NewFile" if new_file_name else "ExistingFile"
            with open(f"LastAIResponse_{branch}.txt", "w", encoding="utf-8") as _dbg:
                _dbg.write(f"Provider: {provider}\nFile Mode: Existing\nBranch: {branch}\n")
                _dbg.write(f"base_name: {base_name}\nnew_file_name: {new_file_name}\n\n")
                _dbg.write("=== RAW AI RESPONSE ===\n")
                _dbg.write(result or "(empty)")
        except Exception:
            pass

        parsed_result = parse_json_result(result, default_fallback_file)

        if isinstance(parsed_result, dict):
            if new_file_name:
                parsed_result = normalize_keys(parsed_result)
                parsed_result = sanitize_step_quoting(parsed_result)
            else:
                existing_code = file_content if isinstance(file_content, str) else ""
                try:
                    parsed_result = merge_into_existing(file_type, existing_code, parsed_result)
                except Exception:
                    pass
                parsed_result = normalize_keys(parsed_result)
                parsed_result = sanitize_step_quoting(parsed_result)

        return parsed_result

    else:
        return {"error": "Unknown file mode detected in support_content."}


async def generate_python_behave(
    bdd_content: str, support_content: str, file_content: str, provider: str
) -> dict:
    prompt = f"""
    You are an expert QA automation engineer specialized in Python Behave framework with Selenium WebDriver.

    Based on the following BDD content, generate a complete Behave test structure.
    Use the supporting information provided (base URL, credentials, element locators) in the generated code.

    Return the results as a JSON object containing ALL of these files:
    - features/*.feature           (BDD feature files)
    - features/steps/*_steps.py    (step definitions)
    - environment.py               (hooks: before_all, after_all, before_scenario, after_scenario)
    - requirements.txt             (all required packages)

    Do not include explanations, only valid code files as JSON.

    Example JSON response:
    {{
        "features/login.feature": "...",
        "features/steps/login_steps.py": "...",
        "environment.py": "...",
        "requirements.txt": "..."
    }}

    {SELENIUM_STANDARDS_PYTHON}

    {LOCATOR_USAGE_STANDARDS}

    Supporting Information:
    {support_content}

    BDD Content:
    {bdd_content}
    """
    result = await call_ai(prompt, provider)
    return parse_json_result(result, "features/generated.feature")


async def generate_java_testng(
    bdd_content: str, support_content: str, file_content: str, provider: str
) -> dict:
    prompt = f"""
    You are an expert QA automation engineer specialized in Java TestNG framework with Selenium WebDriver.

    Based on the following BDD content, generate a complete TestNG test structure.
    Use the supporting information provided (base URL, credentials, element locators) in the generated code.

    Return the results as a JSON object containing ALL of these files:
    - src/test/java/tests/*.java          (TestNG test classes)
    - src/test/java/pages/*.java          (Page Object Model classes)
    - src/test/java/utils/BaseTest.java   (base test setup/teardown)
    - src/test/resources/testng.xml       (TestNG suite configuration)
    - pom.xml                             (Maven dependencies - Selenium 4, TestNG, WebDriverManager)

    Do not include explanations, only valid code files as JSON.

    Example JSON response:
    {{
        "src/test/java/tests/LoginTest.java": "...",
        "src/test/java/pages/LoginPage.java": "...",
        "src/test/java/utils/BaseTest.java": "...",
        "src/test/resources/testng.xml": "...",
        "pom.xml": "..."
    }}

    {SELENIUM_STANDARDS_JAVA}

    {LOCATOR_USAGE_STANDARDS}

    Supporting Information:
    {support_content}

    BDD Content:
    {bdd_content}
    """
    result = await call_ai(prompt, provider)
    return parse_json_result(result, "src/test/java/tests/GeneratedTest.java")


async def generate_java_cucumber(
    bdd_content: str, support_content: str, file_content: str, provider: str
) -> dict:
    prompt = f"""
    You are an expert QA automation engineer specialized in Java Cucumber framework with Selenium WebDriver.

    Based on the following BDD content, generate a complete Cucumber-JVM test structure.
    Use the supporting information provided (base URL, credentials, element locators) in the generated code.

    Return the results as a JSON object containing ALL of these files:
    - src/test/resources/features/*.feature    (BDD feature files)
    - src/test/java/steps/*Steps.java          (step definitions)
    - src/test/java/pages/*.java               (Page Object Model classes)
    - src/test/java/hooks/Hooks.java           (before/after hooks)
    - src/test/java/runner/TestRunner.java     (Cucumber runner)
    - pom.xml                                  (Maven dependencies - Selenium 4, Cucumber, WebDriverManager)

    Do not include explanations, only valid code files as JSON.

    Example JSON response:
    {{
        "src/test/resources/features/login.feature": "...",
        "src/test/java/steps/LoginSteps.java": "...",
        "src/test/java/pages/LoginPage.java": "...",
        "src/test/java/hooks/Hooks.java": "...",
        "src/test/java/runner/TestRunner.java": "...",
        "pom.xml": "..."
    }}

    {SELENIUM_STANDARDS_JAVA}

    {LOCATOR_USAGE_STANDARDS}

    Supporting Information:
    {support_content}

    BDD Content:
    {bdd_content}
    """
    result = await call_ai(prompt, provider)
    return parse_json_result(result, "src/test/resources/features/generated.feature")


async def generate_playwright_typescript(
    bdd_content: str, support_content: str, file_content: str, provider: str
) -> dict:
    prompt = f"""
    You are an expert QA automation engineer specialized in Playwright with TypeScript.

    Based on the following BDD content, generate a complete Playwright TypeScript test structure.
    Use the supporting information provided (base URL, credentials, element locators) in the generated code.

    Return the results as a JSON object containing ALL of these files:
    - tests/*.spec.ts          (Playwright test files)
    - pages/*.ts               (Page Object Model classes)
    - fixtures/base.ts         (custom fixtures and setup)
    - playwright.config.ts     (Playwright configuration)
    - package.json             (all required dependencies)

    Do not include explanations, only valid code files as JSON.

    Example JSON response:
    {{
        "tests/login.spec.ts": "...",
        "pages/LoginPage.ts": "...",
        "fixtures/base.ts": "...",
        "playwright.config.ts": "...",
        "package.json": "..."
    }}

    {PLAYWRIGHT_STANDARDS_TS}

    {LOCATOR_USAGE_STANDARDS}

    Supporting Information:
    {support_content}

    BDD Content:
    {bdd_content}
    """
    result = await call_ai(prompt, provider)
    return parse_json_result(result, "tests/generated.spec.ts")


async def route_code_generation(
    language:        str,
    framework:       str,
    bdd_content:     str,
    support_content: str,
    file_content:    str,
    provider:        str,
) -> dict:
    key = (language.strip().lower(), framework.strip().lower())

    routes = {
        ("python",     "pytest"):      generate_python_pytest,
        ("python",     "behave"):      generate_python_behave,
        ("java",       "testng"):      generate_java_testng,
        ("java",       "cucumber"):    generate_java_cucumber,
        ("playwright", "typescript"):  generate_playwright_typescript,
    }

    generator = routes.get(key)
    if not generator:
        raise ValueError(f"Unsupported combination: {language} - {framework}")

    return await generator(bdd_content, support_content, file_content, provider)


async def async_task_generate_code(
    task_id:         str,
    language:        str,
    framework:       str,
    bdd_content:     str,
    support_content: str,
    file_content:    str,
    provider:        str,
):
    try:
        tasks_store[task_id]["status"] = "processing"
        files_dict = await route_code_generation(
            language, framework, bdd_content, support_content, file_content, provider
        )
        tasks_store[task_id]["status"] = "done"
        tasks_store[task_id]["result"] = files_dict
    except Exception as e:
        tasks_store[task_id]["status"] = "error"
        tasks_store[task_id]["result"] = str(e)


@app.post("/generate-agent-code")
async def generate_agent_code(req: GenerateCodeRequest, background_tasks: BackgroundTasks):
    provider = req.ai_provider.strip().lower() if req.ai_provider.strip() else DEFAULT_AI_PROVIDER

    hash_input = "|".join([
        req.project_name, req.language, req.framework,
        req.project_path, req.bdd_content,
        req.support_content, req.file_content, provider,
    ])
    content_hash = hashlib.sha256(hash_input.encode()).hexdigest()

    if content_hash in dedup_index:
        existing_task_id = dedup_index[content_hash]
        if existing_task_id in tasks_store:
            existing = tasks_store[existing_task_id]
            # Only reuse if still active or done; discard errored tasks so user can retry
            if existing["status"] in ("pending", "processing", "done"):
                return {
                    "task_id": existing_task_id,
                    "message": f"Returning existing task ({existing['status']}).",
                }

    task_id = str(uuid.uuid4())
    tasks_store[task_id] = {"status": "pending", "result": None, "error": None}
    dedup_index[content_hash] = task_id

    background_tasks.add_task(
        async_task_generate_code,
        task_id,
        req.language,
        req.framework,
        req.bdd_content,
        req.support_content,
        req.file_content,
        provider,
    )

    return {"task_id": task_id, "message": "Task started."}


@app.get("/task-result/{task_id}")
async def get_task_result(task_id: str):
    if task_id not in tasks_store:
        raise HTTPException(status_code=404, detail="Task ID not found")
    record = tasks_store[task_id]
    return {
        "task_id": task_id,
        "status":  record["status"],
        "result":  record.get("result"),
        "error":   record.get("error"),
    }


@app.get("/task/{task_id}")
async def get_task(task_id: str):
    if task_id not in tasks_store:
        raise HTTPException(status_code=404, detail="Task ID not found")
    record = tasks_store[task_id]
    return {
        "task_id": task_id,
        "status":  record["status"],
        "result":  record.get("result"),
        "error":   record.get("error"),
    }


@app.delete("/task/{task_id}")
async def clear_task(task_id: str):
    tasks_store.pop(task_id, None)
    to_remove = [h for h, tid in dedup_index.items() if tid == task_id]
    for h in to_remove:
        dedup_index.pop(h, None)
    return {"task_id": task_id, "cleared": True}


@app.post("/generate-bdd-scenarios")
async def generate_bdd_scenarios(req: GenerateBDDScenariosRequest):
    provider = req.ai_provider.strip().lower() if req.ai_provider.strip() else DEFAULT_AI_PROVIDER
    
    prompt = f"""
    You are an expert QA Automation Engineer.
    Given the following requirements, generate a complete and professional BDD Gherkin .feature file.
    
    Requirements:
    {req.requirements}
    
    Rules:
    1. Use standard Gherkin syntax (Feature, Scenario, Given, When, Then, And).
    2. Ensure scenarios cover positive and negative cases if applicable.
    3. Return ONLY the content of the .feature file. Do NOT include markdown code fences or any other text.
    """
    
    try:
        content = await call_ai(prompt, provider, expect_json=False)
        # Strip any accidental markdown fences
        content = re.sub(r"```(?:gherkin|feature)?\s*", "", content).replace("```", "").strip()
        return {"status": "success", "filename": "scenarios.feature", "content": content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/generate-formatted-test-cases")
async def generate_formatted_test_cases(req: GenerateTestCasesRequest):
    provider = req.ai_provider.strip().lower() if req.ai_provider.strip() else DEFAULT_AI_PROVIDER
    
    prompt = f"""
    You are an expert QA Automation Engineer.
    Your task is to map the provided requirements into the provided test case template format.
    
    Requirements:
    {req.requirements}
    
    Template Format:
    {req.template}
    
    Instructions:
    1. Extract test cases from the Requirements.
    2. Cover all types of scenarios: Positive, Negative, Edge Cases, Boundary conditions, field level validations, Business rule validations, Error handling, Regression impact scenarios
    3. Return the test cases as a JSON object with a single key "test_cases".
    4. The value for "test_cases" MUST be a JSON array of objects.
    5. Each object in the array represents ONE test case.
    6. CRITICAL FATAL INSTRUCTION: The keys in each JSON object MUST STRICTLY be the exact column headers specified in the Template / Sample Format. 
       - Every column header provided in the Template MUST be a key in every JSON object.
       - Do NOT invent your own keys.
       - NEVER use standard keys like 'Step No', 'Pre-requisite', 'Test Data', 'Action' unless they are explicitly in the template.
       - You MUST map the test cases exactly to whatever keys the user provided in the Template string.
       - Example: if the template provided is ["A", "B", "C"], your JSON must be {{ "test_cases": [ {{ "A": "...", "B": "...", "C": "..." }} ] }}
    7. Include NO other information. Your entire response must be standard, parseable JSON.
    """
    
    try:
        content = await call_ai(prompt, provider, expect_json=False)
        # Strip any accidental markdown fences
        content = re.sub(r"```[a-z]*\s*", "", content).replace("```", "").strip()
        print(content)
        return {"status": "success", "content": content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend:app", host="127.0.0.1", port=8000, reload=True)