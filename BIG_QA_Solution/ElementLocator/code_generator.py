import re
import time

class CodeGenerator:
    @staticmethod
    def generate_class_content(tool: str, language: str, page_name: str, locators: list) -> str:
        """
        locators: list of dicts, expected keys: name/nameHint, value, type, action, category
        """
        lines = []
        
        # Pre-process names to ensure uniqueness
        used_names = set()
        for loc in locators:
            raw_name = loc.get("name") or loc.get("nameHint") or "element"
            base_name = CodeGenerator.clean_name(raw_name, "element", snake_case=(language.lower() == "python"))
            
            # Ensure unique name
            final_name = base_name
            counter = 1
            while final_name in used_names:
                final_name = f"{base_name}_{counter}"
                counter += 1
                
            used_names.add(final_name)
            loc["_final_name"] = final_name

        if language.lower() == "java":
            if tool.lower() == "selenium":
                lines.append("import org.openqa.selenium.*;")
                lines.append("import org.openqa.selenium.support.*;\n")
            elif tool.lower() == "playwright":
                lines.append("import com.microsoft.playwright.*;")
                lines.append("import com.microsoft.playwright.options.AriaRole;\n")
            
            lines.append(f"public class {page_name} {{\n")

            if tool.lower() == "selenium":
                lines.append(f"    public {page_name}(WebDriver driver) {{")
                lines.append("        PageFactory.initElements(driver, this);")
                lines.append("    }\n")
                lines.append("    public void navigateTo(WebDriver driver, String url) {")
                lines.append("        driver.get(url);")
                lines.append("    }\n")
            elif tool.lower() == "playwright":
                lines.append("    private final Page page;\n")
                lines.append(f"    public {page_name}(Page page) {{")
                lines.append("        this.page = page;")
                if locators:
                    for loc in locators:
                        if loc.get("action") == "SwitchToWindow":
                            continue   # window switch has no DOM locator field
                        name = loc.get("_final_name")
                        val = CodeGenerator.escape_quotes(loc.get("value", ""))
                        l_type = loc.get("type", "XPath")
                        if l_type.startswith("getBy"):
                            native_call = CodeGenerator._get_playwright_native_call_java(l_type, val)
                            lines.append(f"        this.{name} = {native_call};")
                        else:
                            lines.append(f"        this.{name} = this.page.locator(\"{val}\");")
                lines.append("    }\n")
                lines.append("    public void navigateTo(String url) {")
                lines.append("        this.page.navigate(url);")
                lines.append("    }\n")

            if locators:
                for loc in locators:
                    if loc.get("action") == "SwitchToWindow":
                        continue   # window switch has no DOM locator field
                    name = loc.get("_final_name")
                    val = CodeGenerator.escape_quotes(loc.get("value", ""))
                    l_type = loc.get("type", "XPath")
                    category = loc.get("category", "Ok")

                    if tool.lower() == "selenium":
                        lines.append(f"    // Priority: {category}")
                        how_str = CodeGenerator._java_how(l_type)
                        lines.append(f"    @FindBy({how_str} = \"{val}\")")
                        lines.append(f"    public WebElement {name};\n")
                    elif tool.lower() == "playwright":
                        lines.append(f"    // Priority: {category}")
                        lines.append(f"    public final Locator {name};\n")

                for loc in locators:
                    name = loc.get("_final_name")
                    action = loc.get("action", "Click")
                    lines.append(CodeGenerator._java_action(tool, name, action))

            lines.append("}\n")

        elif language.lower() == "python":
            lines.append(f"class {page_name}:\n")
            if tool.lower() == "playwright":
                lines.append("    def __init__(self, page):")
                lines.append("        self.page = page")
                if locators:
                    for loc in locators:
                        if loc.get("action") == "SwitchToWindow":
                            continue
                        name = loc.get("_final_name")
                        val = CodeGenerator.escape_quotes(loc.get("value", ""))
                        l_type = loc.get("type", "XPath")
                        category = loc.get("category", "Ok")
                        lines.append(f"        # Priority: {category}")
                        if l_type.startswith("getBy"):
                            native_call = CodeGenerator._get_playwright_native_call_python(l_type, val)
                            lines.append(f"        self.{name} = {native_call}")
                        else:
                            lines.append(f"        self.{name} = page.locator(\"{val}\")")
                lines.append("")
                lines.append("    def navigate_to(self, url):")
                lines.append("        self.page.goto(url)\n")
            elif tool.lower() == "selenium":
                lines.append("    def navigate_to(self, driver, url):")
                lines.append("        driver.get(url)\n")
                if locators:
                    for loc in locators:
                        if loc.get("action") == "SwitchToWindow":
                            continue
                        name = loc.get("_final_name")
                        val = CodeGenerator.escape_quotes(loc.get("value", ""))
                        category = loc.get("category", "Ok")
                        lines.append(f"    # Priority: {category}")
                        lines.append(f"    {name} = \"{val}\"\n")

            if locators:
                for loc in locators:
                    name = loc.get("_final_name")
                    action = loc.get("action", "Click")
                    lines.append(CodeGenerator._python_action(tool, name, action))
            else:
                if tool.lower() not in ["playwright", "selenium"]:
                    lines.append("    pass\n")

        elif language.lower() == "c#":
            if tool.lower() == "selenium":
                lines.append("using OpenQA.Selenium;")
                lines.append("using SeleniumExtras.PageObjects;\n")
            elif tool.lower() == "playwright":
                lines.append("using Microsoft.Playwright;\n")

            lines.append(f"public class {page_name} {{\n")

            if tool.lower() == "selenium":
                lines.append(f"    public {page_name}(IWebDriver driver) {{")
                lines.append("        PageFactory.InitElements(driver, this);")
                lines.append("    }\n")
                lines.append("    public void NavigateTo(IWebDriver driver, string url) {")
                lines.append("        driver.Navigate().GoToUrl(url);")
                lines.append("    }\n")
            elif tool.lower() == "playwright":
                lines.append("    private readonly IPage _page;\n")
                lines.append(f"    public {page_name}(IPage page) {{")
                lines.append("        _page = page;")
                if locators:
                    for loc in locators:
                        if loc.get("action") == "SwitchToWindow":
                            continue   # window switch has no DOM locator field
                        name = loc.get("_final_name")
                        val = CodeGenerator.escape_quotes(loc.get("value", ""))
                        l_type = loc.get("type", "XPath")
                        if l_type.startswith("getBy"):
                            native_call = CodeGenerator._get_playwright_native_call_csharp(l_type, val)
                            lines.append(f"        this._{name} = {native_call};")
                        else:
                            lines.append(f"        this._{name} = page.Locator(\"{val}\");")
                lines.append("    }\n")
                lines.append("    public async System.Threading.Tasks.Task NavigateToAsync(string url) {")
                lines.append("        await _page.GotoAsync(url);")
                lines.append("    }\n")

            if locators:
                for loc in locators:
                    if loc.get("action") == "SwitchToWindow":
                        continue   # window switch has no DOM locator field
                    name = loc.get("_final_name")
                    val = CodeGenerator.escape_quotes(loc.get("value", ""))
                    l_type = loc.get("type", "XPath")
                    category = loc.get("category", "Ok")

                    if tool.lower() == "selenium":
                        lines.append(f"    // Priority: {category}")
                        how_str = CodeGenerator._csharp_how(l_type)
                        lines.append(f"    [FindsBy({how_str}, Using = \"{val}\")]")
                        lines.append(f"    public IWebElement {name} {{ get; set; }}\n")
                    elif tool.lower() == "playwright":
                        lines.append(f"    // Priority: {category}")
                        lines.append(f"    public readonly ILocator _{name};\n")
                    else:
                        lines.append(f"    // Priority: {category}")
                        lines.append(f"    public string {name}Locator = \"{val}\";\n")

                for loc in locators:
                    name = loc.get("_final_name")
                    action = loc.get("action", "Click")
                    lines.append(CodeGenerator._csharp_action(tool, name, action))

            lines.append("}\n")

        elif language.lower() in ["javascript", "typescript"]:
            is_ts = language.lower() == "typescript"
            is_pw = tool.lower() == "playwright"

            if is_pw:
                if is_ts:
                    lines.append("import { Page, Locator } from '@playwright/test';\n")
                else:
                    lines.append("const { expect } = require('@playwright/test');\n")

            if is_ts:
                lines.append(f"export class {page_name} {{")
            else:
                lines.append(f"class {page_name} {{")

            if is_ts:
                if is_pw:
                    lines.append("    readonly page: Page;")
                if locators:
                    for loc in locators:
                        if loc.get("action") == "SwitchToWindow":
                            continue   # window switch has no DOM locator field
                        name = loc.get("_final_name")
                        if is_pw:
                            lines.append(f"    readonly {name}: Locator;")
                        else:
                            lines.append(f"    readonly {name}: string;")
                lines.append("")

            if is_pw:
                if is_ts:
                    lines.append("    constructor(page: Page) {")
                else:
                    lines.append("    constructor(page) {")
                lines.append("        this.page = page;")
            else:
                lines.append("    constructor() {")

            if locators:
                for loc in locators:
                    if loc.get("action") == "SwitchToWindow":
                        continue   # window switch has no DOM locator field
                    name = loc.get("_final_name")
                    val = CodeGenerator.escape_quotes(loc.get("value", ""))
                    l_type = loc.get("type", "XPath")  # MUST read per-locator
                    category = loc.get("category", "Ok")

                    lines.append(f"        // Priority: {category}")
                    if is_pw:
                        if l_type.startswith("getBy"):
                            native_call = CodeGenerator._get_playwright_native_call(l_type, val)
                            lines.append(f"        this.{name} = {native_call};")
                        else:
                            lines.append(f"        this.{name} = page.locator(\"{val}\");")
                    else:
                        lines.append(f"        this.{name} = \"{val}\";")

            lines.append("    }\n")
            if is_pw:
                if is_ts:
                    lines.append("    async navigateTo(url: string) {")
                else:
                    lines.append("    async navigateTo(url) {")
                lines.append("        await this.page.goto(url);")
                lines.append("    }\n")
            else:
                if is_ts:
                    lines.append("    async navigateTo(driver: any, url: string) {")
                else:
                    lines.append("    async navigateTo(driver, url) {")
                lines.append("        await driver.get(url);")
                lines.append("    }\n")
            lines.append("")

            if locators:
                for loc in locators:
                    name = loc.get("_final_name")
                    action = loc.get("action", "Click")
                    lines.append(CodeGenerator._js_action(tool, name, action))
            
            lines.append("}")
            lines.append("")
            if not is_ts:
                if is_pw:
                    lines.append(f"module.exports = {{ {page_name} }};")
                else:
                    lines.append(f"module.exports = {page_name};")

        return "\n".join(lines)

    @staticmethod
    def _parse_get_by_role(val: str) -> tuple[str, str | None]:
        val_stripped = val.strip()
        
        # Check for JavaScript options format: 'button', {name: 'Login'}
        if "," in val_stripped:
            parts = val_stripped.split(",", 1)
            role = parts[0].strip().strip("'").strip('"')
            rest = parts[1].strip()
            import re
            name_match = re.search(r"name\s*[:=]\s*['\"](.*?)['\"]", rest)
            if name_match:
                return role, name_match.group(1)
            return role, None
            
        # Heuristic for || or : separator
        separator = "||" if "||" in val_stripped else ":" if ":" in val_stripped else None
        if separator:
            parts = val_stripped.split(separator, 1)
            role = parts[0].strip().strip("'").strip('"')
            name = parts[1].strip().strip("'").strip('"')
            return role, name
            
        # Simple unquoted fallback
        role = val_stripped.strip("'").strip('"')
        return role, None

    @staticmethod
    def _get_playwright_native_call(l_type: str, val: str) -> str:
        """Converts internal type to Playwright native call string."""
        import json
        if l_type == "getByRole":
            role, name = CodeGenerator._parse_get_by_role(val)
            if name:
                return f"page.getByRole('{role}', {{ name: '{name}' }})"
            return f"page.getByRole('{role}')"
            
        safe_val = json.dumps(val)
        if l_type == "getByText":
            return f"page.getByText({safe_val})"
        elif l_type == "getByLabel":
            return f"page.getByLabel({safe_val})"
        elif l_type == "getByPlaceholder":
            return f"page.getByPlaceholder({safe_val})"
        elif l_type == "getByAltText":
            return f"page.getByAltText({safe_val})"
        elif l_type == "getByTitle":
            return f"page.getByTitle({safe_val})"
        elif l_type == "getByTestId":
            return f"page.getByTestId({safe_val})"
        return f"page.locator({safe_val})"

    @staticmethod
    def _get_playwright_native_call_python(l_type: str, val: str) -> str:
        import json
        if l_type == "getByRole":
            role, name = CodeGenerator._parse_get_by_role(val)
            if name:
                escaped_name = name.replace('"', '\\"')
                return f"page.get_by_role(\"{role}\", name=\"{escaped_name}\")"
            return f"page.get_by_role(\"{role}\")"
            
        safe_val = json.dumps(val)
        if l_type == "getByText":
            return f"page.get_by_text({safe_val})"
        elif l_type == "getByLabel":
            return f"page.get_by_label({safe_val})"
        elif l_type == "getByPlaceholder":
            return f"page.get_by_placeholder({safe_val})"
        elif l_type == "getByAltText":
            return f"page.get_by_alt_text({safe_val})"
        elif l_type == "getByTitle":
            return f"page.get_by_title({safe_val})"
        elif l_type == "getByTestId":
            return f"page.get_by_test_id({safe_val})"
        return f"page.locator({safe_val})"

    @staticmethod
    def _get_playwright_native_call_java(l_type: str, val: str) -> str:
        import json
        if l_type == "getByRole":
            role, name = CodeGenerator._parse_get_by_role(val)
            role_enum = role.upper()
            if name:
                escaped_name = name.replace('"', '\\"')
                return f"page.getByRole(AriaRole.{role_enum}, new Page.GetByRoleOptions().setName(\"{escaped_name}\"))"
            return f"page.getByRole(AriaRole.{role_enum})"
            
        safe_val = json.dumps(val)
        if l_type == "getByText":
            return f"page.getByText({safe_val})"
        elif l_type == "getByLabel":
            return f"page.getByLabel({safe_val})"
        elif l_type == "getByPlaceholder":
            return f"page.getByPlaceholder({safe_val})"
        elif l_type == "getByAltText":
            return f"page.getByAltText({safe_val})"
        elif l_type == "getByTitle":
            return f"page.getByTitle({safe_val})"
        elif l_type == "getByTestId":
            return f"page.getByTestId({safe_val})"
        return f"page.locator({safe_val})"

    @staticmethod
    def _get_playwright_native_call_csharp(l_type: str, val: str) -> str:
        import json
        if l_type == "getByRole":
            role, name = CodeGenerator._parse_get_by_role(val)
            role_pascal = role[0].upper() + role[1:] if role else ""
            if name:
                escaped_name = name.replace('"', '\\"')
                return f"page.GetByRole(AriaRole.{role_pascal}, new PageGetByRoleOptions {{ Name = \"{escaped_name}\" }})"
            return f"page.GetByRole(AriaRole.{role_pascal})"
            
        safe_val = json.dumps(val)
        if l_type == "getByText":
            return f"page.GetByText({safe_val})"
        elif l_type == "getByLabel":
            return f"page.GetByLabel({safe_val})"
        elif l_type == "getByPlaceholder":
            return f"page.GetByPlaceholder({safe_val})"
        elif l_type == "getByAltText":
            return f"page.GetByAltText({safe_val})"
        elif l_type == "getByTitle":
            return f"page.GetByTitle({safe_val})"
        elif l_type == "getByTestId":
            return f"page.GetByTestId({safe_val})"
        return f"page.Locator({safe_val})"


    @staticmethod
    def clean_name(name: str, fallback: str, snake_case=False) -> str:
        s = re.sub(r'[^a-zA-Z0-9_]', '', name) if snake_case else re.sub(r'[^a-zA-Z0-9]', '', name)
        if not s:
            s = f"{fallback}{int(time.time()*1000)}"
        return s

    @staticmethod
    def escape_quotes(val: str) -> str:
        if not val:
            return ""
        return val.replace('"', '\\"')

    @staticmethod
    def _java_how(l_type: str) -> str:
        mapping = {
            "id": "id", "css": "css", "name": "name",
            "link text": "linkText", "partial link": "partialLinkText",
            "tag name": "tagName", "xpath": "xpath"
        }
        return mapping.get(l_type.lower(), "xpath")

    @staticmethod
    def _csharp_how(l_type: str) -> str:
        mapping = {
            "id": "How.Id", "css": "How.CssSelector", "name": "How.Name",
            "link text": "How.LinkText", "partial link": "How.PartialLinkText",
            "tag name": "How.TagName", "xpath": "How.XPath"
        }
        return f"How = {mapping.get(l_type.lower(), 'How.XPath')}"

    @staticmethod
    def _java_action(tool: str, el_name: str, action: str) -> str:
        m_name = action.lower() + el_name[0].upper() + el_name[1:]
        res = []
        if tool.lower() == "selenium":
            if action == "Click":
                res.append(f"    public void {m_name}() {{\n        {el_name}.click();\n    }}\n")
            elif action == "Type":
                res.append(f"    public void {m_name}(String text) {{\n        {el_name}.sendKeys(text);\n    }}\n")
            elif action == "Clear":
                res.append(f"    public void {m_name}() {{\n        {el_name}.clear();\n    }}\n")
            elif action == "GetText":
                res.append(f"    public String {m_name}() {{\n        return {el_name}.getText();\n    }}\n")
            elif action == "IsDisplayed":
                res.append(f"    public boolean {m_name}() {{\n        return {el_name}.isDisplayed();\n    }}\n")
            elif action == "SelectByVisibleText":
                res.append(f"    public void {m_name}(String text) {{\n         Select dropDown = new Select({el_name});\n         dropDown.SelectByVisibleText(text);\n    }}\n")
            elif action == "Hover":
                res.append(f"    public void {m_name}(WebDriver driver) {{\n        new org.openqa.selenium.interactions.Actions(driver).moveToElement({el_name}).perform();\n    }}\n")
            elif action == "DoubleClick":
                res.append(f"    public void {m_name}(WebDriver driver) {{\n        new org.openqa.selenium.interactions.Actions(driver).doubleClick({el_name}).perform();\n    }}\n")
            elif action in ("RightClick", "ContextClick"):
                res.append(f"    public void {m_name}(WebDriver driver) {{\n        new org.openqa.selenium.interactions.Actions(driver).contextClick({el_name}).perform();\n    }}\n")
            elif action == "WaitForVisible":
                res.append(f"    public void {m_name}(WebDriver driver, int timeoutSeconds) {{\n        new org.openqa.selenium.support.ui.WebDriverWait(driver, java.time.Duration.ofSeconds(timeoutSeconds)).until(org.openqa.selenium.support.ui.ExpectedConditions.visibilityOf({el_name}));\n    }}\n")
            elif action == "SwitchToFrame":
                res.append(f"    public void {m_name}(WebDriver driver) {{\n        driver.switchTo().frame({el_name});\n    }}\n")
            elif action == "SwitchToWindow":
                res.append(f"    public void {m_name}(WebDriver driver) {{\n        String currentHandle = driver.getWindowHandle();\n        for (String handle : driver.getWindowHandles()) {{\n            if (!handle.equals(currentHandle)) {{\n                driver.switchTo().window(handle);\n                break;\n            }}\n        }}\n    }}\n")
        elif tool.lower() == "playwright":
            if action == "Click":
                res.append(f"    public void {m_name}() {{\n        {el_name}.click();\n    }}\n")
            elif action == "Type":
                res.append(f"    public void {m_name}(String text) {{\n        {el_name}.fill(text);\n    }}\n")
            elif action == "Clear":
                res.append(f"    public void {m_name}() {{\n        {el_name}.fill(\"\");\n    }}\n")
            elif action == "GetText":
                res.append(f"    public String {m_name}() {{\n        return {el_name}.textContent();\n    }}\n")
            elif action == "IsDisplayed":
                res.append(f"    public boolean {m_name}() {{\n        this.{el_name}.waitfor(new Locator.WaitForOptions()\n            .setState(WaitForSelectorState.VISIBLE)\n            .setTimeout(10000));\n        return {el_name}.isVisible();\n    }}\n")
            elif action == "SelectByVisibleText":
                res.append(f"    public void {m_name}(String text) {{\n        {el_name}.selectOption(new SelectOption().withLabel(text));\n    }}\n")
            elif action == "Hover":
                res.append(f"    public void {m_name}() {{\n        {el_name}.hover();\n    }}\n")
            elif action == "DoubleClick":
                res.append(f"    public void {m_name}() {{\n        {el_name}.doubleClick();\n    }}\n")
            elif action in ("RightClick", "ContextClick"):
                res.append(f"    public void {m_name}() {{\n        {el_name}.click(new Locator.ClickOptions().setButton(com.microsoft.playwright.options.MouseButton.RIGHT));\n    }}\n")
            elif action == "WaitForVisible":
                res.append(f"    public void {m_name}() {{\n        {el_name}.waitFor(new Locator.WaitForOptions().setState(com.microsoft.playwright.options.WaitForSelectorState.VISIBLE));\n    }}\n")
            elif action == "SwitchToFrame":
                # Playwright: the iframe Locator exposes the frame's contents via contentFrame().
                res.append(f"    public FrameLocator {m_name}() {{\n        return {el_name}.contentFrame();\n    }}\n")
            elif action == "SwitchToWindow":
                # Playwright: a new window/tab is a Page in the same BrowserContext.
                res.append(f"    public Page {m_name}() {{\n        java.util.List<Page> pages = page.context().pages();\n        return pages.get(pages.size() - 1);\n    }}\n")
        return "".join(res)

    @staticmethod
    def _python_action(tool: str, el_name: str, action: str) -> str:
        m_name = f"{action.lower()}_{el_name.lower()}"
        res = []
        if tool.lower() == "playwright":
            if action == "Click":
                res.append(f"    def {m_name}(self):\n        self.{el_name}.click()\n\n")
            elif action == "Type":
                res.append(f"    def {m_name}(self, text):\n        self.{el_name}.fill(text)\n\n")
            elif action == "Clear":
                res.append(f"    def {m_name}(self):\n        self.{el_name}.fill(\"\")\n\n")
            elif action == "GetText":
                res.append(f"    def {m_name}(self):\n        return self.{el_name}.text_content()\n\n")
            elif action == "IsDisplayed":
                res.append(f"    def {m_name}(self):\n        self.{el_name}.wait_for(state='visible', timeout=10000)\n        return self.{el_name}.is_visible()\n\n")
            elif action == "SelectByVisibleText":
                res.append(f"    def {m_name}(self, text):\n        self.{el_name}.select_option(label=text)\n\n")
            elif action == "Hover":
                res.append(f"    def {m_name}(self):\n        self.{el_name}.hover()\n\n")
            elif action == "DoubleClick":
                res.append(f"    def {m_name}(self):\n        self.{el_name}.double_click()\n\n")
            elif action in ("RightClick", "ContextClick"):
                res.append(f"    def {m_name}(self):\n        self.{el_name}.click(button='right')\n\n")
            elif action == "WaitForVisible":
                res.append(f"    def {m_name}(self):\n        self.{el_name}.wait_for(state='visible')\n\n")
            elif action == "SwitchToFrame":
                res.append(f"    def {m_name}(self):\n        return self.{el_name}.content_frame\n\n")
            elif action == "SwitchToWindow":
                res.append(f"    def {m_name}(self):\n        pages = self.page.context.pages\n        return pages[-1]\n\n")
        else:
            if action == "Click":
                res.append(f"    def {m_name}(self, driver):\n        driver.find_element('xpath', self.{el_name}).click()\n\n")
            elif action == "Type":
                res.append(f"    def {m_name}(self, driver, text):\n        driver.find_element('xpath', self.{el_name}).send_keys(text)\n\n")
            elif action == "Clear":
                res.append(f"    def {m_name}(self, driver):\n        driver.find_element('xpath', self.{el_name}).clear()\n\n")
            elif action == "GetText":
                res.append(f"    def {m_name}(self, driver):\n        return driver.find_element('xpath', self.{el_name}).text\n\n")
            elif action == "IsDisplayed":
                res.append(f"    def {m_name}(self, driver):\n        return driver.find_element('xpath', self.{el_name}).is_displayed()\n\n")
            elif action == "SelectByVisibleText":
                res.append(f"    def {m_name}(self, driver, text):\n        from selenium.webdriver.support.ui import Select\n        Select(driver.find_element('xpath', self.{el_name})).select_by_visible_text(text)\n\n")
            elif action == "Hover":
                res.append(f"    def {m_name}(self, driver):\n        from selenium.webdriver.common.action_chains import ActionChains\n        el = driver.find_element('xpath', self.{el_name})\n        ActionChains(driver).move_to_element(el).perform()\n\n")
            elif action == "DoubleClick":
                res.append(f"    def {m_name}(self, driver):\n        from selenium.webdriver.common.action_chains import ActionChains\n        el = driver.find_element('xpath', self.{el_name})\n        ActionChains(driver).double_click(el).perform()\n\n")
            elif action in ("RightClick", "ContextClick"):
                res.append(f"    def {m_name}(self, driver):\n        from selenium.webdriver.common.action_chains import ActionChains\n        el = driver.find_element('xpath', self.{el_name})\n        ActionChains(driver).context_click(el).perform()\n\n")
            elif action == "WaitForVisible":
                res.append(f"    def {m_name}(self, driver, timeout=10):\n        from selenium.webdriver.support.ui import WebDriverWait\n        from selenium.webdriver.support import expected_conditions as EC\n        WebDriverWait(driver, timeout).until(EC.visibility_of_element_located(('xpath', self.{el_name})))\n\n")
            elif action == "SwitchToFrame":
                res.append(f"    def {m_name}(self, driver):\n        driver.switch_to.frame(driver.find_element('xpath', self.{el_name}))\n\n")
            elif action == "SwitchToWindow":
                res.append(f"    def {m_name}(self, driver):\n        current_handle = driver.current_window_handle\n        for handle in driver.window_handles:\n            if handle != current_handle:\n                driver.switch_to.window(handle)\n                break\n\n")
        return "".join(res)

    @staticmethod
    def _csharp_action(tool: str, el_name: str, action: str) -> str:
        m_name = action + el_name[0].upper() + el_name[1:]
        res = []
        if tool.lower() == "selenium":
            if action == "Click":
                res.append(f"    public void {m_name}() {{\n        {el_name}.Click();\n    }}\n\n")
            elif action == "Type":
                res.append(f"    public void {m_name}(string text) {{\n        {el_name}.SendKeys(text);\n    }}\n\n")
            elif action == "Clear":
                res.append(f"    public void {m_name}() {{\n        {el_name}.Clear();\n    }}\n\n")
            elif action == "GetText":
                res.append(f"    public string {m_name}() {{\n        return {el_name}.Text;\n    }}\n\n")
            elif action == "IsDisplayed":
                res.append(f"    public bool {m_name}() {{\n        return {el_name}.Displayed;\n    }}\n\n")
            elif action == "SelectByVisibleText":
                res.append(f"    public void {m_name}(string text) {{\n        var select = new SelectElement({el_name});\n        select.SelectByText(text);\n    }}\n\n")
            elif action == "Hover":
                res.append(f"    public void {m_name}(IWebDriver driver) {{\n        new OpenQA.Selenium.Interactions.Actions(driver).MoveToElement({el_name}).Perform();\n    }}\n\n")
            elif action == "DoubleClick":
                res.append(f"    public void {m_name}(IWebDriver driver) {{\n        new OpenQA.Selenium.Interactions.Actions(driver).DoubleClick({el_name}).Perform();\n    }}\n\n")
            elif action in ("RightClick", "ContextClick"):
                res.append(f"    public void {m_name}(IWebDriver driver) {{\n        new OpenQA.Selenium.Interactions.Actions(driver).ContextClick({el_name}).Perform();\n    }}\n\n")
            elif action == "WaitForVisible":
                res.append(f"    public void {m_name}(IWebDriver driver, int timeoutSeconds) {{\n        new OpenQA.Selenium.Support.UI.WebDriverWait(driver, System.TimeSpan.FromSeconds(timeoutSeconds)).Until(d => {el_name}.Displayed);\n    }}\n\n")
            elif action == "SwitchToFrame":
                res.append(f"    public void {m_name}(IWebDriver driver) {{\n        driver.SwitchTo().Frame({el_name});\n    }}\n\n")
            elif action == "SwitchToWindow":
                res.append(f"    public void {m_name}(IWebDriver driver) {{\n        var currentHandle = driver.CurrentWindowHandle;\n        foreach (var handle in driver.WindowHandles) {{\n            if (handle != currentHandle) {{\n                driver.SwitchTo().Window(handle);\n                break;\n            }}\n        }}\n    }}\n\n")
        elif tool.lower() == "playwright":
            m_name += "Async"
            el_ref = f"_{el_name}"
            if action == "Click":
                res.append(f"    public async Task {m_name}() {{\n        await {el_ref}.ClickAsync();\n    }}\n\n")
            elif action == "Type":
                res.append(f"    public async Task {m_name}(string text) {{\n        await {el_ref}.FillAsync(text);\n    }}\n\n")
            elif action == "Clear":
                res.append(f"    public async Task {m_name}() {{\n        await {el_ref}.FillAsync(\"\");\n    }}\n\n")
            elif action == "GetText":
                res.append(f"    public async Task<string> {m_name}() {{\n        return await {el_ref}.TextContentAsync();\n    }}\n\n")
            elif action == "IsDisplayed":
                res.append(f"    public async Task<bool> {m_name}() {{\n        return await {el_ref}.IsVisibleAsync();\n    }}\n\n")
            elif action == "SelectByVisibleText":
                res.append(f"    public async Task {m_name}(string text) {{\n        await {el_ref}.SelectOptionAsync(new[] {{ new SelectOptionValue {{ Label = text }} }});\n    }}\n\n")
            elif action == "Hover":
                res.append(f"    public async Task {m_name}() {{\n        await {el_ref}.HoverAsync();\n    }}\n\n")
            elif action == "DoubleClick":
                res.append(f"    public async Task {m_name}() {{\n        await {el_ref}.DoubleClickAsync();\n    }}\n\n")
            elif action in ("RightClick", "ContextClick"):
                res.append(f"    public async Task {m_name}() {{\n        await {el_ref}.ClickAsync(new LocatorClickOptions {{ Button = MouseButton.Right }});\n    }}\n\n")
            elif action == "WaitForVisible":
                res.append(f"    public async Task {m_name}() {{\n        await {el_ref}.WaitForAsync(new LocatorWaitForOptions {{ State = WaitForSelectorState.Visible }});\n    }}\n\n")
            elif action == "SwitchToFrame":
                res.append(f"    public IFrameLocator {m_name}() {{\n        return {el_ref}.ContentFrame;\n    }}\n\n")
            elif action == "SwitchToWindow":
                res.append(f"    public IPage {m_name}() {{\n        var pages = _page.Context.Pages;\n        return pages[pages.Count - 1];\n    }}\n\n")
        return "".join(res)

    @staticmethod
    def _js_action(tool: str, el_name: str, action: str) -> str:
        m_name = action.lower() + el_name[0].upper() + el_name[1:]
        res = []
        if tool.lower() == "playwright":
            if action == "Click":
                res.append(f"    async {m_name}() {{\n        await this.{el_name}.click();\n    }}\n\n")
            elif action == "Type":
                res.append(f"    async {m_name}(text) {{\n        await this.{el_name}.fill(text);\n    }}\n\n")
            elif action == "Clear":
                res.append(f"    async {m_name}() {{\n        await this.{el_name}.fill('');\n    }}\n\n")
            elif action == "GetText":
                res.append(f"    async {m_name}() {{\n        return await this.{el_name}.textContent();\n    }}\n\n")
            elif action == "IsDisplayed":
                res.append(f"    async {m_name}() {{\n        await this.{el_name}.waitFor({{ state: 'visible', timeout: 10000}});\n        return await this.{el_name}.isVisible();\n    }}\n\n")
            elif action == "SelectByVisibleText":
                res.append(f"    async {m_name}(text) {{\n        await this.{el_name}.selectOption({{ label: text }});\n    }}\n\n")
            elif action == "Hover":
                res.append(f"    async {m_name}() {{\n        await this.{el_name}.hover();\n    }}\n\n")
            elif action == "DoubleClick":
                res.append(f"    async {m_name}() {{\n        await this.{el_name}.doubleClick();\n    }}\n\n")
            elif action in ("RightClick", "ContextClick"):
                res.append(f"    async {m_name}() {{\n        await this.{el_name}.click({{ button: 'right' }});\n    }}\n\n")
            elif action == "WaitForVisible":
                res.append(f"    async {m_name}() {{\n        await this.{el_name}.waitFor({{ state: 'visible' }});\n    }}\n\n")
            elif action == "SwitchToFrame":
                res.append(f"    {m_name}() {{\n        return this.{el_name}.contentFrame();\n    }}\n\n")
            elif action == "SwitchToWindow":
                res.append(f"    {m_name}() {{\n        const pages = this.page.context().pages();\n        return pages[pages.length - 1];\n    }}\n\n")
        else:
            if action == "Click":
                res.append(f"    async {m_name}(driver) {{\n        await driver.findElement(By.xpath(this.{el_name})).click();\n    }}\n\n")
            elif action == "Type":
                res.append(f"    async {m_name}(driver, text) {{\n        await driver.findElement(By.xpath(this.{el_name})).sendKeys(text);\n    }}\n\n")
            elif action == "Clear":
                res.append(f"    async {m_name}(driver) {{\n        await driver.findElement(By.xpath(this.{el_name})).clear();\n    }}\n\n")
            elif action == "GetText":
                res.append(f"    async {m_name}(driver) {{\n        return await driver.findElement(By.xpath(this.{el_name})).getText();\n    }}\n\n")
            elif action == "IsDisplayed":
                res.append(f"    async {m_name}(driver) {{\n        return await driver.findElement(By.xpath(this.{el_name})).isDisplayed();\n    }}\n\n")
            elif action == "SelectByVisibleText":
                res.append(f"    async {m_name}(driver, text) {{\n        const select = await driver.findElement(By.xpath(this.{el_name}));\n        await select.sendKeys(text);\n    }}\n\n")
            elif action == "Hover":
                res.append(f"    async {m_name}(driver) {{\n        const el = await driver.findElement(By.xpath(this.{el_name}));\n        await driver.actions().move({{origin: el}}).perform();\n    }}\n\n")
            elif action == "DoubleClick":
                res.append(f"    async {m_name}(driver) {{\n        const el = await driver.findElement(By.xpath(this.{el_name}));\n        await driver.actions().doubleClick(el).perform();\n    }}\n\n")
            elif action in ("RightClick", "ContextClick"):
                res.append(f"    async {m_name}(driver) {{\n        const el = await driver.findElement(By.xpath(this.{el_name}));\n        await driver.actions().click(el, 2).perform();\n    }}\n\n")
            elif action == "WaitForVisible":
                res.append(f"    async {m_name}(driver, timeoutMs = 10000) {{\n        const el = await driver.findElement(By.xpath(this.{el_name}));\n        await driver.wait(until.elementIsVisible(el), timeoutMs);\n    }}\n\n")
            elif action == "SwitchToFrame":
                res.append(f"    async {m_name}(driver) {{\n        await driver.switchTo().frame(await driver.findElement(By.xpath(this.{el_name})));\n    }}\n\n")
            elif action == "SwitchToWindow":
                res.append(f"    async {m_name}(driver) {{\n        const currentHandle = await driver.getWindowHandle();\n        const handles = await driver.getAllWindowHandles();\n        for (const handle of handles) {{\n            if (handle !== currentHandle) {{\n                await driver.switchTo().window(handle);\n                break;\n            }}\n        }}\n    }}\n\n")
        return "".join(res)
