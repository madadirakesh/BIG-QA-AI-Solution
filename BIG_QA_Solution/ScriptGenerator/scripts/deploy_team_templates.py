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
      "description": "Enterprise Playwright TS BDD Template"
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
        "file_content": "APP_URL=https://www.saucedemo.com\nBROWSER=chromium\nHEADLESS=false\nADMIN_USER=standard_user\nADMIN_PASS=secret_sauce\n",
        "is_binary": 0
      },
      {
        "file_path": "tsconfig.json",
        "file_content": "{\n  \"compilerOptions\": {\n    /* Modern but Forgiving Standards */\n    \"target\": \"ESNext\",\n    \"module\": \"node16\",\n    \"moduleResolution\": \"node16\",\n    \"lib\": [\"ESNext\", \"DOM\"],\n    \n    \"rootDir\": \".\",\n    \"outDir\": \"./dist\",\n    \n    /* Interoperability & Legacy Bridges */\n    \"esModuleInterop\": true,\n    \"skipLibCheck\": true,\n    \"resolveJsonModule\": true,\n    \"forceConsistentCasingInFileNames\": true,\n    \"strict\": true,\n    \"noImplicitAny\": false\n  },\n  \"include\": [\n    \"test/**/*.ts\",\n    \"cucumber.js\"\n  ],\n  \"exclude\": [\n    \"node_modules\",\n    \"dist\"\n  ]\n}",
        "is_binary": 0
      },
      {
        "file_path": "test-runner.js",
        "file_content": "const { execSync } = require('child_process');\n\nconst timestamp = new Date().toISOString().replace(/[:.]/g, \"-\");\nconst resultDir = `results/${timestamp}`;\nconst extraArgs = process.argv.slice(2).join(' ');\nconst tagArgs = extraArgs ? ` ${extraArgs}` : '';\n\nprocess.env.RESULT_DIR = resultDir;\n\ntry {\n  execSync(`npx cucumber-js test/features/*.feature --require-module ts-node/register --require test/hooks/*.ts --require test/stepDefinitions/*.ts --require test/pageObjects/*.ts --require test/utils/configReader.ts --format json:${resultDir}/cucumber_report.json${tagArgs}`, { stdio: 'inherit' });\n} catch (error) {\n  process.exit(1);\n}",
        "is_binary": 0
      },
      {
        "file_path": "extent-config.xml",
        "file_content": "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<extentreports>\n    <configuration>\n        <theme>standard</theme>\n        <encoding>UTF-8</encoding>\n        <protocol>https</protocol>\n        <documentTitle>AI Automation Architect - Test Report</documentTitle>\n        <reportName>Regression Suite Execution</reportName>\n        \n        <scripts>\n            <![CDATA[\n                $(document).ready(function() {\n                    // Custom JS to add enterprise logo\n                });\n            ]]>\n        </scripts>\n        <styles>\n            <![CDATA[\n                .brand-logo { background-color: #2c3e50 !important; }\n                .report-name { font-weight: bold; color: #3498db; }\n            ]]>\n        </styles>\n    </configuration>\n</extentreports>",
        "is_binary": 0
      },
      {
        "file_path": "test/utils/reports.ts",
        "file_content": "const reporter = require('cucumber-html-reporter');\nconst fs = require('fs');\nconst path = require('path');\n\nlet resultDir = process.env.RESULT_DIR;\nif (!resultDir) {\n    // Find the latest timestamped folder\n    const resultsPath = path.join(process.cwd(), 'results');\n    const folders = fs.readdirSync(resultsPath).filter(f => fs.statSync(path.join(resultsPath, f)).isDirectory());\n    folders.sort((a, b) => fs.statSync(path.join(resultsPath, b)).mtime - fs.statSync(path.join(resultsPath, a)).mtime);\n    resultDir = folders.length > 0 ? path.join('results', folders[0]) : 'results';\n}\n\nconst jsonPath = path.join(process.cwd(), resultDir, 'cucumber_report.json');\n\n// ARCHITECTURAL CHECK: Prevent crash if tests didn't run\nif (!fs.existsSync(jsonPath)) {\n    console.error(\"\u274c Execution Error: The JSON report was not generated. Check your test logs above!\");\n    process.exit(0); // Exit cleanly so you can see the actual error\n}\n\nconst options = {\n    theme: 'bootstrap',\n    jsonFile: jsonPath,\n    output: path.join(process.cwd(), resultDir, 'cucumber_report.html'),\n    reportSuiteAsScenarios: true,\n    scenarioTimestamp: true,\n    launchReport: true\n};\n\nreporter.generate(options);",
        "is_binary": 0
      },
      {
        "file_path": "test/utils/configReader.ts",
        "file_content": "import * as dotenv from \"dotenv\";\nimport * as path from \"path\";\n\n// Load .env file from the root directory\ndotenv.config({ path: path.join(__dirname, \"../../.env\") });\n\nexport class ConfigReader {\n    public static getProperty(key: string): string {\n        const value = process.env[key];\n        if (!value) {\n            throw new Error(`Property ${key} not found in .env file`);\n        }\n        return value;\n    }\n\n    public static getEnvUrl(): string {\n        return this.getProperty(\"APP_URL\");\n    }\n}",
        "is_binary": 0
      },
      {
        "file_path": "test/hooks/hooks.ts",
        "file_content": "import { Before, After, Status, BeforeAll, AfterAll } from \"@cucumber/cucumber\";\nimport { chromium, firefox, webkit, Browser, BrowserContext, Page } from \"@playwright/test\";\nconst fs = require('fs-extra');\nimport * as path from \"path\";\nimport { execSync } from \"child_process\";\n\nlet browser: Browser;\nlet context: BrowserContext;\nexport let page: Page;\n\nBeforeAll(async function () {\n    try {\n        console.log(\"\ud83d\udd0d Validating environment dependencies...\");\n        // This check mimics the 'install_dependencies' logic in your invoker\n        execSync(\"npm list @playwright/test\", { stdio: \"ignore\" });\n        // Install Playwright browsers\n        execSync(\"npx playwright install\", { stdio: \"ignore\" });\n    } catch (error) {\n        console.error(\"\u274c Required packages missing. Executing emergency install...\");\n        execSync(\"npm install\", { stdio: \"inherit\" });\n        execSync(\"npx playwright install\", { stdio: \"inherit\" });\n    }\n    // Rule 4: Create result folder with timestamp\n    const resultDir = process.env.RESULT_DIR || path.join(process.cwd(), \"results\", new Date().toISOString().replace(/[:.]/g, \"-\"));\n    fs.ensureDirSync(resultDir);\n    fs.ensureDirSync(path.join(resultDir, \"screenshots\"));\n});\n\nBefore(async function () {\n    // Rule 8: Configurable browser\n    const browserType = process.env.BROWSER || \"chromium\";\n    const launchOptions = { headless: process.env.HEADLESS === \"true\" };\n\n    if (browserType === \"firefox\") browser = await firefox.launch(launchOptions);\n    else if (browserType === \"webkit\") browser = await webkit.launch(launchOptions);\n    else browser = await chromium.launch(launchOptions);\n\n    // Bypassing SSL errors as standard QA practice\n    context = await browser.newContext({ ignoreHTTPSErrors: true });\n    page = await context.newPage();\n});\n\nAfter(async function (scenario) {\n    // Rule 6: Screenshot on failure\n    if (scenario.result?.status === Status.FAILED) {\n        const resultDir = process.env.RESULT_DIR || path.join(process.cwd(), \"results\", new Date().toISOString().replace(/[:.]/g, \"-\"));\n        const image = await page.screenshot({ path: path.join(resultDir, \"screenshots\", `${scenario.pickle.name}.png`), fullPage: true });\n        await this.attach(image, \"image/png\");\n    }\n    // Rule 5: Close instances\n    await page.close();\n    await context.close();\n    await browser.close();\n});",
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
        # Ensure tables exist
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ProjectTemplates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool TEXT NOT NULL,
                language TEXT NOT NULL,
                framework TEXT NOT NULL,
                description TEXT
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
            meta = t['metadata']
            # Check if exists to avoid duplicates
            cursor.execute(
                "SELECT id FROM ProjectTemplates WHERE tool=? AND language=? AND framework=?",
                (meta['tool'], meta['language'], meta['framework'])
            )
            exists = cursor.fetchone()
            if exists:
                print(f"  - Template {meta['tool']}/{meta['language']} already exists. Skipping.")
                continue

            cursor.execute(
                "INSERT INTO ProjectTemplates (tool, language, framework, description) VALUES (?, ?, ?, ?)",
                (meta['tool'], meta['language'], meta['framework'], meta['description'])
            )
            new_id = cursor.lastrowid
            
            print(f"  + Ingesting {meta['tool']}/{meta['language']} (ID: {new_id})")
            for f in t['files']:
                cursor.execute(
                    "INSERT INTO TemplateFiles (template_id, file_path, file_content, is_binary) VALUES (?, ?, ?, ?)",
                    (new_id, f['file_path'], f['file_content'], f['is_binary'])
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
