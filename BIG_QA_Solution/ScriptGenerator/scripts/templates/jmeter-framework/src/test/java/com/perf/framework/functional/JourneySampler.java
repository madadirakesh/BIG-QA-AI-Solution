package com.perf.framework.functional;

import java.io.PrintWriter;
import java.io.StringWriter;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;

import org.apache.jmeter.config.Arguments;
import org.apache.jmeter.protocol.java.sampler.AbstractJavaSamplerClient;
import org.apache.jmeter.protocol.java.sampler.JavaSamplerContext;
import org.apache.jmeter.samplers.SampleResult;
import org.apache.jmeter.threads.JMeterContextService;
import org.apache.jmeter.threads.JMeterVariables;
import org.apache.jmeter.util.JMeterUtils;
import org.openqa.selenium.WebDriver;

/**
 * JMeter "Java Request" sampler that runs one step of a recorded Selenium journey.
 *
 * The plans in TestScripts/functional use one sampler per user action, so every
 * action is timed as its own sample. Plain Java rather than a JSR223/Groovy script:
 * the Groovy bundled with JMeter 5.6.3 cannot read Java 22+ class files, which made
 * scripted steps fail on current JDKs.
 *
 * Parameters:
 *   journey  fully qualified class of the journey (extends SeleniumSupport)
 *   action   launch | step | close
 *   step     method to call for action=step (e.g. step03)
 *
 * JMeter properties: selenium.headless (falls back to pw.headless, default true),
 * selenium.baseUrl (default: the journey's recorded DEFAULT_BASE_URL).
 */
public class JourneySampler extends AbstractJavaSamplerClient {

    private static final String JOURNEY_VAR = "selenium.journey";
    private static final String FAILED_VAR = "selenium.failed";

    @Override
    public Arguments getDefaultParameters() {
        Arguments arguments = new Arguments();
        arguments.addArgument("journey", "");
        arguments.addArgument("action", "step");
        arguments.addArgument("step", "");
        return arguments;
    }

    @Override
    public SampleResult runTest(JavaSamplerContext context) {
        SampleResult result = new SampleResult();
        result.setDataType(SampleResult.TEXT);
        JMeterVariables vars = JMeterContextService.getContext().getVariables();
        String action = context.getParameter("action", "step");
        try {
            if ("launch".equals(action)) {
                vars.remove(FAILED_VAR);
                boolean headless = Boolean.parseBoolean(
                        JMeterUtils.getPropDefault("selenium.headless", JMeterUtils.getPropDefault("pw.headless", "true")));
                Class<?> type = Class.forName(context.getParameter("journey"), true,
                        Thread.currentThread().getContextClassLoader());
                String baseUrl = JMeterUtils.getPropDefault("selenium.baseUrl",
                        String.valueOf(type.getField("DEFAULT_BASE_URL").get(null)));
                result.sampleStart();
                WebDriver driver = SeleniumSupport.createDriver(headless);
                Object journey = type.getConstructor(WebDriver.class, String.class).newInstance(driver, baseUrl);
                result.sampleEnd();
                vars.putObject(JOURNEY_VAR, journey);
                succeed(result, "Chrome started (headless=" + headless + ", base URL " + baseUrl + ")");

            } else if ("close".equals(action)) {
                Object journey = vars.getObject(JOURNEY_VAR);
                result.sampleStart();
                if (journey instanceof SeleniumSupport) {
                    ((SeleniumSupport) journey).quit();
                }
                result.sampleEnd();
                vars.putObject(JOURNEY_VAR, null);
                succeed(result, "Browser closed");

            } else {
                Object journey = vars.getObject(JOURNEY_VAR);
                if (journey == null || vars.get(FAILED_VAR) != null) {
                    // The browser did not start, or an earlier step failed: skip rather than time out.
                    result.setIgnore();
                    return result;
                }
                Method step = journey.getClass().getMethod(context.getParameter("step"));
                result.sampleStart();
                try {
                    step.invoke(journey);
                } finally {
                    result.sampleEnd();
                }
                succeed(result, "OK");
            }
        } catch (Throwable failure) {
            Throwable cause = failure instanceof InvocationTargetException && failure.getCause() != null
                    ? failure.getCause() : failure;
            if (result.getStartTime() == 0) {
                result.sampleStart();
            }
            if (result.getEndTime() == 0) {
                result.sampleEnd();
            }
            vars.put(FAILED_VAR, "true");
            StringWriter trace = new StringWriter();
            cause.printStackTrace(new PrintWriter(trace));
            result.setSuccessful(false);
            result.setResponseCode("500");
            result.setResponseMessage(cause.getClass().getSimpleName() + ": " + firstLine(cause.getMessage()));
            result.setResponseData(trace.toString(), StandardCharsets.UTF_8.name());
        }
        return result;
    }

    private static void succeed(SampleResult result, String message) {
        result.setSuccessful(true);
        result.setResponseCodeOK();
        result.setResponseMessage(message);
        result.setResponseData(message, StandardCharsets.UTF_8.name());
    }

    private static String firstLine(String message) {
        if (message == null) {
            return "";
        }
        int newline = message.indexOf('\n');
        return newline == -1 ? message : message.substring(0, newline);
    }
}
