package runners;

import org.junit.platform.suite.api.ConfigurationParameter;
import org.junit.platform.suite.api.IncludeEngines;
import org.junit.platform.suite.api.SelectClasspathResource;
import org.junit.platform.suite.api.Suite;

import static io.cucumber.junit.platform.engine.Constants.GLUE_PROPERTY_NAME;
import static io.cucumber.junit.platform.engine.Constants.PLUGIN_PROPERTY_NAME;

/**
 * Entry point Maven Surefire picks up to run the Cucumber suite via the JUnit Platform.
 *
 * The @Suite annotation marks this as a JUnit Platform test suite. Cucumber's
 * cucumber-junit-platform-engine registers itself as an engine named "cucumber", which we
 * include below. The classpath resource "features" resolves to src/test/resources/features
 * (where the .feature files live).
 *
 * The two @ConfigurationParameter values duplicate what is also set in
 * src/test/resources/junit-platform.properties. They are repeated here so a developer reading
 * just this file can see the glue and reporter wiring at a glance; the properties file remains
 * the source of truth if you only run via `mvn test`.
 */
@Suite
@IncludeEngines("cucumber")
@SelectClasspathResource("features")
@ConfigurationParameter(key = GLUE_PROPERTY_NAME, value = "stepDefinitions,hooks")
@ConfigurationParameter(key = PLUGIN_PROPERTY_NAME, value = "pretty, html:Results/cucumber.html, json:Results/cucumber.json")
public class TestRunner {
}
