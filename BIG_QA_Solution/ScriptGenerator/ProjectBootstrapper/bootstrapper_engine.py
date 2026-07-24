import os
import re
import logging
import sqlite3
import subprocess
#from BIG_QA_Solution.ScriptGenerator.db.app_db import DB_PATH
from db.app_db import DB_PATH
# Import works whether this module is loaded flat (bootstrapper_ui.py adds ProjectBootstrapper/ to
# sys.path) or as a package (app.py imports ProjectBootstrapper.bootstrapper_engine).
try:
    from versions_catalog import resolve_versions, FALLBACK_VERSIONS
except ModuleNotFoundError:
    from ProjectBootstrapper.versions_catalog import resolve_versions, FALLBACK_VERSIONS

# Per-project credential encryption. generate_key() mints a random AES-256 key for THIS project
# (written into its .env as CRED_KEY); encrypt_secret() stores the password as "ENC:<token>".
# The generated project decrypts both itself at run time using its own CRED_KEY — see
# utils.crypto_util for the token format and the per-language decrypt code in the templates.
# Try the flat package import first, then the longer paths, matching how this app loads siblings.
try:
    from utils.crypto_util import encrypt_secret, generate_key
except ModuleNotFoundError:
    try:
        from ScriptGenerator.utils.crypto_util import encrypt_secret, generate_key
    except ModuleNotFoundError:
        from BIG_QA_Solution.ScriptGenerator.utils.crypto_util import encrypt_secret, generate_key

logger = logging.getLogger("ProjectBootstrapper")


def _to_artifact_id(project_name):
    """
    Convert a free-form project name into a Maven-safe artifactId.

    Maven requires artifactId to match [A-Za-z0-9_\\-.]+ — spaces and most punctuation are
    rejected. Users typically type human-friendly names like "First Java App", so we sanitise
    here rather than forcing the user into a stricter naming convention on the modal.

    Rule:
      - Replace any run of disallowed characters with a single dash.
      - Lowercase the result so coordinates look conventional (Maven convention).
      - Trim leading/trailing dashes so we never produce "-foo-".
      - Fall back to "qa-project" if the whole name was disallowed characters (e.g. "!!!").

    The original project_name is still used for the on-disk directory name and for the
    {{PROJECT_NAME}} placeholder, so the user's chosen casing survives where it doesn't break
    a downstream tool.
    """
    sanitized = re.sub(r'[^A-Za-z0-9_.-]+', '-', project_name).strip('-').lower()
    return sanitized or 'qa-project'

class BootstrapperEngine:
    """
    Dynamically generates the scaffolding for a new QA Automation project.
    """
    @staticmethod
    def _sec_str(val):
        import json
        return json.dumps(val or "")[1:-1].replace("'", "\\'")

    @staticmethod
    def generate_project(project_name, base_path, tool, language, framework, package_manager, url, username, password, version_profile=None):
        # version_profile: label of a validated combo (versions_catalog); None -> default profile.
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
            "Typescript": "TypeScript",
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

            # Resolve the version profile once; falls back to the catalog default / FALLBACK_VERSIONS.
            versions = resolve_versions(tool, search_lang, framework, version_profile)

            def _ver(key):
                return versions.get(key) or FALLBACK_VERSIONS.get(key, "")

            # Mint one AES-256 key for THIS project. It is written into the project's .env as
            # CRED_KEY (via the {{CRED_KEY}} placeholder + the .env post-processing below) and is
            # the same key used to encrypt the PASSWORD here — so the generated project can
            # decrypt its own password at run time with no dependency on this app.
            cred_key = generate_key()

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
                        # {{PROJECT_NAME}} stays as the user typed it (used in display labels and
                        # the on-disk directory). {{ARTIFACT_ID}} is sanitised for Maven / npm
                        # package-name contexts where spaces and most punctuation are illegal.
                        content = content.replace("{{PROJECT_NAME}}", project_name)
                        # Merge of two independent fixes:
                        #   - HEAD added {{ARTIFACT_ID}} — Maven-safe ID for pom.xml's <artifactId>
                        #     (e.g. "First Java App" -> "first-java-app"). See _to_artifact_id().
                        #   - main wrapped user-supplied values with _sec_str() — escapes characters
                        #     that would break the generated file (quotes/newlines) or enable
                        #     injection. Both are needed; they fix different problems.
                        # ARTIFACT_ID does NOT need _sec_str(): _to_artifact_id() already
                        # sanitises to [A-Za-z0-9_.-]+ which is XML/JSON safe by construction.
                        content = content.replace("{{ARTIFACT_ID}}", _to_artifact_id(project_name))
                        content = content.replace("{{BASE_URL}}", BootstrapperEngine._sec_str(url or "https://example.com"))
                        content = content.replace("{{USERNAME}}", BootstrapperEngine._sec_str(username or "admin"))
                        # CRED_KEY is this project's AES key (base64). It is not secret-escaped —
                        # base64 is .env-safe — and the generated project reads it to decrypt below.
                        content = content.replace("{{CRED_KEY}}", cred_key)
                        # PASSWORD is stored encrypted-at-rest as "ENC:<token>"; the generated
                        # project decrypts it at run time with CRED_KEY. The token is base64, so it
                        # needs no _sec_str() escaping. {{PASSWORD}} only appears in .env templates.
                        content = content.replace("{{PASSWORD}}", encrypt_secret(password or "password123", cred_key))
                        # Dependency-version placeholders ({{PLAYWRIGHT_VERSION}}, {{SELENIUM_VERSION}},
                        # {{TYPESCRIPT_VERSION}}, {{REQNROLL_VERSION}}, {{JAVA_VERSION}}, ...). Driven by
                        # the resolved version profile so a new version key added to versions_catalog
                        # works here with no code change: each key K maps to the placeholder
                        # {{<K>_VERSION}} (upper-cased). Trusted catalog values, no escaping needed.
                        for _vkey in set(versions) | set(FALLBACK_VERSIONS):
                            if _vkey == "label":
                                continue
                            content = content.replace("{{" + _vkey.upper() + "_VERSION}}", _ver(_vkey))
                    
                    with open(full_path, 'w', encoding='utf-8') as f:
                        f.write(content or "")

            # Ensure .env file contains and is updated with the correct user details
            env_file_path = os.path.join(target_dir, ".env")
            if os.path.exists(env_file_path):
                try:
                    with open(env_file_path, 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                    
                    # PASSWORD is encrypted-at-rest (ENC:<token>) and CRED_KEY is the project key
                    # that decrypts it; APP_URL/USER are not secret so they stay plaintext. Mirrors
                    # the placeholder substitution above for the case where the template ships a
                    # populated .env (or omits the CRED_KEY line — then it is appended here).
                    updates = {
                        'APP_URL': url or "https://example.com",
                        'USER': username or "admin",
                        'CRED_KEY': cred_key,
                        'PASSWORD': encrypt_secret(password or "password123", cred_key)
                    }
                    
                    new_lines = []
                    seen = set()
                    
                    for line in lines:
                        stripped = line.strip()
                        if stripped and not stripped.startswith('#') and '=' in line:
                            parts = line.split('=', 1)
                            key = parts[0].strip()
                            if key in updates:
                                new_lines.append(f"{key}={updates[key]}\n")
                                seen.add(key)
                            else:
                                new_lines.append(line)
                        else:
                            new_lines.append(line)
                            
                    for key, val in updates.items():
                        if key not in seen:
                            if new_lines and not new_lines[-1].endswith('\n'):
                                new_lines[-1] += '\n'
                            new_lines.append(f"{key}={val}\n")
                            
                    with open(env_file_path, 'w', encoding='utf-8') as f:
                        f.writelines(new_lines)
                    logger.info(f"Successfully updated/added APP_URL, USER, PASSWORD in {env_file_path}")
                except Exception as e:
                    logger.error(f"Failed to post-process .env file: {e}")

            # 4. Ensure mandatory empty directories for AI generation exist.
            # The template ingestion only creates parent directories implicitly when it writes
            # a file (see the os.makedirs(os.path.dirname(full_path), ...) call above), so any
            # folder the user expects to see at scaffold time but that has no seed file must be
            # created here. The Script Developer wizard later writes step/page files into these.
            # For Playwright/TS (Cucumber)
            if search_lang == "TypeScript":
                for folder in ["test/pageObjects", "test/stepDefinitions", "test/features", "Results"]:
                    os.makedirs(os.path.join(target_dir, folder), exist_ok=True)
            # For Python (Behave POM): pages/ + utils/ + features/steps/ mirror both Python
            # templates' on-disk layout. (Dropped the old "tests/" folder — that was a pytest-ism;
            # Behave discovers steps under features/steps, never tests/.)
            elif search_lang == "Python":
                for folder in ["pages", "utils", "features/steps", "Results"]:
                    os.makedirs(os.path.join(target_dir, folder), exist_ok=True)
            # For Java (Playwright + Cucumber). Step definitions and page objects start empty so
            # the user immediately sees where AI-generated code will land. Results/ is created
            # at runtime by the Cucumber HTML reporter, but pre-creating it avoids first-run
            # confusion when the user opens the project in their IDE.
            elif search_lang == "Java":
                for folder in [
                    "src/main/java/pageObjects",
                    "src/test/java/stepDefinitions",
                    "Results",
                ]:
                    os.makedirs(os.path.join(target_dir, folder), exist_ok=True)

            # 5. Generate sample test files using provided URL and credentials
            BootstrapperEngine._inject_sample_test(target_dir, search_lang, tool, framework, url, username, password)

            conn.close()
            logger.info(f"Successfully generated project '{project_name}' from database template.")
            return True, target_dir

        except Exception as e:
            logger.error(f"Error during template-based generation: {e}")
            return False, f"Template generation error: {str(e)}"

    @staticmethod
    def _generate_sample_script(target_dir, tool, language, framework):
        import glob
        import asyncio
        import sys
        sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
        try:
            from api.backend import route_code_generation
        except ImportError as e:
            logger.error(f"Could not import backend for AI generation: {e}")
            return
            
        feature_files = glob.glob(os.path.join(target_dir, "**", "*.feature"), recursive=True)
        if not feature_files:
            logger.info("No feature file found to generated framework.")
            return
            
        with open(feature_files[0], "r", encoding="utf-8") as f:
            bdd_content = f.read()
            
        support_content = (
            "File Mode: New\n"
            "DO NOT generate the .feature file\n"
            "DO NOT generate boilerplate configuration files\n"
            "Generate only stepdefinition file and pageobject file.\n"
            "DO NOT generate any comments or mock page code in the step definition file.\n"
            "Use exact casing for 'Given', 'When', 'Then' keywords. DO NOT use all caps like 'GIVEN'.\n"
            "DO NOT use 'And' or 'But' keywords; instead, substitute them with appropriate 'Given', 'When', or 'Then'.\n"
            "Ensure proper imports are present, including importing 'Page' from '@playwright/test' or 'page' from '../hooks/hooks' as needed."
        )
        
        try:
            logger.info(f"Generating sample script for {feature_files[0]}...")
            files_dict = asyncio.run(route_code_generation(
                language=language,
                framework=framework,
                bdd_content=bdd_content,
                support_content=support_content,
                file_content="",
                provider="",
                tool=tool
            ))
            
            for rel_path, file_content in files_dict.items():
                if rel_path.endswith(".feature"):
                    continue
                
                if tool.lower() == "playwright" and language in ["TS / JS", "TypeScript", "JavaScript"]:
                    base_name = os.path.basename(rel_path)
                    name_part, ext_part = os.path.splitext(base_name)
                    base_name = name_part.replace(".", "_") + ext_part
                    if "step" in rel_path.lower() or base_name.lower().endswith("steps.ts") or base_name.lower().endswith("steps.js"):
                        rel_path = f"test/stepDefinitions/{base_name}"
                        import_line = 'import { ConfigReader } from "../utils/configReader";\n'
                        if import_line not in file_content:
                            file_content = import_line + file_content
                        
                        file_content = file_content.replace("../pageobjects/", "../pageObjects/")
                        file_content = file_content.replace("../page_objects/", "../pageObjects/")
                        file_content = file_content.replace("./pageobjects/", "./pageObjects/")
                    elif "page" in rel_path.lower() or base_name.lower().endswith("page.ts") or base_name.lower().endswith("page.js"):
                        rel_path = f"test/pageObjects/{base_name}"

                full_path = os.path.join(target_dir, rel_path)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(file_content)
            logger.info("Successfully generated sample scripts for feature file.")
        except Exception as e:
            logger.error(f"Error during AI generation of sample scripts: {e}")

    @staticmethod
    def _run_command(cmd, cwd, timeout=1800):
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True
            )
            stdout, _ = proc.communicate(timeout=timeout)
            stdout = stdout or ""
            if proc.returncode != 0:
                return False, f"Command failed (exit {proc.returncode}): {cmd}\nOutput:\n{stdout}"
            return True, stdout
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, _ = proc.communicate()
            stdout = stdout or ""
            return False, f"Command timed out after {timeout}s: {cmd}\nOutput until timeout:\n{stdout}"
        except Exception as e:
            try:
                proc.kill()
            except Exception:
                pass
            return False, str(e)

    @staticmethod
    def execute_smoke_test(project_path, tool, language, framework, package_manager):
        # Skip smoke test for ANY Python + Behave template (Selenium or Playwright). Both ship a
        # complete, lenient login sample, so `behave` is meant to be run by the user against a real
        # app — running it at creation time launches a live browser against the user-supplied URL,
        # which is slow and fragile (display/headless, network, the target app's real markup). This
        # mirrors the Java/C#/TypeScript skips: scaffold now, let the user run the suite themselves.
        if (language and str(language).lower() == "python" and
                framework and "behave" in str(framework).lower()):
            logger.info(f"Skipping smoke test for {tool}/{language}/{framework} (Behave scaffold runs separately)")
            return True, f"Smoke test skipped for {tool}/Python/Behave. Ready for test development."

        # Skip smoke test for TypeScript/Playwright projects - they only have scaffolding
        if language in ["Typescript", "JavaScript", "TypeScript"] and tool == "Playwright":
            logger.info(f"Skipping smoke test for {language}/{tool} (scaffolding only)")
            return True, "Smoke test skipped for TypeScript scaffolding. Ready for manual test development."

        # Skip smoke test for any Java + Cucumber template. Both Playwright/Java and Selenium/Java
        # now ship a runnable sample (pageObjects/LoginPage.java + stepDefinitions/LoginSteps.java)
        # so `mvn test` passes out of the box — we skip here only because a smoke run at creation
        # time means a full Maven build plus a browser-binary download, too slow/network-dependent
        # for the interactive flow. The user can run `mvn test` themselves after install.
        if language == "Java" and "Cucumber" in (framework or ""):
            logger.info(f"Skipping smoke test for {language}/{tool}/{framework} (Cucumber scaffold runs separately)")
            return True, f"Smoke test skipped for {tool}/Java/Cucumber scaffolding. Ready for manual test development."

        # Skip smoke test for C# (Reqnroll). Like the Java case, the template ships a runnable
        # sample (PageObjects/LoginPage.cs + StepDefinitions/LoginSteps.cs) so `dotnet test` passes
        # on its own — we skip here only because a smoke run at creation time means a full
        # `dotnet restore` + build plus a Selenium Manager driver download, too slow/network-
        # dependent for the interactive flow. The user can run `dotnet test` themselves after install.
        if language == "C#":
            logger.info(f"Skipping smoke test for {language}/{tool}/{framework} (Reqnroll scaffold runs separately)")
            return True, f"Smoke test skipped for {tool}/C#/Reqnroll scaffolding. Ready for manual test development."

        if language == "Python":
            if "Behave" in framework or "Jbehave" in framework:
                cmd = (
                    "venv\\Scripts\\python -m behave -f json -o Results/behave_report.json -f html -o Results/report.html -f pretty"
                    if os.name == 'nt' else
                    "venv/bin/python3 -m behave -f json -o Results/behave_report.json -f html -o Results/report.html -f pretty"
                )
            else:
                cmd = "venv\\Scripts\\python -m pytest tests/ --html=Results/report.html" if os.name == 'nt' else "venv/bin/python3 -m pytest tests/ --html=Results/report.html"
        elif language == "Java":
            cmd = "mvn test"
        elif language == "C#":
            cmd = 'dotnet test --results-directory Results --logger "html;LogFileName=report.html"'
        else:
            return False, "Smoke test not configured for this language."

        success, output = BootstrapperEngine._run_command(cmd, project_path)
        output = output or ""
        if not success:
            if language == "Python" and ("Behave" in framework or "Jbehave" in framework):
                try:
                    with open(os.path.join(project_path, "Results", "behave_report.txt"), "w", encoding="utf-8") as f:
                        f.write(output)
                except Exception:
                    pass
                return False, "Behave Smoke Test had failures. Check Results/report.html for detailed output."
            return False, output.strip()

        return True, output.strip()

    @staticmethod
    def _inject_sample_test(target_dir, search_lang, tool, framework, url, username, password):
        """
        Inject a sample login test for templates that don't already ship one.

        Currently only the TypeScript/Playwright/Cucumber template needs this — it ships empty
        step/page folders and we generate the sample via AI here. Every other template (Java, C#,
        and both Python templates) ships a complete runnable login sample in its on-disk files, so
        this is a no-op for them. (url/username/password are accepted for parity and possible future
        use by other branches; the AI flow reads them from the scaffolded .env instead.)
        """
        try:
            if search_lang == "TypeScript" and tool == "Playwright" and "Cucumber" in framework:
                import glob
                import asyncio
                import sys

                # 1. Feature file
                feature_path = os.path.join(target_dir, "test", "features", "loginFeature.feature")
                if not os.path.exists(feature_path):
                    feature_files = glob.glob(os.path.join(target_dir, "**", "*.feature"), recursive=True)
                    if feature_files:
                        feature_path = feature_files[0]
                    else:
                        logger.info("No feature file found to generate scripts.")
                        return

                with open(feature_path, "r", encoding="utf-8") as f:
                    bdd_content = f.read()

                sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
                try:
                    from api.backend import route_code_generation
                except ImportError as e:
                    logger.error(f"Could not import backend for AI generation: {e}")
                    return

                support_content = (
                    "File Mode: New\n"
                    "DO NOT generate the .feature file\n"
                    "DO NOT generate boilerplate configuration files\n"
                    "Generate only stepdefinition file and pageobject file.\n"
                    "For launching the application URL, read 'APP_URL' from the .env file "
                    "and for credentials read 'USER' and 'PASSWORD' properties from the .env file "
                    "while creating the stepdefinition file.\n"
                    "Strickt Rules: DO NOT generate any comments or mock page code in the step definition file.\n"
                    "Use exact casing for 'Given', 'When', 'Then' keywords. DO NOT use all caps like 'GIVEN'.\n"
                    "DO NOT use 'And' or 'But' keywords; instead, substitute them with appropriate 'Given', 'When', or 'Then'.\n"
                    "Ensure proper imports are present, including importing 'Page' from '@playwright/test' or 'page' from '../hooks/hooks' as needed.\n"
                    "CRITICAL LOCATORS: Ensure to use the real and unique locators with out any hallucination for the elements in the page.\n"
                    "CRITICAL VERIFICATION: For 'Then I should be redirected to the homepage', DO NOT check the URL for 'dashboard' or hallucinate URLs. Instead, verify that Log in or Sign in button captured in previous steps is not displayed"
                )

                try:
                    logger.info(f"Dynamically generating sample script based on {feature_path}...")
                    files_dict = asyncio.run(route_code_generation(
                        language=search_lang,
                        framework=framework,
                        bdd_content=bdd_content,
                        support_content=support_content,
                        file_content="",
                        provider="",
                        tool=tool
                    ))

                    for rel_path, file_content in files_dict.items():
                        if rel_path.endswith(".feature"):
                            continue
                        
                        base_name = os.path.basename(rel_path)
                        name_part, ext_part = os.path.splitext(base_name)
                        base_name = name_part.replace(".", "_") + ext_part
                        dest_rel_path = rel_path
                        if "step" in rel_path.lower() or base_name.lower().endswith("steps.ts") or base_name.lower().endswith("steps.js"):
                            dest_rel_path = f"test/stepDefinitions/{base_name}"
                            import_line = 'import { ConfigReader } from "../utils/configReader";\n'
                            if import_line not in file_content:
                                file_content = import_line + file_content
                            
                            file_content = file_content.replace("../pageobjects/", "../pageObjects/")
                            file_content = file_content.replace("../page_objects/", "../pageObjects/")
                            file_content = file_content.replace("../pageobjects/", "../pageObjects/")
                            file_content = file_content.replace("../pages/", "../pageObjects/")
                            file_content = file_content.replace("../page/", "../pageObjects/")
                            file_content = file_content.replace("../PageObjects/", "../pageObjects/")
                        elif "page" in rel_path.lower() or base_name.lower().endswith("page.ts") or base_name.lower().endswith("page.js"):
                            dest_rel_path = f"test/pageObjects/{base_name}"

                        full_path = os.path.join(target_dir, dest_rel_path)
                        os.makedirs(os.path.dirname(full_path), exist_ok=True)
                        with open(full_path, "w", encoding="utf-8") as f:
                            f.write(file_content)

                    logger.info("Successfully generated sample scripts for feature file.")
                except Exception as e:
                    logger.error(f"Error during AI generation of sample scripts: {e}")


            else:
                # Java, C#, Selenium/Python and Playwright/Python all ship a complete, runnable
                # login sample inside their on-disk template (pages + steps + feature), so there is
                # nothing to inject here. Only the TypeScript template relies on AI generation above.
                logger.info(f"Sample test injection not needed for {tool}/{search_lang}/{framework} (template ships a sample).")
        except Exception as e:
            logger.error(f"Failed to inject sample test: {e}")

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
            safe_url, safe_user, safe_pwd = BootstrapperEngine._sec_str(url), BootstrapperEngine._sec_str(username), BootstrapperEngine._sec_str(password)
            if tool == "Playwright":
                base_page_content = f'''\
class BasePage:
    def __init__(self, page):
        self.page = page

    def navigate(self):
        self.page.goto("{safe_url}")
'''
                test_content = f'''\
import pytest
from pages.login_page import LoginPage

def test_login(page):
    login_page = LoginPage(page)
    login_page.navigate()
    login_page.login("{safe_user}", "{safe_pwd}")
    assert True # Replace with actual assertion
'''
            else: # Selenium
                base_page_content = f'''\
class BasePage:
    def __init__(self, driver):
        self.driver = driver

    def navigate(self):
        self.driver.get("{safe_url}")
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
    login_page.login("{safe_user}", "{safe_pwd}")
    assert True # Replace with actual assertion
'''

            with open(os.path.join(target_dir, "pages", "base_page.py"), "w") as f:
                f.write(base_page_content)

            if "Behave" in framework or "Jbehave" in framework:
                with open(os.path.join(target_dir, "behave.ini"), "w") as f:
                    f.write(
                        "[behave]\n"
                        "paths = features\n"
                        "default_tags = @smoke\n"
                        "stop = true\n"
                        "show_skipped = false\n"
                        "format = pretty\n"
                        "outfiles = Results/behave_report.txt\n"
                        "stdout_capture = false\n"
                        "stderr_capture = false\n"
                        "log_capture = false\n\n"
                        "[behave.formatters]\n"
                        "html = behave_html_formatter:HTMLFormatter\n"
                    )
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
            safe_url = BootstrapperEngine._sec_str(url)
            if tool == "Playwright":
                os.makedirs(os.path.join(target_dir, "Results"), exist_ok=True)
                config_content = f'''import {{ defineConfig }} from '@playwright/test';
export default defineConfig({{
  testDir: './tests',
  reporter: [['html', {{ outputFolder: 'Results' }}]],
  use: {{
    headless: false,
    baseURL: '{safe_url}',
  }},
}});
''' if is_ts else f'''const {{ defineConfig }} = require('@playwright/test');
module.exports = defineConfig({{
  testDir: './tests',
  reporter: [['html', {{ outputFolder: 'Results' }}]],
  use: {{
    headless: false,
    baseURL: '{safe_url}',
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
