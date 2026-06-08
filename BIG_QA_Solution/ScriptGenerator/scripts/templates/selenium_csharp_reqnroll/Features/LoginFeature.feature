Feature: Login Functionality

  # Sample feature with matching, runnable step definitions (StepDefinitions/LoginSteps.cs, backed
  # by PageObjects/LoginPage.cs) so `dotnet test` passes out of the box. The steps are lenient
  # (navigate to APP_URL, verify the page loaded) and meant to be replaced by the Script Developer
  # wizard (AI) or your own real tests.
  Scenario: Verify Successful Login
    Given I launch the application
    When I enter valid Username and Password
    And I click the login button
    Then I should be redirected to the homepage
