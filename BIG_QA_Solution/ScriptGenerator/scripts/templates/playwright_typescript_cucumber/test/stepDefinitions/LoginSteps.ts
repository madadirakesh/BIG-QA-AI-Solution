import { Given, Then, When } from "@cucumber/cucumber";
import { LoginPage } from "../pageObjects/LoginPage";
import { ConfigReader } from "../utils/configReader";
import { ICustomWorld } from "../hooks/hooks";

function loginPage(world: ICustomWorld): LoginPage {
    if (!world.page) {
        throw new Error("Playwright page is unavailable. Ensure the Cucumber Before hook is loaded.");
    }
    return new LoginPage(world.page);
}

Given("I launch the application", async function (this: ICustomWorld) {
    await loginPage(this).open(ConfigReader.getEnvUrl());
});

When("I enter valid Username and Password", async function (this: ICustomWorld) {
    await loginPage(this).enterCredentials(
        ConfigReader.getProperty("USER"),
        ConfigReader.getProperty("PASSWORD"),
    );
});

When("I click the login button", async function (this: ICustomWorld) {
    await loginPage(this).submit();
});

Then("I should be redirected to the homepage", async function (this: ICustomWorld) {
    if (!await loginPage(this).isLoaded()) {
        throw new Error("Expected the application page to be loaded after the login attempt.");
    }
});
