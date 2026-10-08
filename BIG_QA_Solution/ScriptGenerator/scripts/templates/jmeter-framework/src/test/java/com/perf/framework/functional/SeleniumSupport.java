package com.perf.framework.functional;

import java.time.Duration;

import org.openqa.selenium.By;
import org.openqa.selenium.ElementClickInterceptedException;
import org.openqa.selenium.JavascriptExecutor;
import org.openqa.selenium.Keys;
import org.openqa.selenium.NoSuchElementException;
import org.openqa.selenium.StaleElementReferenceException;
import org.openqa.selenium.TimeoutException;
import org.openqa.selenium.WebDriver;
import org.openqa.selenium.WebDriverException;
import org.openqa.selenium.WebElement;
import org.openqa.selenium.chrome.ChromeDriver;
import org.openqa.selenium.chrome.ChromeOptions;
import org.openqa.selenium.support.ui.ExpectedCondition;
import org.openqa.selenium.support.ui.ExpectedConditions;
import org.openqa.selenium.support.ui.Select;
import org.openqa.selenium.support.ui.WebDriverWait;

/**
 * Base class for the functional journeys recorded from the Performance Test page.
 *
 * A generated journey extends this class and implements one method per recorded
 * user action (step01(), step02(), ...). The JMeter plan in TestScripts/functional
 * calls each method from its own JSR223 sampler, so every action is timed as a
 * separate sample - including the wait for its element, which is what a real user
 * waits for too.
 *
 * System properties: selenium.wait (seconds an action waits for its element, default 15).
 */
public abstract class SeleniumSupport {

    protected static final Duration WAIT = Duration.ofSeconds(Long.getLong("selenium.wait", 15L));

    protected final WebDriver driver;
    protected final String baseUrl;

    protected SeleniumSupport(WebDriver driver, String baseUrl) {
        this.driver = driver;
        this.baseUrl = baseUrl == null ? "" : baseUrl.replaceAll("/+$", "");
    }

    /** Chrome, headless unless asked otherwise; Selenium Manager resolves the driver binary. */
    public static WebDriver createDriver(boolean headless) {
        ChromeOptions options = new ChromeOptions();
        if (headless) {
            options.addArguments("--headless=new");
        }
        options.addArguments("--window-size=1920,1080");
        return new ChromeDriver(options);
    }

    public void quit() {
        driver.quit();
    }

    protected String url(String pathOrUrl) {
        return pathOrUrl.matches("(?i)^https?://.*") ? pathOrUrl : baseUrl + pathOrUrl;
    }

    protected void open(String pathOrUrl) {
        driver.get(url(pathOrUrl));
    }

    /** Wait for a navigation an earlier action triggered. Soft: redirects may land elsewhere. */
    protected void waitForUrl(String fragment) {
        try {
            new WebDriverWait(driver, WAIT).until(d -> d.getCurrentUrl().contains(fragment));
        } catch (TimeoutException ignored) {
            // The next action's own wait decides whether the page is usable.
        }
    }

    /**
     * Wait for the element in the page or any of its iframes (the recorder captures
     * actions inside frames too). Found in a frame, the driver stays switched into it
     * for the action; the next step starts from the top document again.
     */
    protected WebElement find(By by, ExpectedCondition<WebElement> condition) {
        return new WebDriverWait(driver, WAIT)
                .withMessage(by + " was not found")
                .until(d -> {
                    d.switchTo().defaultContent();
                    WebElement found = check(d, condition);
                    if (found != null) {
                        return found;
                    }
                    for (WebElement frame : d.findElements(By.tagName("iframe"))) {
                        try {
                            d.switchTo().frame(frame);
                        } catch (WebDriverException gone) {
                            continue;
                        }
                        found = check(d, condition);
                        if (found != null) {
                            return found;
                        }
                        d.switchTo().defaultContent();
                    }
                    return null;
                });
    }

    private static WebElement check(WebDriver d, ExpectedCondition<WebElement> condition) {
        try {
            return condition.apply(d);
        } catch (NoSuchElementException | StaleElementReferenceException notYet) {
            return null;
        }
    }

    private void clickElement(WebElement element) {
        try {
            element.click();
        } catch (ElementClickInterceptedException covered) {
            // An overlay (cookie banner, sticky header) is over the element.
            ((JavascriptExecutor) driver).executeScript("arguments[0].click();", element);
        }
    }

    protected void click(By by) {
        clickElement(find(by, ExpectedConditions.elementToBeClickable(by)));
    }

    protected void setChecked(By by, boolean checked) {
        // Styled checkboxes are often visually hidden, so wait for presence only.
        WebElement element = find(by, ExpectedConditions.presenceOfElementLocated(by));
        if (element.isSelected() != checked) {
            clickElement(element);
        }
    }

    protected void type(By by, String text) {
        WebElement element = find(by, ExpectedConditions.visibilityOfElementLocated(by));
        element.clear();
        element.sendKeys(text);
    }

    protected void select(By by, String visibleText) {
        new Select(find(by, ExpectedConditions.presenceOfElementLocated(by))).selectByVisibleText(visibleText);
    }

    protected void pressEnter(By by) {
        find(by, ExpectedConditions.visibilityOfElementLocated(by)).sendKeys(Keys.ENTER);
    }

    protected void submit(By by) {
        find(by, ExpectedConditions.presenceOfElementLocated(by)).submit();
    }

    protected static String env(String name) {
        String value = System.getenv(name);
        return value == null ? "" : value;
    }
}
