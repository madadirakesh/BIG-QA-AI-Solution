import { Before, After, Status, BeforeAll, AfterAll, IWorld } from "@cucumber/cucumber";
import { chromium, firefox, webkit, Browser, BrowserContext, Page } from "@playwright/test";
const fs = require('fs-extra');
import * as path from "path";
import { execSync } from "child_process";

// ICustomWorld is the Cucumber "World" object \u2014 the `this` available inside every step.
// We export it so generated step-definition files can type their callbacks as
// `function (this: ICustomWorld)` and safely use `this.page`. The Before hook below assigns
// `this.page` for each scenario, so it is always populated by the time a step runs. We keep
// the module-level `page` export too, for step files that prefer `import { page }`.
export interface ICustomWorld extends IWorld {
    page?: Page;
    context?: BrowserContext;
    browser?: Browser;
}

let browser: Browser;
let context: BrowserContext;
export let page: Page;

BeforeAll(async function () {
    try {
        console.log("🔍 Validating environment dependencies...");
        // This check mimics the 'install_dependencies' logic in your invoker
        execSync("npm list @playwright/test", { stdio: "ignore" });
        // Install Playwright browsers
        execSync("npx playwright install", { stdio: "ignore" });
    } catch (error) {
        console.error("❌ Required packages missing. Executing emergency install...");
        execSync("npm install", { stdio: "inherit" });
        execSync("npx playwright install", { stdio: "inherit" });
    }
    // Rule 4: Create result folder with timestamp
    const resultDir = process.env.RESULT_DIR || path.join(process.cwd(), "results", new Date().toISOString().replace(/[:.]/g, "-"));
    fs.ensureDirSync(resultDir);
    fs.ensureDirSync(path.join(resultDir, "screenshots"));
});

Before(async function (this: ICustomWorld) {
    // Rule 8: Configurable browser
    const browserType = process.env.BROWSER || "chromium";
    const launchOptions = { headless: process.env.HEADLESS === "true" };

    if (browserType === "firefox") browser = await firefox.launch(launchOptions);
    else if (browserType === "webkit") browser = await webkit.launch(launchOptions);
    else browser = await chromium.launch(launchOptions);

    // Bypassing SSL errors as standard QA practice
    context = await browser.newContext({ ignoreHTTPSErrors: true });
    page = await context.newPage();
    this.page = page;
});

After(async function (this: ICustomWorld, scenario) {
    // Rule 6: Screenshot on failure
    if (scenario.result?.status === Status.FAILED) {
        const resultDir = process.env.RESULT_DIR || path.join(process.cwd(), "results", new Date().toISOString().replace(/[:.]/g, "-"));
        const image = await page.screenshot({ path: path.join(resultDir, "screenshots", `${scenario.pickle.name}.png`), fullPage: true });
        await this.attach(image, "image/png");
    }
    // Rule 5: Close instances
    await page.close();
    await context.close();
    await browser.close();
});