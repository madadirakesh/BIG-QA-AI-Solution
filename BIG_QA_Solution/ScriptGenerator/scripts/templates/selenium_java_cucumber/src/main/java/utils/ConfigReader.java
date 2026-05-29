package utils;

import io.github.cdimascio.dotenv.Dotenv;

/**
 * Centralised, type-safe access to values from the project's .env file.
 *
 * Why a static Dotenv instance: dotenv-java parses the file on construction, so loading it once
 * at class-init avoids re-reading the file on every getProperty() call. ignoreIfMissing() lets
 * the project still compile and run if a developer forgets to copy .env locally — individual
 * getProperty() calls will then throw an IllegalStateException with the missing key.
 *
 * This class is identical to the ConfigReader used in every other Java template in this repo.
 * Keeping the pattern uniform means step definitions and page objects you write today will be
 * portable across Selenium and Playwright templates with no edits.
 */
public final class ConfigReader {

    private static final Dotenv DOTENV = Dotenv.configure().ignoreIfMissing().load();

    private ConfigReader() {
        // utility class — not meant to be instantiated.
    }

    /** Returns the value for {@code key}, or throws if it is missing/blank. */
    public static String getProperty(String key) {
        String value = DOTENV.get(key);
        if (value == null || value.isBlank()) {
            throw new IllegalStateException("Property '" + key + "' not found in .env file");
        }
        return value;
    }

    /** Returns the value for {@code key}, or {@code fallback} if it is missing/blank. */
    public static String getOrDefault(String key, String fallback) {
        String value = DOTENV.get(key);
        return (value == null || value.isBlank()) ? fallback : value;
    }

    /** Convenience accessor for the most commonly read property. */
    public static String getAppUrl() {
        return getProperty("APP_URL");
    }
}
