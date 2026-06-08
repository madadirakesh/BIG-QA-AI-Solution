Feature: Login Functionality

  # Sample feature so the user has a starting point. A matching, runnable step-definition class
  # ships with this template (stepDefinitions/LoginSteps.java, backed by pageObjects/LoginPage.java)
  # so `mvn test` passes out of the box. Those steps are deliberately lenient — they navigate to
  # APP_URL and verify the page loaded — and are meant to be replaced by the Script Developer
  # wizard (AI) or by your own real tests. The smoke test is still skipped at creation time to
  # avoid a full Maven build + Playwright browser download during the interactive flow.
  Scenario: Verify Successful Login
    Given I launch the application
    When I enter valid Username and Password
    And I click the login button
    Then I should be redirected to the homepage
