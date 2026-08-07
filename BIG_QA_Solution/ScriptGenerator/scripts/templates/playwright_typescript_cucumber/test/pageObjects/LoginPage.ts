import { Page } from "@playwright/test";

export class LoginPage {
    private readonly usernameSelector =
        "#txtUserID, input[autocomplete='username'], input[type='email'], input[name*='user' i], input[id*='user' i], input[type='text']";
    private readonly passwordSelector =
        "#txtPassword, input[autocomplete='current-password'], input[type='password']";
    private readonly submitSelector =
        "#sub, button[type='submit'], input[type='submit'], button:has-text('Login'), button:has-text('Sign in')";

    public constructor(private readonly page: Page) {}

    public async open(url: string): Promise<void> {
        await this.page.goto(url, { waitUntil: "domcontentloaded" });
    }

    public async enterCredentials(username: string, password: string): Promise<void> {
        await this.fillFirstIfPresent(this.usernameSelector, username);
        await this.fillFirstIfPresent(this.passwordSelector, password);
    }

    public async submit(): Promise<void> {
        const button = this.page.locator(this.submitSelector).first();
        if (await button.count()) {
            await button.click().catch(() => undefined);
            await this.page.waitForLoadState("domcontentloaded").catch(() => undefined);
        }
    }

    public async isLoaded(): Promise<boolean> {
        return this.page.url().length > 0 && await this.page.locator("body").count() > 0;
    }

    private async fillFirstIfPresent(selector: string, value: string): Promise<void> {
        const field = this.page.locator(selector).first();
        if (await field.count()) {
            await field.fill(value).catch(() => undefined);
        }
    }
}
