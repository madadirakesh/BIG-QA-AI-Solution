/*
 * playwright_steps.groovy  -  one script, one step per JSR223 Sampler.
 * Parameter: start | navigate | verify | stop
 *
 * The browser lives in JMeter thread variables, so each virtual user has its own
 * browser and each step is timed as its own sample in the reports.
 *
 * JMeter properties (set via Maven -Dperf.pw.*):
 *   pw.baseUrl, pw.headless, pw.expectedHeading, results.dir
 */
import com.microsoft.playwright.*
import java.nio.file.Paths

String action = args[0]
String baseUrl = props.getProperty('pw.baseUrl', 'https://example.com')
boolean headless = props.getProperty('pw.headless', 'true').toBoolean()
String expectedHeading = props.getProperty('pw.expectedHeading', 'Example Domain')
String resultsDir = props.getProperty('results.dir', 'Results')

Page page = (Page) vars.getObject('pw.page')

try {
    if (action == 'start') {
        Playwright playwright = Playwright.create()
        Browser browser = playwright.chromium().launch(new BrowserType.LaunchOptions().setHeadless(headless))
        vars.putObject('pw.playwright', playwright)
        vars.putObject('pw.browser', browser)
        vars.putObject('pw.page', browser.newPage())
        SampleResult.setResponseMessage('Chromium started (headless=' + headless + ')')

    } else if (action == 'navigate') {
        Response response = page.navigate(baseUrl)
        SampleResult.setResponseCode(String.valueOf(response.status()))
        SampleResult.setResponseMessage(page.title())
        if (response.status() >= 400) {
            throw new AssertionError('Navigation to ' + baseUrl + ' returned HTTP ' + response.status())
        }

    } else if (action == 'verify') {
        String heading = page.locator('h1').first().innerText()
        SampleResult.setResponseData(heading, 'UTF-8')
        if (!heading.contains(expectedHeading)) {
            throw new AssertionError("Expected heading '" + expectedHeading + "' but found '" + heading + "'")
        }
        SampleResult.setResponseMessage('Heading verified: ' + heading)

    } else if (action == 'stop') {
        (vars.getObject('pw.browser') as Browser)?.close()
        (vars.getObject('pw.playwright') as Playwright)?.close()
        SampleResult.setResponseMessage('Browser closed')

    } else {
        throw new IllegalArgumentException('Unknown action: ' + action)
    }
} catch (Throwable t) {
    SampleResult.setSuccessful(false)
    SampleResult.setResponseCode('500')
    SampleResult.setResponseMessage(t.getMessage())
    log.error("Playwright step '${action}' failed", t)
    try {                                    // screenshot for debugging
        if (page != null) {
            new File(resultsDir, 'screenshots').mkdirs()
            page.screenshot(new Page.ScreenshotOptions()
                    .setPath(Paths.get(resultsDir, 'screenshots', action + '-' + System.currentTimeMillis() + '.png')))
        }
    } catch (Throwable ignored) { }
}
