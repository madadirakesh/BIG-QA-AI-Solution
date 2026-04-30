import sqlite3
import os

# Standalone Seeder Script generated for the BIG-QA Team
# Run this to populate your local database with standardized project templates.

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "local_database.db")

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
        "file_content": "module.exports = {\n  default: {\n    formatOptions: {\n      snippetInterface: \"async-await\"\n    },\n    paths: [\n      \"test/features/*.feature\"\n    ],\n    dryRun: false,\n    require: [\n      \"dist/test/**/*.js\"  // Point this to the compiled JS, not the TS\n    ],\n    format: [\n      \"progress-bar\",\n      \"json:results/cucumber_report.json\"\n    ],\n    parallel: 1\n  }\n}",
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
        "file_content": "const { execSync } = require('child_process');\n\nconst timestamp = new Date().toISOString().replace(/[:.]/g, \"-\");\nconst resultDir = `results/${timestamp}`;\nconst extraArgs = process.argv.slice(2).join(' ');\nconst tagArgs = extraArgs ? ` ${extraArgs}` : '';\n\nprocess.env.RESULT_DIR = resultDir;\n\ntry {\n  execSync(`npx cucumber-js \"test/features/**/*.feature\" --require-module ts-node/register --require \"test/hooks/**/*.ts\" --require \"test/stepDefinitions/**/*.ts\" --require \"test/pageObjects/**/*.ts\" --require \"test/utils/configReader.ts\" --format json:${resultDir}/cucumber_report.json${tagArgs}`, { stdio: 'inherit' });\n} catch (error) {\n  process.exit(1);\n}",
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
        "file_content": "import { Before, After, Status, BeforeAll, AfterAll } from \"@cucumber/cucumber\";\nimport { chromium, firefox, webkit, Browser, BrowserContext, Page } from \"@playwright/test\";\nconst fs = require('fs-extra');\nimport * as path from \"path\";\nimport { execSync } from \"child_process\";\n\nlet browser: Browser;\nlet context: BrowserContext;\nexport let page: Page;\n\nBeforeAll(async function () {\n    try {\n        console.log(\"🔍 Validating environment dependencies...\");\n        // This check mimics the 'install_dependencies' logic in your invoker\n        execSync(\"npm list @playwright/test\", { stdio: \"ignore\" });\n        // Install Playwright browsers\n        execSync(\"npx playwright install\", { stdio: \"ignore\" });\n    } catch (error) {\n        console.error(\"❌ Required packages missing. Executing emergency install...\");\n        execSync(\"npm install\", { stdio: \"inherit\" });\n        execSync(\"npx playwright install\", { stdio: \"inherit\" });\n    }\n    // Rule 4: Create result folder with timestamp\n    const resultDir = process.env.RESULT_DIR || path.join(process.cwd(), \"results\", new Date().toISOString().replace(/[:.]/g, \"-\"));\n    fs.ensureDirSync(resultDir);\n    fs.ensureDirSync(path.join(resultDir, \"screenshots\"));\n});\n\nBefore(async function () {\n    // Rule 8: Configurable browser\n    const browserType = process.env.BROWSER || \"chromium\";\n    const launchOptions = { headless: process.env.HEADLESS === \"true\" };\n\n    if (browserType === \"firefox\") browser = await firefox.launch(launchOptions);\n    else if (browserType === \"webkit\") browser = await webkit.launch(launchOptions);\n    else browser = await chromium.launch(launchOptions);\n\n    // Bypassing SSL errors as standard QA practice\n    context = await browser.newContext({ ignoreHTTPSErrors: true });\n    page = await context.newPage();\n});\n\nAfter(async function (scenario) {\n    // Rule 6: Screenshot on failure\n    if (scenario.result?.status === Status.FAILED) {\n        const resultDir = process.env.RESULT_DIR || path.join(process.cwd(), \"results\", new Date().toISOString().replace(/[:.]/g, \"-\"));\n        const image = await page.screenshot({ path: path.join(resultDir, \"screenshots\", `${scenario.pickle.name}.png`), fullPage: true });\n        await this.attach(image, \"image/png\");\n    }\n    // Rule 5: Close instances\n    await page.close();\n    await context.close();\n    await browser.close();\n});",
        "is_binary": 0
      },
      {
        "file_path": "test/features/loginFeature.feature",
        "file_content": "Feature: Login Functionality\n\n  Scenario: Successful Login\n    Given I navigate to the login page\n    When I enter valid credentials\n    And I click the login button\n    Then I should be redirected to the homepage\n",
        "is_binary": 0
      },
      {
        "file_path": "test/stepDefinitions/loginSteps.ts",
        "file_content": "import { Given, When, Then } from \"@cucumber/cucumber\";\nimport { expect } from \"@playwright/test\";\nimport { page } from \"../hooks/hooks\";\n\nGiven('I navigate to the login page', async function () {\n  await page.goto('https://www.saucedemo.com/');\n});\n\nWhen('I enter valid credentials', async function () {\n  await page.locator('[data-test=\"username\"]').fill('standard_user');\n  await page.locator('[data-test=\"password\"]').fill('secret_sauce');\n});\n\nWhen('I click the login button', async function () {\n  await page.locator('[data-test=\"login-button\"]').click();\n});\n\nThen('I should be redirected to the homepage', async function () {\n  await expect(page.locator('.title')).toBeVisible();\n  await expect(page.locator('.title')).toHaveText('Products');\n});\n",
        "is_binary": 0
      }
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
  }
]

def seed():
    print(f"Connecting to database at: {DB_PATH}")
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
        for t in TEMPLATES_DATA:
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