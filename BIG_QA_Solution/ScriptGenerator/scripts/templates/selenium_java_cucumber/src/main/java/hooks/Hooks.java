package hooks;

import io.cucumber.java.After;
import io.cucumber.java.Before;
import io.cucumber.java.Scenario;
import org.openqa.selenium.OutputType;
import org.openqa.selenium.TakesScreenshot;
import org.openqa.selenium.WebDriver;
import utils.DriverFactory;

import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;

/**
 * Cucumber lifecycle hooks that wrap a fresh WebDriver around each scenario.
 *
 * Wiring:
 *   - This class lives in the `hooks` package, which is listed in cucumber.glue (see
 *     src/test/resources/junit-platform.properties). That registration is what makes Cucumber
 *     scan for @Before / @After here.
 *   - DriverFactory owns the actual browser; this class just decides when to start/stop it
 *     and what to capture on failure. Identical shape to the Playwright/Java template's hooks.
 *
 * Failure capture:
 *   - On a failed scenario we attach the screenshot to the Cucumber report (so it shows up in
 *     cucumber.html) AND save a timestamped copy to Results/screenshots/ on disk. If the disk
 *     write fails we swallow the error — the in-report attachment is the primary artefact.
 */
public class Hooks {

    @Before
    public void beforeScenario() {
        // Fresh driver per scenario so cookies / sessionStorage / window state never leak
        // between tests.
        DriverFactory.startDriver();
    }

    @After
    public void afterScenario(Scenario scenario) {
        WebDriver driver = DriverFactory.getDriver();
        if (scenario.isFailed() && driver != null) {
            try {
                byte[] screenshot = ((TakesScreenshot) driver).getScreenshotAs(OutputType.BYTES);
                scenario.attach(screenshot, "image/png", scenario.getName());

                // Sanitise scenario name so the file path is safe on every OS.
                String safeName = scenario.getName().replaceAll("[^a-zA-Z0-9-_]", "_");
                String timestamp = LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyyMMdd_HHmmss"));
                Path dir = Paths.get("Results", "screenshots");
                Files.createDirectories(dir);
                Files.write(dir.resolve(safeName + "_" + timestamp + ".png"), screenshot);
            } catch (Exception ignored) {
                // Disk archival is best-effort; the report attachment above is sufficient.
            }
        }
        DriverFactory.quitDriver();
    }
}
