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

CRITICAL STYLE HARMONIZATION (For Existing Projects):
- If "EXISTING STEP DEFINITIONS" or "EXISTING PAGE OBJECTS" are provided in the Supporting Information, you MUST analyze them first.
- Mirror their exact style: match their imports, class structures, method naming conventions (e.g., snake_case), wait strategies, helper usages, and comment styles.
- New page objects must inherit from the same base classes (if applicable).
- New step definitions must use the same step decorator styles (e.g. `@given`, `@when`, `@then`) and parameter conventions (e.g., passing the browser fixture).
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

CRITICAL STYLE HARMONIZATION (For Existing Projects):
- If "EXISTING STEP DEFINITIONS" or "EXISTING PAGE OBJECTS" are provided in the Supporting Information, you MUST analyze them first.
- Mirror their exact style: match their package statements, imports, annotation patterns (e.g., @Given, @When, @Then), variable naming, class structures, and PageFactory/PageObject initialization patterns.
- New page objects must inherit from any base page class used by existing pages.
"""

PLAYWRIGHT_STANDARDS_TS = """
IMPORTANT - Follow Playwright TypeScript standards strictly:

Required imports in every file:
    import { test, expect } from '@playwright/test';
    import { Page, Locator, Browser, BrowserContext } from '@playwright/test';  (as needed)

If generating BDD step definitions for Cucumber, ensure you include:
    import { Given, When, Then } from '@cucumber/cucumber';
    import { expect } from '@playwright/test';
    // Use an appropriate page fixture or state manager for Playwright in Cucumber (e.g., a global page object or passing page via custom World).
    // Do NOT mix Playwright's `test` runner (`test.step`, `test.page`) inside Cucumber steps.
Example : Use page.locator('[data-test="username"]') for locator in typescript test automation framework as primary.
if not able to generate as above use these Playwright locator strategies as alternate:
    page.getByRole('button', { name: 'Submit' })
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
    export class LoginPage {
        readonly page: Page;
        readonly usernameInput: Locator;

        constructor(page: Page) {
            this.page = page;
            this.usernameInput =page.locator('[data-test="username"]');
        }
    }

CRITICAL - URL & Credentials (.env via ConfigReader):
    - For steps that launch/navigate to the application URL or enter credentials, do NOT
      hardcode the URL, username, or password unless the scenario/test data provides them.
    - Import and use the existing ConfigReader utility (test/utils/configReader.ts):
          import { ConfigReader } from '../utils/configReader';   // adjust relative path
          await this.page.goto(ConfigReader.getEnvUrl());          // APP_URL
          await this.usernameInput.fill(ConfigReader.getProperty('USER'));
          await this.passwordInput.fill(ConfigReader.getProperty('PASSWORD'));
    - This MUST work the same way it does in the Selenium Java template - never special-case
      TypeScript/Playwright into hardcoded values.

CRITICAL - Page Object Methods:
    - Generate ONE corresponding method per unique action/step mentioned in the BDD scenarios.
    - Every action in a Step Definition MUST call a corresponding method in the Page Object.
    - Each method must perform exactly one action (single responsibility).
    - Use async methods for all actions.
    Example:
    async enterUsername(username: string) {
        await this.usernameInput.fill(username);
    }
    async clickSubmit() {
        await this.submitButton.click();
    }

CRITICAL - File Structure & Naming Convention:
    - Page Object files MUST be placed in a `pageObjects/` directory (e.g., `test/pageObjects/loginPage.ts`).
    - Step Definition files MUST be placed in a `stepDefinitions/` directory (e.g., `test/stepDefinitions/loginSteps.ts`).
    - Use camelCase for folder names (`pageObjects`, `stepDefinitions`) and file names (`loginPage.ts`, `loginSteps.ts`).
    - Do NOT generate root-level files for page objects or step definitions.

NEVER use:
    Selenium-style locators or methods
    document.querySelector() or other DOM methods
    Non-async calls to Playwright methods

CRITICAL STYLE HARMONIZATION (For Existing Projects):
- If "EXISTING STEP DEFINITIONS" or "EXISTING PAGE OBJECTS" are provided in the Supporting Information, you MUST analyze them first.
- Mirror their exact style: match their imports (e.g., ESM import vs CommonJS require), test runner pattern (e.g. Playwright Test `@playwright/test` vs Cucumber-JS wrapper), locator creation style (`page.locator` vs standard `page.getBy*`), naming conventions (camelCase vs snake_case), and async/await usage.
- New page objects must align with the exact structure of existing ones, including constructor signatures.
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

ENV_CONFIG_STANDARDS = """
CRITICAL - URL & CREDENTIALS RESOLUTION (read the .env via ConfigReader):

This rule applies to EVERY language/tool combination equally (Playwright TypeScript, Selenium/
Playwright Java, Python, C#) - do NOT treat TypeScript/Playwright differently from Java.

When a BDD step launches or navigates to the application URL (e.g. "I launch the application",
"I navigate to the login page", "Given I am on the home page", "open the URL") OR enters
credentials (e.g. "I enter valid Username and Password", "I login as a valid user", "enter
username/password"), resolve the value in this STRICT priority order:

  1. If the value is explicitly given in the BDD scenario step text, a Scenario Outline
     Examples table, or a data table (i.e. provided as test data), use that literal value.
  2. OTHERWISE you MUST read it from the project's .env file through the project's ConfigReader
     utility (listed under "Project Reusable Utilities"). NEVER hardcode a URL, username, or
     password, and NEVER invent placeholder/example values when a ConfigReader utility exists.

Standard .env keys shipped by the project templates:
    APP_URL   -> the base/application URL
    USER      -> the username
    PASSWORD  -> the password (may be AES-encrypted as "ENC:..."; ConfigReader decrypts it)
    BROWSER, HEADLESS, CRED_KEY -> runtime config (don't hardcode these either)

Use the EXISTING ConfigReader utility - never recreate or re-import a parallel one. Correct calls
per stack:
  - Playwright TypeScript: import { ConfigReader } from '<relative-path>/utils/configReader';
        await this.page.goto(ConfigReader.getEnvUrl());            // APP_URL
        await loginPage.login(ConfigReader.getProperty('USER'), ConfigReader.getProperty('PASSWORD'));
  - Selenium / Playwright Java: import utils.ConfigReader;
        loginPage.open(ConfigReader.getAppUrl());                  // APP_URL
        loginPage.login(ConfigReader.getProperty("USER"), ConfigReader.getProperty("PASSWORD"));
  - Python (Selenium/Playwright): from utils.config_reader import ConfigReader
        page.goto(ConfigReader.get_app_url())                      # APP_URL
        login_page.login(ConfigReader.get("USER"), ConfigReader.get("PASSWORD"))
  - C# Reqnroll: using <Namespace>.Utils;
        loginPage.Open(ConfigReader.GetAppUrl());                  // APP_URL
        loginPage.Login(ConfigReader.GetProperty("USER"), ConfigReader.GetProperty("PASSWORD"));

If (and only if) no ConfigReader utility appears under "Project Reusable Utilities", read the
environment directly instead of hardcoding: process.env.APP_URL (TypeScript), os.getenv (Python),
System.getenv / dotenv (Java), Environment.GetEnvironmentVariable (C#).
"""

def get_pytest_new_suite_prompt(file_templates: dict, support_content: str, bdd_content: str) -> str:
    """
    Returns the prompt for generating a complete Pytest-BDD test suite.
    # Used in file: BIG_QA_Solution/ScriptGenerator/api/backend.py
    # Used under function: PythonPytestGenerator.generate
    """
    return f"""
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
        - Use single-quoted decorator strings: @when('I click on the \\'Monitoring\\' tab')
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

def get_pytest_new_file_in_existing_suite_prompt(file_type: str, new_file_name: str, bdd_steps_text: str, file_type_instructions: str, support_content: str, bdd_content: str, file_content: str) -> str:
    """
    Returns the prompt for generating a new file inside an existing test suite.
    # Used in file: BIG_QA_Solution/ScriptGenerator/api/backend.py
    # Used under function: PythonPytestGenerator.generate
    """
    return f"""
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

def get_pytest_extend_existing_file_prompt(file_type: str, bdd_steps_text: str, file_type_instructions: str, base_name: str, support_content: str, bdd_content: str, file_content: str) -> str:
    """
    Returns the prompt for extending an existing file with new test generation code.
    # Used in file: BIG_QA_Solution/ScriptGenerator/api/backend.py
    # Used under function: PythonPytestGenerator.generate
    """
    return f"""
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

def get_python_behave_prompt(support_content: str, bdd_content: str) -> str:
    """
    Returns the prompt for generating Behave tests.
    # Used in file: BIG_QA_Solution/ScriptGenerator/api/backend.py
    # Used under function: PythonBehaveGenerator.generate
    """
    return f"""
        You are an expert QA automation engineer specialized in Python Behave framework with Selenium WebDriver.
        Based on the following BDD content, generate a complete Behave test structure.

        {SELENIUM_STANDARDS_PYTHON}
        {LOCATOR_USAGE_STANDARDS}

        Supporting Information:
        {support_content}

        BDD Content:
        {bdd_content}
        """

def get_universal_script_generation_prompt(framework: str, tool: str, language: str, standards: str, support_content: str, bdd_content: str) -> str:
    """
    Returns the prompt for generating tests using the Universal Script Generator.
    # Used in file: BIG_QA_Solution/ScriptGenerator/api/backend.py
    # Used under function: UniversalScriptGenerator.generate
    """
    return f"""
        You are an expert QA automation engineer specialized in the {framework} framework using {tool} with {language}.
        Your task: generate a COMPLETE, FULLY WORKING test suite based on the provided content.

        ── OUTPUT FORMAT ──────────────────────────────────────────────────────────────
        Return ONLY a single valid JSON object. Keys are relative file paths, values are the complete file contents as strings.
        
        CRITICAL RULES:
        1. If 'DO NOT generate the .feature file' is in Supporting Information, omit the .feature file from output.
        2. If 'DO NOT generate Page Object classes' is in Supporting Information, omit Page Object files and reuse the provided locals.
        3. ALWAYS look for and call/invoke reusable functions from Project Reusable Utilities instead of rewriting logic if they match the step needs.
        4. DO NOT GENERATE boilerplate configuration files (e.g. pom.xml, package.json, playwright.config.ts, tsconfig.json, .csproj, DriverFactory, Hooks, conftest.py, specflow.json). ONLY generate actual script layer files (Feature, Step Definitions, Page Objects).
        5. If 'DB Locators' are provided natively mapped to [Page Object: X] headers, YOU MUST create the exact Page Object class matching 'X' and populate it exclusively with those exact locators. Under NO circumstances should you hallucinate or generate dynamic locators for elements that exist in [Page Object: X]. If an element needed for a step is NOT in the DB Locators, generate a new locator for it. But ALWAYS prioritize using the provided DB Locators first.
        6. Follow the 'Project Layout Mappings (CRITICAL)' to exactly match the directory structure of the generated files. Check these mappings before formatting the JSON file keys. E.g., if Feature Files must go to 'src/test/resources/features', the output JSON key must be 'src/test/resources/features/MyFeature.feature'.
        7. Do not generate Step definitions if the step definition is already defined in 'EXISTING STEP DEFINITIONS' in the Supporting Information. Only generate NEW step definitions that are missing.
        8. For BDD Step Definitions, NEVER use 'And', 'Or', or 'But' as the step annotation keyword (e.g., @And, @But). Always substitute them with the appropriate 'Given', 'When', or 'Then' keyword that logically corresponds to the step's preceding context in the scenario.

        {standards}
        {LOCATOR_USAGE_STANDARDS}
        {ENV_CONFIG_STANDARDS}

        Supporting Information:
        {support_content}

        BDD Content:
        {bdd_content}
        """
