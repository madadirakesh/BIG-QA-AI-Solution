import re
import time

class CodeGenerator:
    @staticmethod
    def generate_class_content(tool: str, language: str, page_name: str, locators: list) -> str:
        """
        locators: list of dicts, expected keys: name, value, type, action, category
        """
        lines = []

        if language.lower() == "java":
            if tool.lower() == "selenium":
                lines.append("import org.openqa.selenium.*;")
                lines.append("import org.openqa.selenium.support.*;\n")
            elif tool.lower() == "playwright":
                lines.append("import com.microsoft.playwright.*;\n")
            
            lines.append(f"public class {page_name} {{\n")

            if tool.lower() == "selenium":
                lines.append(f"    public {page_name}(WebDriver driver) {{")
                lines.append("        PageFactory.initElements(driver, this);")
                lines.append("    }\n")
            elif tool.lower() == "playwright":
                lines.append("    private final Page page;\n")
                lines.append(f"    public {page_name}(Page page) {{")
                lines.append("        this.page = page;")
                if locators:
                    for loc in locators:
                        name = CodeGenerator.clean_name(loc.get("name", ""), "element")
                        val = CodeGenerator.escape_quotes(loc.get("value", ""))
                        lines.append(f"        this.{name} = this.page.locator(\"{val}\");")
                lines.append("    }\n")

            if locators:
                for loc in locators:
                    name = CodeGenerator.clean_name(loc.get("name", ""), "element")
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
                    name = CodeGenerator.clean_name(loc.get("name", ""), "element")
                    action = loc.get("action", "Click")
                    lines.append(CodeGenerator._java_action(tool, name, action))

            lines.append("}\n")

        elif language.lower() == "python":
            lines.append(f"class {page_name}:\n")
            if not locators:
                lines.append("    pass\n")
            else:
                for loc in locators:
                    name = CodeGenerator.clean_name(loc.get("name", ""), "element", snake_case=True)
                    val = CodeGenerator.escape_quotes(loc.get("value", ""))
                    category = loc.get("category", "Ok")

                    lines.append(f"    # Priority: {category}")
                    lines.append(f"    {name} = \"{val}\"\n")

                for loc in locators:
                    name = CodeGenerator.clean_name(loc.get("name", ""), "element", snake_case=True)
                    action = loc.get("action", "Click")
                    lines.append(CodeGenerator._python_action(tool, name, action))

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
            elif tool.lower() == "playwright":
                lines.append("    private readonly IPage _page;\n")
                lines.append(f"    public {page_name}(IPage page) {{")
                lines.append("        _page = page;")
                if locators:
                    for loc in locators:
                        name = CodeGenerator.clean_name(loc.get("name", ""), "element")
                        val = CodeGenerator.escape_quotes(loc.get("value", ""))
                        lines.append(f"        this._{name} = page.Locator(\"{val}\");")
                lines.append("    }\n")

            if locators:
                for loc in locators:
                    name = CodeGenerator.clean_name(loc.get("name", ""), "element")
                    val = CodeGenerator.escape_quotes(loc.get("value", ""))
                    l_type = loc.get("type", "XPath")
                    category = loc.get("category", "Ok")

                    if tool.lower() == "selenium":
                        lines.append(f"    // Priority: {category}")
                        how_str = CodeGenerator._csharp_how(l_type)
                        lines.append(f"    [FindsBy({how_str} = \"{val}\")]")
                        lines.append(f"    public IWebElement {name} {{ get; set; }}\n")
                    elif tool.lower() == "playwright":
                        lines.append(f"    // Priority: {category}")
                        lines.append(f"    public readonly ILocator _{name};\n")
                    else:
                        lines.append(f"    // Priority: {category}")
                        lines.append(f"    public string {name}Locator = \"{val}\";\n")

                for loc in locators:
                    name = CodeGenerator.clean_name(loc.get("name", ""), "element")
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
                        name = CodeGenerator.clean_name(loc.get("name", ""), "element")
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
                    name = CodeGenerator.clean_name(loc.get("name", ""), "element")
                    val = CodeGenerator.escape_quotes(loc.get("value", ""))
                    category = loc.get("category", "Ok")

                    lines.append(f"        // Priority: {category}")
                    if is_pw:
                        lines.append(f"        this.{name} = page.locator(\"{val}\");\n")
                    else:
                        lines.append(f"        this.{name} = \"{val}\";\n")

            lines.append("    }\n")

            if locators:
                for loc in locators:
                    name = CodeGenerator.clean_name(loc.get("name", ""), "element")
                    action = loc.get("action", "Click")
                    lines.append(CodeGenerator._js_action(tool, name, action))
            
            lines.append("}\n")
            if not is_ts:
                if is_pw:
                    lines.append(f"module.exports = {page_name};\n")
                else:
                    lines.append(f"export default {page_name}();\n")

        return "\n".join(lines)


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
                res.append(f"    public boolean {m_name}() {{\n        return {el_name}.isVisible();\n    }}\n")
            elif action == "SelectByVisibleText":
                res.append(f"    public void {m_name}(String text) {{\n        {el_name}.selectOption(new SelectOption().withLabel(text));\n    }}\n")
        return "".join(res)

    @staticmethod
    def _python_action(tool: str, el_name: str, action: str) -> str:
        m_name = f"{action.lower()}_{el_name.lower()}"
        res = []
        if tool.lower() == "playwright":
            if action == "Click":
                res.append(f"    def {m_name}(self):\n        self.page.locator(self.{el_name}).click()\n\n")
            elif action == "Type":
                res.append(f"    def {m_name}(self, text):\n        self.page.locator(self.{el_name}).fill(text)\n\n")
            elif action == "Clear":
                res.append(f"    def {m_name}(self):\n        self.page.locator(self.{el_name}).fill(\"\")\n\n")
            elif action == "GetText":
                res.append(f"    def {m_name}(self):\n        return self.page.locator(self.{el_name}).text_content()\n\n")
            elif action == "IsDisplayed":
                res.append(f"    def {m_name}(self):\n        return self.page.locator(self.{el_name}).is_visible()\n\n")
            elif action == "SelectByVisibleText":
                res.append(f"    def {m_name}(self, text):\n        self.page.locator(self.{el_name}).select_option(label=text)\n\n")
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
                res.append(f"    def {m_name}(self, driver, text):\n        Select(driver.find_element('xpath', self.{el_name})).select_by_visible_text(text)\n\n")
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
                res.append(f"    async {m_name}() {{\n        return await this.{el_name}.isVisible();\n    }}\n\n")
            elif action == "SelectByVisibleText":
                res.append(f"    async {m_name}(text) {{\n        await this.{el_name}.selectOption({{ label: text }});\n    }}\n\n")
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
        return "".join(res)
