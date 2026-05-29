package hooks;

import com.microsoft.playwright.Page;
import io.cucumber.java.After;
import io.cucumber.java.Before;
import io.cucumber.java.Scenario;
import utils.PlaywrightFactory;

import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;

/**
 * Cucumber lifecycle hooks that wrap the Playwright session around each scenario.
 *
 * Wiring:
 *   - This class lives in the `hooks` package, which is listed in cucumber.glue (see
 *     src/test/resources/junit-platform.properties). That registration is what makes Cucumber
 *     scan for @Before / @After here.
 *   - PlaywrightFactory owns the actual browser; this class just decides when to start and stop
 *     it and what to capture on failure.
 *
 * Failure capture:
 *   - On a failed scenario we both attach the screenshot to the Cucumber report (via
 *     scenario.attach) and persist a copy to Results/screenshots/ on disk for archival. If the
 *     disk write fails we silently swallow — the in-report attachment is the primary artefact.
 */
public class Hooks {

    @Before
    public void beforeScenario() {
        // Start a fresh browser/page for every scenario so state never leaks between tests.
        PlaywrightFactory.startBrowser();
    }

    @After
    public void afterScenario(Scenario scenario) {
        Page page = PlaywrightFactory.getPage();
        if (scenario.isFailed() && page != null) {
            byte[] screenshot = page.screenshot();
            scenario.attach(screenshot, "image/png", scenario.getName());
            try {
                // Sanitise scenario name so the file path is safe on all OSes.
                String safeName = scenario.getName().replaceAll("[^a-zA-Z0-9-_]", "_");
                String timestamp = LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyyMMdd_HHmmss"));
                Path dir = Paths.get("Results", "screenshots");
                Files.createDirectories(dir);
                Files.write(dir.resolve(safeName + "_" + timestamp + ".png"), screenshot);
            } catch (Exception ignored) {
                // Disk archival is best-effort; report attachment above is sufficient.
            }
        }
        PlaywrightFactory.closeBrowser();
    }
}
