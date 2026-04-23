import os
import logging
import sqlite3
from db.app_db import DB_PATH

logger = logging.getLogger("ProjectBootstrapper")

class BootstrapperEngine:
    """
    Dynamically generates the scaffolding for a new QA Automation project.
    """
    @staticmethod
    def generate_project(project_name, base_path, tool, language, framework, package_manager, url, username, password):
        target_dir = os.path.join(base_path, project_name)
        
        try:
            os.makedirs(target_dir, exist_ok=False)
            logger.info(f"Created project directory: {target_dir}")
        except FileExistsError:
            logger.warning(f"Project directory already exists: {target_dir}")
            return False, f"Project directory '{target_dir}' already exists."
        except Exception as e:
            logger.error(f"Failed to create project directory: {e}")
            return False, f"System error creating directory: {str(e)}"

        # Standardize language naming
        lang_map = {
            "JS / TS": "TypeScript",
            "JavaScript": "TypeScript",
            "JS": "TypeScript",
            "TS": "TypeScript"
        }
        search_lang = lang_map.get(language, language)

        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 1. Find the template
            cursor.execute(
                "SELECT id FROM ProjectTemplates WHERE tool = ? AND language = ? AND framework = ?",
                (tool, search_lang, framework)
            )
            template_row = cursor.fetchone()
            
            if not template_row:
                logger.warning(f"No template found in DB for: {tool}/{search_lang}/{framework}")
                return False, f"No project template found in database for {tool} + {search_lang} + {framework}. Please contact admin to ingest this template."

            template_id = template_row['id']

            # 2. Fetch all files
            cursor.execute("SELECT file_path, file_content, is_binary FROM TemplateFiles WHERE template_id = ?", (template_id,))
            template_files = cursor.fetchall()

            if not template_files:
                return False, "Found template metadata but no files are associated with it."

            # 3. Process and write files
            for file_record in template_files:
                rel_path = file_record['file_path']
                content = file_record['file_content']
                is_binary = file_record['is_binary']

                # Create full destination path
                full_path = os.path.join(target_dir, rel_path)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)

                if is_binary:
                    pass
                else:
                    if content:
                        content = content.replace("{{PROJECT_NAME}}", project_name)
                        content = content.replace("{{BASE_URL}}", url or "https://example.com")
                        content = content.replace("{{USERNAME}}", username or "admin")
                        content = content.replace("{{PASSWORD}}", password or "password123")
                    
                    with open(full_path, 'w', encoding='utf-8') as f:
                        f.write(content or "")

            # 4. Ensure mandatory empty directories for AI generation exist
            # For Playwright/TS (Cucumber)
            if search_lang == "TypeScript":
                for folder in ["test/pageObjects", "test/stepDefinitions", "test/features", "Results"]:
                    os.makedirs(os.path.join(target_dir, folder), exist_ok=True)
            # For Python (Generic)
            elif search_lang == "Python":
                for folder in ["pages", "tests", "features", "Results"]:
                    os.makedirs(os.path.join(target_dir, folder), exist_ok=True)

            conn.close()
            logger.info(f"Successfully generated project '{project_name}' from database template.")
            return True, target_dir

        except Exception as e:
            logger.error(f"Error during template-based generation: {e}")
            return False, f"Template generation error: {str(e)}"

    @staticmethod
    def execute_smoke_test(project_path, tool, language, framework, package_manager):
        if language == "Python":
            if "Behave" in framework or "Jbehave" in framework:
                cmd = "venv\\Scripts\\python -m behave -f html" if os.name == 'nt' else "venv/bin/python3 -m behave -f html"
            else:
                cmd = "venv\\Scripts\\python -m pytest tests/ --html=Results/report.html" if os.name == 'nt' else "venv/bin/python3 -m pytest tests/ --html=Results/report.html"
        elif language == "Java":
            cmd = "mvn test"
        elif language in ["JS / TS", "JavaScript", "TypeScript"]:
            cmd = "npm test"
        elif language == "C#":
            cmd = 'dotnet test --logger "html;LogFileName=Results/report.html"'
        else:
            return False, "Smoke test not configured for this language."

        try:
            import subprocess
            result = subprocess.run(cmd, cwd=project_path, shell=True, check=True, capture_output=True, text=True)
            if language == "Python" and ("Behave" in framework or "Jbehave" in framework):
                with open(os.path.join(project_path, "Results", "report.html"), "w", encoding="utf-8") as f:
                    f.write(result.stdout)
                return True, "Behave Smoke Test completed successfully. HTML Report generated."
            return True, result.stdout
        except subprocess.CalledProcessError as e:
            if language == "Python" and ("Behave" in framework or "Jbehave" in framework):
                with open(os.path.join(project_path, "Results", "report.html"), "w", encoding="utf-8") as f:
                    f.write(e.stdout)
                return False, "Behave Smoke Test had failures. Check Results/report.html for detailed output."
            return False, e.stderr if e.stderr else e.stdout

    @staticmethod
    def _generate_python_project(target_dir, tool, framework, url, username, password):
        try:
            logger.info("Initializing Python project folders...")
            for folder in ["pages", "tests", "utils", "Results"]:
                os.makedirs(os.path.join(target_dir, folder), exist_ok=True)

            # BDD Folders
            if "Behave" in framework or "Jbehave" in framework:
                os.makedirs(os.path.join(target_dir, "features", "steps"))

            # requirements.txt
            req_content = ""
            if tool == "Selenium":
                req_content = "selenium\npytest\npytest-html\npytest-xdist\nwebdriver-manager\n"
            elif tool == "Playwright":
                req_content = "playwright\npytest\npytest-playwright\npytest-html\npytest-xdist\n"
            
            if "Behave" in framework or "Jbehave" in framework:
                req_content += "behave\nbehave-html-formatter\n"

            with open(os.path.join(target_dir, "requirements.txt"), "w") as f:
                f.write(req_content)

            # conftest.py or Base Setup
            if tool == "Playwright":
                base_page_content = f'''\
class BasePage:
    def __init__(self, page):
        self.page = page

    def navigate(self):
        self.page.goto("{url}")
'''
                test_content = f'''\
import pytest
from pages.login_page import LoginPage

def test_login(page):
    login_page = LoginPage(page)
    login_page.navigate()
    login_page.login("{username}", "{password}")
    assert True # Replace with actual assertion
'''
            else: # Selenium
                base_page_content = f'''\
class BasePage:
    def __init__(self, driver):
        self.driver = driver

    def navigate(self):
        self.driver.get("{url}")
'''
                test_content = f'''\
import pytest
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from pages.login_page import LoginPage

@pytest.fixture()
def driver():
    driver = webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()))
    yield driver
    driver.quit()

def test_login(driver):
    login_page = LoginPage(driver)
    login_page.navigate()
    login_page.login("{username}", "{password}")
    assert True # Replace with actual assertion
'''

            with open(os.path.join(target_dir, "pages", "base_page.py"), "w") as f:
                f.write(base_page_content)

            if "Behave" in framework or "Jbehave" in framework:
                with open(os.path.join(target_dir, "behave.ini"), "w") as f:
                    f.write("[behave.formatters]\nhtml = behave_html_formatter:HTMLFormatter\n")
                with open(os.path.join(target_dir, "features", "smoke.feature"), "w") as f:
                    f.write("Feature: Smoke Test\n\n  Scenario: Load Application\n    Given I launch the application\n    Then The application loads\n")
                with open(os.path.join(target_dir, "features", "environment.py"), "w") as f:
                    f.write("def before_all(context):\n    print('Setup driver')\n\ndef after_all(context):\n    print('Teardown driver')\n")
                with open(os.path.join(target_dir, "features", "steps", "smoke_steps.py"), "w") as f:
                    f.write("from behave import given, then\n\n@given('I launch the application')\ndef step_impl(context):\n    pass\n\n@then('The application loads')\ndef step_impl(context):\n    pass\n")
            else:
                with open(os.path.join(target_dir, "tests", "test_smoke.py"), "w") as f:
                    f.write(test_content)

            # Basic login page
            login_page_content = f'''\
from .base_page import BasePage

class LoginPage(BasePage):
    def login(self, username, password):
        print(f"Logging in with {{username}} and {{password}}")
        # Add actual locators and click actions here
'''
            with open(os.path.join(target_dir, "pages", "login_page.py"), "w") as f:
                f.write(login_page_content)

            return True, "Python project generated successfully."
        except Exception as e:
            logger.error(f"Error generating Python project: {e}")
            return False, f"Python generation error: {str(e)}"

    @staticmethod
    def _generate_java_project(target_dir, project_name, tool, framework, url, username, password):
        os.makedirs(os.path.join(target_dir, "src", "main", "java", "pages"))
        os.makedirs(os.path.join(target_dir, "src", "test", "java", "tests"))
        os.makedirs(os.path.join(target_dir, "Results"))

        if "Cucumber" in framework:
            os.makedirs(os.path.join(target_dir, "src", "test", "resources", "features"))
            os.makedirs(os.path.join(target_dir, "src", "test", "java", "steps"))
            os.makedirs(os.path.join(target_dir, "src", "test", "java", "runners"))

        # pom.xml
        pom_content = f'''<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
    <modelVersion>4.0.0</modelVersion>

    <groupId>com.qa</groupId>
    <artifactId>{project_name.lower()}</artifactId>
    <version>1.0-SNAPSHOT</version>

    <properties>
        <maven.compiler.source>11</maven.compiler.source>
        <maven.compiler.target>11</maven.compiler.target>
    </properties>

    <dependencies>
        <!-- Adding some base dependencies -->
'''
        if tool == "Selenium":
             pom_content += '''
        <dependency>
            <groupId>org.seleniumhq.selenium</groupId>
            <artifactId>selenium-java</artifactId>
            <version>4.10.0</version>
        </dependency>
'''
        elif tool == "Playwright":
             pom_content += '''
        <dependency>
            <groupId>com.microsoft.playwright</groupId>
            <artifactId>playwright</artifactId>
            <version>1.37.0</version>
        </dependency>
'''
        pom_content += '''
        <dependency>
            <groupId>org.testng</groupId>
            <artifactId>testng</artifactId>
            <version>7.8.0</version>
            <scope>test</scope>
        </dependency>
        <!-- Extent Reports Dependency -->
        <dependency>
            <groupId>com.aventstack</groupId>
            <artifactId>extentreports</artifactId>
            <version>5.1.1</version>
        </dependency>
'''
        if "Cucumber" in framework:
             pom_content += '''
        <!-- Cucumber Dependencies -->
        <dependency>
            <groupId>io.cucumber</groupId>
            <artifactId>cucumber-java</artifactId>
            <version>7.13.0</version>
        </dependency>
        <dependency>
            <groupId>io.cucumber</groupId>
            <artifactId>cucumber-testng</artifactId>
            <version>7.13.0</version>
        </dependency>
'''
        pom_content += '''    </dependencies>
</project>
'''
        with open(os.path.join(target_dir, "pom.xml"), "w") as f:
            f.write(pom_content)

        # Base Page
        with open(os.path.join(target_dir, "src", "main", "java", "pages", "BasePage.java"), "w") as f:
            f.write("package pages;\n\npublic class BasePage {\n    // Base setup here\n}\n")
            
        # Extent Reporting Setup
        os.makedirs(os.path.join(target_dir, "src", "main", "java", "utils"), exist_ok=True)
        extent_content = f'''package utils;

import com.aventstack.extentreports.ExtentReports;
import com.aventstack.extentreports.reporter.ExtentSparkReporter;

public class ExtentManager {{
    private static ExtentReports extent;

    public static ExtentReports getInstance() {{
        if (extent == null) {{
            ExtentSparkReporter spark = new ExtentSparkReporter("Results/ExtentReport.html");
            extent = new ExtentReports();
            extent.attachReporter(spark);
        }}
        return extent;
    }}
}}
'''
        with open(os.path.join(target_dir, "src", "main", "java", "utils", "ExtentManager.java"), "w") as f:
            f.write(extent_content)
            
        if "Cucumber" in framework:
            with open(os.path.join(target_dir, "src", "test", "resources", "features", "SmokeTest.feature"), "w") as f:
                f.write("Feature: Smoke Test\n\n  Scenario: Verifying framework setup\n    Given The application is launched\n    Then The page loads successfully\n")
            with open(os.path.join(target_dir, "src", "test", "java", "runners", "TestRunner.java"), "w") as f:
                f.write("package runners;\n\nimport io.cucumber.testng.AbstractTestNGCucumberTests;\nimport io.cucumber.testng.CucumberOptions;\nimport org.testng.annotations.AfterSuite;\nimport utils.ExtentManager;\n\n@CucumberOptions(features = \"src/test/resources/features\", glue = \"steps\")\npublic class TestRunner extends AbstractTestNGCucumberTests {\n    @AfterSuite\n    public void teardown() {\n        ExtentManager.getInstance().flush();\n    }\n}\n")
            with open(os.path.join(target_dir, "src", "test", "java", "steps", "SmokeSteps.java"), "w") as f:
                f.write("package steps;\n\nimport io.cucumber.java.en.Given;\nimport io.cucumber.java.en.Then;\nimport utils.ExtentManager;\nimport com.aventstack.extentreports.ExtentTest;\n\npublic class SmokeSteps {\n    @Given(\"The application is launched\")\n    public void setup() {\n        ExtentTest test = ExtentManager.getInstance().createTest(\"Setup Step\");\n        System.out.println(\"App launched.\");\n        test.pass(\"App launched successfully\");\n    }\n    @Then(\"The page loads successfully\")\n    public void verify() {\n        ExtentTest test = ExtentManager.getInstance().createTest(\"Verify Step\");\n        System.out.println(\"Page loaded.\");\n        test.pass(\"Page loaded successfully\");\n    }\n}\n")
        else:
            with open(os.path.join(target_dir, "src", "test", "java", "tests", "SmokeTest.java"), "w") as f:
                f.write("package tests;\n\nimport org.testng.annotations.Test;\nimport org.testng.annotations.AfterSuite;\nimport utils.ExtentManager;\nimport com.aventstack.extentreports.ExtentTest;\n\npublic class SmokeTest {\n    @Test\n    public void loginTest() {\n        ExtentTest test = ExtentManager.getInstance().createTest(\"Smoke Login Test\");\n        System.out.println(\"Smoke test running...\");\n        test.pass(\"Smoke test passed\");\n    }\n    @AfterSuite\n    public void teardown() {\n        ExtentManager.getInstance().flush();\n    }\n}\n")

        return True, "Java project generated successfully."

    @staticmethod
    def _generate_js_project(target_dir, language, tool, framework, url, username, password):
        os.makedirs(os.path.join(target_dir, "pages"))
        os.makedirs(os.path.join(target_dir, "tests"))

        if "Cucumber" in framework:
            os.makedirs(os.path.join(target_dir, "features", "step_definitions"))

        is_ts = language == "TypeScript"
        ext = "ts" if is_ts else "js"

        deps = ""
        script_test = "npx playwright test"
        if "Cucumber" in framework:
            deps = ',\n    "@cucumber/cucumber": "^9.5.0"'
            script_test = "npx cucumber-js --format html:Results/report.html"
            
        if is_ts:
            deps += ',\n    "typescript": "^5.0.0",\n    "@types/node": "^20.0.0"'

        # package.json
        pkg_content = f'''{{
  "name": "{tool.lower()}-tests",
  "version": "1.0.0",
  "description": "QA Automation Framework",
  "scripts": {{
    "test": "{script_test}"
  }},
  "dependencies": {{}},
  "devDependencies": {{
    "@playwright/test": "^1.37.0"{deps}
  }}
}}'''
        with open(os.path.join(target_dir, "package.json"), "w") as f:
            f.write(pkg_content)

        if "Cucumber" in framework:
            with open(os.path.join(target_dir, f"cucumber.{ext}"), "w") as f:
                f.write("module.exports = { default: '--publish-quiet' };\n")
            with open(os.path.join(target_dir, "features", "smoke.feature"), "w") as f:
                f.write("Feature: Smoke Test\n\n  Scenario: Load Application\n    Given The application is running\n")
            with open(os.path.join(target_dir, "features", "step_definitions", f"smoke_steps.{ext}"), "w") as f:
                f.write("const { Given } = require('@cucumber/cucumber');\n\nGiven('The application is running', async function () {\n  console.log('App running');\n});\n" if not is_ts else "import { Given } from '@cucumber/cucumber';\n\nGiven('The application is running', async function () {\n  console.log('App running');\n});\n")
        else:
            if tool == "Playwright":
                os.makedirs(os.path.join(target_dir, "Results"), exist_ok=True)
                config_content = f'''import {{ defineConfig }} from '@playwright/test';
export default defineConfig({{
  testDir: './tests',
  reporter: [['html', {{ outputFolder: 'Results' }}]],
  use: {{
    headless: false,
    baseURL: '{url}',
  }},
}});
''' if is_ts else f'''const {{ defineConfig }} = require('@playwright/test');
module.exports = defineConfig({{
  testDir: './tests',
  reporter: [['html', {{ outputFolder: 'Results' }}]],
  use: {{
    headless: false,
    baseURL: '{url}',
  }},
}});
'''
                with open(os.path.join(target_dir, f"playwright.config.{ext}"), "w") as f:
                    f.write(config_content)
                    
            code_prefix = "import { test, expect } from '@playwright/test';\n" if is_ts else "const { test, expect } = require('@playwright/test');\n"
            
            with open(os.path.join(target_dir, "tests", f"smoke.spec.{ext}"), "w") as f:
                f.write(f"{code_prefix}\ntest('smoke', async ({{ page }}) => {{\n  console.log('Smoke test');\n  await page.goto('/');\n}});\n")

        return True, "JS/TS project generated successfully."

    @staticmethod
    def _generate_csharp_project(target_dir, tool, framework, url, username, password):
        os.makedirs(os.path.join(target_dir, "Pages"))
        os.makedirs(os.path.join(target_dir, "Tests"))
        os.makedirs(os.path.join(target_dir, "Results"))

        if "Reqnroll" in framework:
            os.makedirs(os.path.join(target_dir, "Features"))
            os.makedirs(os.path.join(target_dir, "StepDefinitions"))
            os.makedirs(os.path.join(target_dir, "Support"))
            
            with open(os.path.join(target_dir, "Features", "Smoke.feature"), "w") as f:
                f.write("Feature: Smoke Test\n\n  Scenario: Application loads\n    Given App is launched\n")
            with open(os.path.join(target_dir, "StepDefinitions", "SmokeSteps.cs"), "w") as f:
                f.write("using Reqnroll;\n\n[Binding]\npublic class SmokeSteps\n{\n    [Given(\"App is launched\")]\n    public void GivenAppIsLaunched() {}\n}\n")
            csproj = f'''<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net6.0</TargetFramework>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Reqnroll" Version="1.0.0" />
    <PackageReference Include="Reqnroll.NUnit" Version="1.0.0" />
    <PackageReference Include="NUnit" Version="3.13.3" />
  </ItemGroup>
</Project>'''
            with open(os.path.join(target_dir, f"{os.path.basename(target_dir)}.csproj"), "w") as f:
                f.write(csproj)

        return True, "C# project generated successfully."
