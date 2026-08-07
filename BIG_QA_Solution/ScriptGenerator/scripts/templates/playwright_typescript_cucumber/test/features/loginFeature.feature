Feature: Login Functionality

  @smoke
  Scenario: Verify Successful Login
    Given I launch the application
    When I enter valid Username and Password
    And I click the login button
    Then I should be redirected to the homepage
