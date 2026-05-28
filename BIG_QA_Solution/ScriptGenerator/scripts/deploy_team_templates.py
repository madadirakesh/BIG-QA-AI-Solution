import sqlite3
import os
import json

# Standalone Seeder Script generated for the BIG-QA Team
# Run this to populate your local database with standardized project templates.
#
# Two sources of templates are merged at runtime:
#   1. TEMPLATES_DATA below (legacy inline strings — kept for the first three templates).
#   2. The `templates/` directory next to this file, where each subdirectory is one template
#      stored as real source files (pom.xml, *.java, *.feature, etc.) plus a metadata.json.
#      This is the preferred place to add new templates going forward — files keep IDE syntax
#      highlighting and avoid Python string-escape pain.
#
# Both sources are deduplicated by (tool, language, framework). Files-on-disk templates win on
# tie, so you can override an inline template by creating a directory with matching metadata.

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "local_database.db")
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")

TEMPLATES_DATA = [
  {
    "metadata": {
      "tool": "Playwright",
      "language": "TypeScript",
      "framework": "Cucumber",
      "description": "Enterprise Playwright TS BDD Template",
      "default_run_commands": "npm test -- --tags \"@smoke\"\nnpm run report"
    },
    "files": [
      {
        "file_path": "cucumber.config.js",
        "file_content": "module.exports = {\n  default: {\n    formatOptions: {\n      snippetInterface: \"async-await\"\n    },\n    paths: [\n      \"test/features/*.feature\"\n    ],\n    dryRun: false,\n    require: [\n      \"dist/test/**/*.js\"  // Point this to the compiled JS, not the TS\n    ],\n    format: [\n      \"progress-bar\",\n      \"json:results/cucumber_report.json\"\n    ],\n    parallel: 1,\n    timeout:30000\n}\n}",
        "is_binary": 0
      },
      {
        "file_path": "package.json",
        "file_content": "{\n  \"name\": \"playwright-ts-bdd-framework\",\n  \"version\": \"1.0.0\",\n  \"description\": \"Enterprise Playwright TypeScript BDD Framework\",\n  \"main\": \"index.js\",\n  \"scripts\": {\n    \"test\": \"node test-runner.js\",\n    \"report\": \"npx ts-node test/utils/reports.ts\"\n  },\n  \"devDependencies\": {\n    \"@cucumber/cucumber\": \"^11.2.0\",\n    \"@playwright/test\": \"^1.40.0\",\n    \"@types/fs-extra\": \"^11.0.4\",\n    \"@types/node\": \"^20.10.0\",\n    \"cucumber-html-reporter\": \"^5.5.0\",\n    \"dotenv\": \"^16.3.1\",\n    \"fs-extra\": \"^11.3.4\",\n    \"install\": \"^0.13.0\",\n    \"npm\": \"^11.12.1\",\n    \"ts-node\": \"^10.9.1\",\n    \"tsx\": \"^4.21.0\",\n    \"typescript\": \"^5.2.2\"\n  }\n}\n",
        "is_binary": 0
      },
      {
        "file_path": ".env",
        "file_content": "APP_URL={{BASE_URL}}\nBROWSER=chromium\nHEADLESS=false\nUSER={{USERNAME}}\nPASSWORD={{PASSWORD}}\n",
        "is_binary": 0
      },
      {
        "file_path": "tsconfig.json",
        "file_content": "{\n  \"compilerOptions\": {\n    /* Modern but Forgiving Standards */\n    \"target\": \"ESNext\",\n    \"module\": \"node16\",\n    \"moduleResolution\": \"node16\",\n    \"lib\": [\"ESNext\", \"DOM\"],\n    \n    \"rootDir\": \".\",\n    \"outDir\": \"./dist\",\n    \n    /* Interoperability & Legacy Bridges */\n    \"esModuleInterop\": true,\n    \"skipLibCheck\": true,\n    \"resolveJsonModule\": true,\n    \"forceConsistentCasingInFileNames\": true,\n    \"strict\": true,\n    \"noImplicitAny\": false\n  },\n  \"include\": [\n    \"test/**/*.ts\",\n    \"cucumber.js\"\n  ],\n  \"exclude\": [\n    \"node_modules\",\n    \"dist\"\n  ]\n}",
        "is_binary": 0
      },
      {
        "file_path": "test-runner.js",
        "file_content": "const { execSync } = require('child_process');\n\nconst timestamp = new Date().toISOString().replace(/[:.]/g, \"-\");\nconst resultDir = `results/${timestamp}`;\nconst extraArgs = process.argv.slice(2).join(' ');\nconst tagArgs = extraArgs ? ` ${extraArgs}` : '';\n\nprocess.env.RESULT_DIR = resultDir;\n\nlet testExitCode = 0;\n\ntry {\n  execSync(`npx cucumber-js \"test/features/**/*.feature\" --require-module ts-node/register --require \"test/hooks/**/*.ts\" --require \"test/stepDefinitions/**/*.ts\" --require \"test/pageObjects/**/*.ts\" --require \"test/utils/configReader.ts\" --format json:${resultDir}/cucumber_report.json${tagArgs}`, { stdio: 'inherit' });\n} catch (error) {\n  testExitCode = 1;\n}\n\ntry {\n  execSync('npx ts-node test/utils/reports.ts', { stdio: 'inherit' });\n} catch (reportError) {\n  console.error('Failed to generate HTML report:', reportError.message);\n}\n\nprocess.exit(testExitCode);",
        "is_binary": 0
      },
      {
        "file_path": "extent-config.xml",
        "file_content": "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<extentreports>\n    <configuration>\n        <theme>standard</theme>\n        <encoding>UTF-8</encoding>\n        <protocol>https</protocol>\n        <documentTitle>AI Automation Architect - Test Report</documentTitle>\n        <reportName>Regression Suite Execution</reportName>\n        \n        <scripts>\n            <![CDATA[\n                $(document).ready(function() {\n                    // Custom JS to add enterprise logo\n                });\n            ]]>\n        </scripts>\n        <styles>\n            <![CDATA[\n                .brand-logo { background-color: #2c3e50 !important; }\n                .report-name { font-weight: bold; color: #3498db; }\n            ]]>\n        </styles>\n    </configuration>\n</extentreports>",
        "is_binary": 0
      },
      {
        "file_path": "test/utils/reports.ts",
        "file_content": "const reporter = require('cucumber-html-reporter');\nconst fs = require('fs');\nconst path = require('path');\n\nlet resultDir = process.env.RESULT_DIR;\nif (!resultDir) {\n    // Find the latest timestamped folder\n    const resultsPath = path.join(process.cwd(), 'results');\n    const folders = fs.readdirSync(resultsPath).filter(f => fs.statSync(path.join(resultsPath, f)).isDirectory());\n    folders.sort((a, b) => fs.statSync(path.join(resultsPath, b)).mtime - fs.statSync(path.join(resultsPath, a)).mtime);\n    resultDir = folders.length > 0 ? path.join('results', folders[0]) : 'results';\n}\n\nconst jsonPath = path.join(process.cwd(), resultDir, 'cucumber_report.json');\n\n// ARCHITECTURAL CHECK: Prevent crash if tests didn't run\nif (!fs.existsSync(jsonPath)) {\n    console.error(\"\\u274c Execution Error: The JSON report was not generated. Check your test logs above!\");\n    process.exit(0); // Exit cleanly so you can see the actual error\n}\n\nconst options = {\n    theme: 'bootstrap',\n    jsonFile: jsonPath,\n    output: path.join(process.cwd(), resultDir, 'cucumber_report.html'),\n    reportSuiteAsScenarios: true,\n    scenarioTimestamp: true,\n    launchReport: true\n};\n\nreporter.generate(options);",
        "is_binary": 0
      },
      {
        "file_path": "test/utils/configReader.ts",
        "file_content": "import * as dotenv from \"dotenv\";\nimport * as path from \"path\";\n\n// Load .env file from the root directory\ndotenv.config({ path: path.join(__dirname, \"../../.env\") });\n\nexport class ConfigReader {\n    public static getProperty(key: string): string {\n        const value = process.env[key];\n        if (!value) {\n            throw new Error(`Property ${key} not found in .env file`);\n        }\n        return value;\n    }\n\n    public static getEnvUrl(): string {\n        return this.getProperty(\"APP_URL\");\n    }\n}",
        "is_binary": 0
      },
      {
        "file_path": "test/hooks/hooks.ts",
        "file_content": "import { Before, After, Status, BeforeAll, AfterAll } from \"@cucumber/cucumber\";\nimport { chromium, firefox, webkit, Browser, BrowserContext, Page } from \"@playwright/test\";\nconst fs = require('fs-extra');\nimport * as path from \"path\";\nimport { execSync } from \"child_process\";\n\nlet browser: Browser;\nlet context: BrowserContext;\nexport let page: Page;\n\nBeforeAll(async function () {\n    try {\n        console.log(\"🔍 Validating environment dependencies...\");\n        // This check mimics the 'install_dependencies' logic in your invoker\n        execSync(\"npm list @playwright/test\", { stdio: \"ignore\" });\n        // Install Playwright browsers\n        execSync(\"npx playwright install\", { stdio: \"ignore\" });\n    } catch (error) {\n        console.error(\"❌ Required packages missing. Executing emergency install...\");\n        execSync(\"npm install\", { stdio: \"inherit\" });\n        execSync(\"npx playwright install\", { stdio: \"inherit\" });\n    }\n    // Rule 4: Create result folder with timestamp\n    const resultDir = process.env.RESULT_DIR || path.join(process.cwd(), \"results\", new Date().toISOString().replace(/[:.]/g, \"-\"));\n    fs.ensureDirSync(resultDir);\n    fs.ensureDirSync(path.join(resultDir, \"screenshots\"));\n});\n\nBefore(async function () {\n    // Rule 8: Configurable browser\n    const browserType = process.env.BROWSER || \"chromium\";\n    const launchOptions = { headless: process.env.HEADLESS === \"true\" };\n\n    if (browserType === \"firefox\") browser = await firefox.launch(launchOptions);\n    else if (browserType === \"webkit\") browser = await webkit.launch(launchOptions);\n    else browser = await chromium.launch(launchOptions);\n\n    // Bypassing SSL errors as standard QA practice\n    context = await browser.newContext({ ignoreHTTPSErrors: true });\n    page = await context.newPage();\n    this.page = page;\n});\n\nAfter(async function (scenario) {\n    // Rule 6: Screenshot on failure\n    if (scenario.result?.status === Status.FAILED) {\n        const resultDir = process.env.RESULT_DIR || path.join(process.cwd(), \"results\", new Date().toISOString().replace(/[:.]/g, \"-\"));\n        const image = await page.screenshot({ path: path.join(resultDir, \"screenshots\", `${scenario.pickle.name}.png`), fullPage: true });\n        await this.attach(image, \"image/png\");\n    }\n    // Rule 5: Close instances\n    await page.close();\n    await context.close();\n    await browser.close();\n});",
        "is_binary": 0
      },
      {
        "file_path": "test/features/loginFeature.feature",
        "file_content": "Feature: Login Functionality\n\n  Scenario: Verify Successful Login\n    Given I launch the application\n    When I enter valid Username and Password\n    And I click the login button\n    Then I should be redirected to the homepage\n",
        "is_binary": 0
      }
      # {
      #   "file_path": "test/stepDefinitions/loginSteps.ts",
      #   "file_content": "import { Given, When, Then } from \"@cucumber/cucumber\";\nimport { expect } from \"@playwright/test\";\nimport { page } from \"../hooks/hooks\";\n\nGiven('I navigate to the login page', async function () {\n  await page.goto('https://www.saucedemo.com/');\n});\n\nWhen('I enter valid credentials', async function () {\n  await page.locator('[data-test=\"username\"]').fill('standard_user');\n  await page.locator('[data-test=\"password\"]').fill('secret_sauce');\n});\n\nWhen('I click the login button', async function () {\n  await page.locator('[data-test=\"login-button\"]').click();\n});\n\nThen('I should be redirected to the homepage', async function () {\n  await expect(page.locator('.title')).toBeVisible();\n  await expect(page.locator('.title')).toHaveText('Products');\n});\n",
      #   "is_binary": 0
      # }
    ]
  },
  {
    "metadata": {
      "tool": "Playwright",
      "language": "Python",
      "framework": "Behave",
      "description": "Enterprise Playwright Python Behave BDD Template",
      "default_run_commands": "behave"
    },
    "files": [
      {
        "file_path": "requirements.txt",
        "file_content": "behave\nbehave-html-formatter\nplaywright\npytest\npytest-playwright\npytest-html\npython-dotenv\n",
        "is_binary": 0
      },
      {
        "file_path": "behave.ini",
        "file_content": "[behave]\npaths = features\nshow_skipped = false\nformat = pretty\noutfiles = Results/behave_report.txt\nstdout_capture = false\nstderr_capture = false\nlog_capture = false\n\n[behave.formatters]\nhtml = behave_html_formatter:HTMLFormatter\n",
        "is_binary": 0
      },
      {
        "file_path": "features/environment.py",
        "file_content": "import os\nfrom playwright.sync_api import sync_playwright\n\n\ndef before_all(context):\n    context.playwright = sync_playwright().start()\n    browser_name = os.getenv(\"BROWSER\", \"chromium\")\n    headless = os.getenv(\"HEADLESS\", \"false\").lower() == \"true\"\n    browser_type = getattr(context.playwright, browser_name, context.playwright.chromium)\n    context.browser = browser_type.launch(headless=headless)\n    context.page = context.browser.new_page()\n\n\ndef after_all(context):\n    if getattr(context, \"browser\", None):\n        context.browser.close()\n    if getattr(context, \"playwright\", None):\n        context.playwright.stop()\n",
        "is_binary": 0
      },
      {
        "file_path": "features/example.feature",
        "file_content": "Feature: Example smoke test\n\n  Scenario: Open the application\n    Given I open the application\n    Then the page title should be visible\n",
        "is_binary": 0
      },
      {
        "file_path": "features/steps/example_steps.py",
        "file_content": "from behave import given, then\n\n\n@given(\"I open the application\")\ndef step_open_application(context):\n    context.page.goto(context.config.userdata.get(\"app_url\", \"https://www.saucedemo.com\"))\n\n\n@then(\"the page title should be visible\")\ndef step_title_visible(context):\n    assert context.page.title() is not None\n",
        "is_binary": 0
      },
      {
        "file_path": ".env",
        "file_content": "APP_URL={{BASE_URL}}\nBROWSER=chromium\nHEADLESS=false\nUSER={{USERNAME}}\nPASSWORD={{PASSWORD}}\n",
        "is_binary": 0
      }
    ]
  },
  # ─────────────────────────────────────────────────────────────────
  # NEW TEMPLATE: Selenium - Python - Behave
  # ─────────────────────────────────────────────────────────────────
  {
    "metadata": {
      "tool": "Selenium",
      "language": "Python",
      "framework": "Behave",
      "description": "Enterprise Selenium Python Behave BDD Template",
      "default_run_commands": "behave\nbehave --tags=@smoke"
    },
    "files": [
      {
        "file_path": "requirements.txt",
        "file_content": "behave\nbehave-html-formatter\nselenium\nwebdriver-manager\npython-dotenv\nAllure-Behave\n",
        "is_binary": 0
      },
      {
        "file_path": "behave.ini",
        "file_content": "[behave]\npaths = features\nshow_skipped = false\nformat = pretty\noutfiles = Results/behave_report.txt\nstdout_capture = false\nstderr_capture = false\nlog_capture = false\n\n[behave.formatters]\nhtml = behave_html_formatter:HTMLFormatter\n",
        "is_binary": 0
      },
      {
        "file_path": ".env",
        "file_content": "APP_URL={{BASE_URL}}\nBROWSER=chrome\nHEADLESS=false\nUSER={{USERNAME}}\nPASSWORD={{PASSWORD}}\n",
        "is_binary": 0
      },
      {
        "file_path": "features/environment.py",
        "file_content": "import os\nimport base64\nfrom datetime import datetime\nfrom dotenv import load_dotenv\nfrom selenium import webdriver\nfrom selenium.webdriver.chrome.service import Service as ChromeService\nfrom selenium.webdriver.firefox.service import Service as FirefoxService\nfrom selenium.webdriver.edge.service import Service as EdgeService\nfrom webdriver_manager.chrome import ChromeDriverManager\nfrom webdriver_manager.firefox import GeckoDriverManager\nfrom webdriver_manager.microsoft import EdgeChromiumDriverManager\n\nload_dotenv()\n\n\ndef before_all(context):\n    \"\"\"Global setup: runs once before the entire test suite.\"\"\"\n    os.makedirs(\"Results/screenshots\", exist_ok=True)\n    context.base_url = os.getenv(\"APP_URL\", \"https://www.saucedemo.com\")\n    context.headless = os.getenv(\"HEADLESS\", \"false\").lower() == \"true\"\n    context.browser_name = os.getenv(\"BROWSER\", \"chrome\").lower()\n\n\ndef before_scenario(context, scenario):\n    \"\"\"Spin up a fresh browser instance before each scenario.\"\"\"\n    context.driver = _create_driver(context.browser_name, context.headless)\n    context.driver.implicitly_wait(10)\n    context.driver.maximize_window()\n\n\ndef after_scenario(context, scenario):\n    \"\"\"Capture screenshot on failure, then tear down the driver.\"\"\"\n    if scenario.status == \"failed\":\n        timestamp = datetime.now().strftime(\"%Y%m%d_%H%M%S\")\n        safe_name = scenario.name.replace(\" \", \"_\").replace(\"/\", \"-\")\n        screenshot_path = f\"Results/screenshots/{safe_name}_{timestamp}.png\"\n        context.driver.save_screenshot(screenshot_path)\n        # Embed screenshot in Behave HTML report\n        with open(screenshot_path, \"rb\") as img_file:\n            context.embed(\n                mime_type=\"image/png\",\n                data=base64.b64encode(img_file.read()).decode(\"utf-8\"),\n                caption=f\"Failure screenshot: {scenario.name}\",\n            )\n    context.driver.quit()\n\n\ndef after_all(context):\n    \"\"\"Global teardown: runs once after the entire test suite.\"\"\"\n    pass\n\n\ndef _create_driver(browser_name: str, headless: bool):\n    \"\"\"Factory function to create the appropriate WebDriver instance.\"\"\"\n    if browser_name == \"firefox\":\n        options = webdriver.FirefoxOptions()\n        if headless:\n            options.add_argument(\"--headless\")\n        return webdriver.Firefox(\n            service=FirefoxService(GeckoDriverManager().install()),\n            options=options,\n        )\n    elif browser_name == \"edge\":\n        options = webdriver.EdgeOptions()\n        if headless:\n            options.add_argument(\"--headless\")\n        return webdriver.Edge(\n            service=EdgeService(EdgeChromiumDriverManager().install()),\n            options=options,\n        )\n    else:  # Default: Chrome\n        options = webdriver.ChromeOptions()\n        if headless:\n            options.add_argument(\"--headless=new\")\n        options.add_argument(\"--no-sandbox\")\n        options.add_argument(\"--disable-dev-shm-usage\")\n        options.add_argument(\"--ignore-certificate-errors\")\n        return webdriver.Chrome(\n            service=ChromeService(ChromeDriverManager().install()),\n            options=options,\n        )\n",
        "is_binary": 0
      },
      {
        "file_path": "features/login.feature",
        "file_content": "Feature: Login Functionality\n\n  @smoke\n  Scenario: Verify Successful Login\n    Given I launch the application\n    When I enter valid Username and Password\n    And I click the login button\n    Then I should be redirected to the homepage\n",
        "is_binary": 0
      },
      {
        "file_path": "features/steps/login_steps.py",
        "file_content": "import os\nfrom behave import given, when, then\nfrom pages.login_page import LoginPage\nfrom pages.home_page import HomePage\n\n\n@given(\"I launch the application\")\ndef step_launch_application(context):\n    context.driver.get(context.base_url)\n    context.login_page = LoginPage(context.driver)\n\n\n@when(\"I enter valid Username and Password\")\ndef step_enter_credentials(context):\n    username = os.getenv(\"USER\", \"standard_user\")\n    password = os.getenv(\"PASSWORD\", \"secret_sauce\")\n    context.login_page.enter_username(username)\n    context.login_page.enter_password(password)\n\n\n@when(\"I click the login button\")\ndef step_click_login(context):\n    context.login_page.click_login()\n    context.home_page = HomePage(context.driver)\n\n\n@then(\"I should be redirected to the homepage\")\ndef step_verify_homepage(context):\n    assert context.home_page.is_loaded(), (\n        f\"Expected homepage to load, but title was: {context.driver.title}\"\n    )\n",
        "is_binary": 0
      },
      {
        "file_path": "pages/base_page.py",
        "file_content": "from selenium.webdriver.support.ui import WebDriverWait\nfrom selenium.webdriver.support import expected_conditions as EC\nfrom selenium.webdriver.remote.webdriver import WebDriver\nfrom selenium.webdriver.common.by import By\n\n\nclass BasePage:\n    \"\"\"Base Page Object — shared helpers for all page classes.\"\"\"\n\n    DEFAULT_TIMEOUT = 15\n\n    def __init__(self, driver: WebDriver):\n        self.driver = driver\n        self.wait = WebDriverWait(driver, self.DEFAULT_TIMEOUT)\n\n    def find(self, by: By, locator: str):\n        return self.wait.until(EC.presence_of_element_located((by, locator)))\n\n    def click(self, by: By, locator: str):\n        element = self.wait.until(EC.element_to_be_clickable((by, locator)))\n        element.click()\n\n    def type_text(self, by: By, locator: str, text: str):\n        element = self.find(by, locator)\n        element.clear()\n        element.send_keys(text)\n\n    def is_visible(self, by: By, locator: str) -> bool:\n        try:\n            self.wait.until(EC.visibility_of_element_located((by, locator)))\n            return True\n        except Exception:\n            return False\n",
        "is_binary": 0
      },
      {
        "file_path": "pages/login_page.py",
        "file_content": "from selenium.webdriver.common.by import By\nfrom pages.base_page import BasePage\n\n\nclass LoginPage(BasePage):\n    \"\"\"Page Object for the Login page.\"\"\"\n\n    # Locators\n    USERNAME_INPUT = (By.ID, \"txtUserID\")\n    PASSWORD_INPUT = (By.ID, \"txtPassword\")\n    LOGIN_BUTTON   = (By.ID, \"sub\")\n    ERROR_MESSAGE  = (By.CSS_SELECTOR, \"[data-test='error']\")\n\n    def enter_username(self, username: str):\n        self.type_text(*self.USERNAME_INPUT, username)\n\n    def enter_password(self, password: str):\n        self.type_text(*self.PASSWORD_INPUT, password)\n\n    def click_login(self):\n        self.click(*self.LOGIN_BUTTON)\n\n    def get_error_message(self) -> str:\n        return self.find(*self.ERROR_MESSAGE).text\n",
        "is_binary": 0
      },
      {
        "file_path": "pages/home_page.py",
        "file_content": "from selenium.webdriver.common.by import By\nfrom pages.base_page import BasePage\n\n\nclass HomePage(BasePage):\n    \"\"\"Page Object for the Home / Products page.\"\"\"\n\n    # Locators\n    PAGE_TITLE = (By.CSS_SELECTOR, \"div.app-logo-title\")\n\n    def is_loaded(self) -> bool:\n        return self.is_visible(*self.PAGE_TITLE)\n\n    def get_title_text(self) -> str:\n        return self.find(*self.PAGE_TITLE).text\n",
        "is_binary": 0
      },
      {
        "file_path": "utils/config_reader.py",
        "file_content": "import os\nfrom dotenv import load_dotenv\n\nload_dotenv()\n\n\nclass ConfigReader:\n    \"\"\"Centralised helper to read configuration from the .env file.\"\"\"\n\n    @staticmethod\n    def get(key: str, default: str = None) -> str:\n        value = os.getenv(key, default)\n        if value is None:\n            raise EnvironmentError(\n                f\"Required environment variable '{key}' is not set in .env\"\n            )\n        return value\n\n    @staticmethod\n    def get_app_url() -> str:\n        return ConfigReader.get(\"APP_URL\")\n\n    @staticmethod\n    def get_browser() -> str:\n        return ConfigReader.get(\"BROWSER\", \"chrome\")\n\n    @staticmethod\n    def is_headless() -> bool:\n        return ConfigReader.get(\"HEADLESS\", \"false\").lower() == \"true\"\n",
        "is_binary": 0
      }
    ]
  }
]

def _load_filesystem_templates(templates_dir):
    """
    Walks ``templates_dir`` and converts each subdirectory into the same
    ``{"metadata": {...}, "files": [...]}`` shape used by the inline TEMPLATES_DATA list.

    Layout convention for each template directory:
      templates/<some_name>/
        metadata.json                  -- tool, language, framework, description, default_run_commands
        <any file tree>                -- ingested verbatim; file paths become relative to this dir.

    Skipped entries:
      - metadata.json itself (it is parsed into the metadata block, not stored as a file).
      - Editor / OS noise: .DS_Store, __pycache__, .pyc.

    Binary detection: files are read as bytes and decoded as UTF-8; if decoding fails we mark
    is_binary=1 and store an empty content string (matches the existing inline contract — see
    BootstrapperEngine.generate_project, which currently no-ops on is_binary files anyway).
    """
    discovered = []
    if not os.path.isdir(templates_dir):
        return discovered

    for entry in sorted(os.listdir(templates_dir)):
        tpl_root = os.path.join(templates_dir, entry)
        if not os.path.isdir(tpl_root):
            continue

        meta_path = os.path.join(tpl_root, "metadata.json")
        if not os.path.isfile(meta_path):
            print(f"  ! Skipping '{entry}': missing metadata.json")
            continue

        with open(meta_path, "r", encoding="utf-8") as f:
            raw_meta = json.load(f)
        # Drop any leading-underscore explanatory keys (e.g. "_comment") so they do not leak
        # into the ProjectTemplates row.
        metadata = {k: v for k, v in raw_meta.items() if not k.startswith("_")}

        files = []
        for dirpath, _dirnames, filenames in os.walk(tpl_root):
            for fname in filenames:
                if fname in ("metadata.json", ".DS_Store") or fname.endswith(".pyc"):
                    continue
                if "__pycache__" in dirpath:
                    continue

                abs_path = os.path.join(dirpath, fname)
                # Store paths relative to the template root, using forward slashes so the
                # scaffolded project layout is identical on Windows and Unix.
                rel_path = os.path.relpath(abs_path, tpl_root).replace(os.sep, "/")

                try:
                    with open(abs_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    is_binary = 0
                except UnicodeDecodeError:
                    content = ""
                    is_binary = 1

                files.append({
                    "file_path": rel_path,
                    "file_content": content,
                    "is_binary": is_binary,
                })

        # Stable file order so re-seeds produce identical DB state.
        files.sort(key=lambda f: f["file_path"])
        discovered.append({"metadata": metadata, "files": files})
        print(f"  + Loaded template from disk: {entry} -> "
              f"{metadata.get('tool')}/{metadata.get('language')}/{metadata.get('framework')} "
              f"({len(files)} files)")

    return discovered


def _merge_templates(inline, from_disk):
    """
    Combines the two template sources. Disk templates win on (tool, language, framework) tie
    so an on-disk override silently replaces the inline definition.
    """
    by_key = {}
    for tpl in inline:
        m = tpl["metadata"]
        by_key[(m["tool"], m["language"], m["framework"])] = tpl
    for tpl in from_disk:
        m = tpl["metadata"]
        by_key[(m["tool"], m["language"], m["framework"])] = tpl
    return list(by_key.values())


def seed():
    print(f"Connecting to database at: {DB_PATH}")
    # Pull in any filesystem-backed templates before opening the DB connection so failures
    # while reading the disk tree are reported up-front without leaving a stale transaction.
    disk_templates = _load_filesystem_templates(TEMPLATES_DIR)
    templates = _merge_templates(TEMPLATES_DATA, disk_templates)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ProjectTemplates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool TEXT NOT NULL,
                language TEXT NOT NULL,
                framework TEXT NOT NULL,
                description TEXT,
                default_run_commands TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS TemplateFiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id INTEGER,
                file_path TEXT NOT NULL,
                file_content TEXT,
                is_binary INTEGER DEFAULT 0,
                FOREIGN KEY (template_id) REFERENCES ProjectTemplates (id)
            )
        """)

        print("Seeding templates...")
        # Iterate the merged list (inline + on-disk). See _merge_templates above.
        for t in templates:
            meta = t["metadata"]
            cursor.execute(
                "SELECT id FROM ProjectTemplates WHERE tool=? AND language=? AND framework=?",
                (meta["tool"], meta["language"], meta["framework"])
            )
            exists = cursor.fetchone()
            if exists:
                template_id = exists[0]
                print(f"  - Template {meta['tool']}/{meta['language']} exists. Updating metadata...")
                cursor.execute(
                    "UPDATE ProjectTemplates SET description=?, default_run_commands=? WHERE id=?",
                    (meta["description"], meta.get("default_run_commands", ""), template_id)
                )
                # Refresh files for existing templates
                cursor.execute("DELETE FROM TemplateFiles WHERE template_id=?", (template_id,))
                for f in t["files"]:
                    cursor.execute(
                        "INSERT INTO TemplateFiles (template_id, file_path, file_content, is_binary) VALUES (?, ?, ?, ?)",
                        (template_id, f["file_path"], f["file_content"], f["is_binary"])
                    )
                continue

            cursor.execute(
                "INSERT INTO ProjectTemplates (tool, language, framework, description, default_run_commands) VALUES (?, ?, ?, ?, ?)",
                (meta["tool"], meta["language"], meta["framework"], meta["description"], meta.get("default_run_commands", ""))
            )
            new_id = cursor.lastrowid

            print(f"  + Ingesting {meta['tool']}/{meta['language']} (ID: {new_id})")
            for f in t["files"]:
                cursor.execute(
                    "INSERT INTO TemplateFiles (template_id, file_path, file_content, is_binary) VALUES (?, ?, ?, ?)",
                    (new_id, f["file_path"], f["file_content"], f["is_binary"])
                )

        conn.commit()
        print("\nSuccess: Database seeded with team templates.")
    except Exception as e:
        conn.rollback()
        print(f"\nError during seeding: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    seed()