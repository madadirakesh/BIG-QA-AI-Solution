import os
from behave import given, when, then
from pages.login_page import LoginPage
from pages.home_page import HomePage


@given("I launch the application")
def step_launch_application(context):
    context.driver.get(context.base_url)
    context.login_page = LoginPage(context.driver)


@when("I enter valid Username and Password")
def step_enter_credentials(context):
    username = os.getenv("USER", "standard_user")
    password = os.getenv("PASSWORD", "secret_sauce")
    context.login_page.enter_username(username)
    context.login_page.enter_password(password)


@when("I click the login button")
def step_click_login(context):
    context.login_page.click_login()
    context.home_page = HomePage(context.driver)


@then("I should be redirected to the homepage")
def step_verify_homepage(context):
    assert context.home_page.is_loaded(), (
        f"Expected homepage to load, but title was: {context.driver.title}"
    )
