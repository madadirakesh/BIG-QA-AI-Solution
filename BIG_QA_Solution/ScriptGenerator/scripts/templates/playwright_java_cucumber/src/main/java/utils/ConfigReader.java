package utils;

import io.github.cdimascio.dotenv.Dotenv;
import javax.crypto.Cipher;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.Base64;

/**
 * Centralised, type-safe access to values from the project's .env file.
 *
 * Why a static Dotenv instance: dotenv-java parses the file on construction, so loading it once
 * at class-init avoids re-reading the file on every getProperty() call. ignoreIfMissing() lets
 * the project still compile and run if a developer forgets to copy .env locally — individual
 * getProperty() calls will then throw an IllegalStateException with the missing key.
 *
 * Step definitions and the PlaywrightFactory call into this class; never read process env or
 * properties directly elsewhere so we have one place to evolve config later (e.g. add per-env
 * overrides like .env.staging).
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
        // Secrets (e.g. PASSWORD) are stored as "ENC:<token>"; decrypt at run time so callers
        // always get plaintext. Non-encrypted values pass straight through.
        return decrypt(value);
    }

    /** Returns the value for {@code key}, or {@code fallback} if it is missing/blank. */
    public static String getOrDefault(String key, String fallback) {
        String value = DOTENV.get(key);
        return (value == null || value.isBlank()) ? fallback : decrypt(value);
    }

    /**
     * Decrypt an "ENC:<token>" value produced by the scaffolder (AES-256-GCM). Token layout:
     * "ENC:" + base64( nonce(12) || ciphertext || gcmTag(16) ); the key is the base64 CRED_KEY
     * in this project's .env. Values without the ENC: prefix are returned unchanged. Uses the
     * JDK's javax.crypto — no third-party dependency.
     */
    private static String decrypt(String value) {
        if (value == null || !value.startsWith("ENC:")) {
            return value;
        }
        String keyB64 = DOTENV.get("CRED_KEY");
        if (keyB64 == null || keyB64.isBlank()) {
            throw new IllegalStateException("CRED_KEY not found in .env file; cannot decrypt secret");
        }
        try {
            byte[] key = Base64.getDecoder().decode(keyB64);
            byte[] raw = Base64.getDecoder().decode(value.substring(4));
            byte[] nonce = Arrays.copyOfRange(raw, 0, 12);
            byte[] cipherAndTag = Arrays.copyOfRange(raw, 12, raw.length);
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE, new SecretKeySpec(key, "AES"), new GCMParameterSpec(128, nonce));
            return new String(cipher.doFinal(cipherAndTag), StandardCharsets.UTF_8);
        } catch (Exception e) {
            throw new IllegalStateException("Failed to decrypt secret from .env", e);
        }
    }

    /** Convenience accessor for the most commonly read property. */
    public static String getAppUrl() {
        return getProperty("APP_URL");
    }
}
