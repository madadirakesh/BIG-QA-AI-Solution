Feature: Login Functionality

  # Sample feature so the user has a starting point. Step definitions are intentionally NOT
  # included in this template — they are generated later by the Script Developer wizard (AI),
  # which is why the smoke test is skipped for Playwright + Java projects at creation time.
  Scenario: Verify Successful Login
    Given I launch the application
    When I enter valid Username and Password
    And I click the login button
    Then I should be redirected to the homepage
