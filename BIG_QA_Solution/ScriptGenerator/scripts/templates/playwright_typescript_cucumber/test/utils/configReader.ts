import * as dotenv from "dotenv";
import * as path from "path";
import * as crypto from "crypto";

// Load .env file from the root directory
const envFilePath = path.join(__dirname, "../../.env");
const parsedEnv = dotenv.config({ path: envFilePath }).parsed || {};

export class ConfigReader {
    public static getProperty(key: string): string {
        const value = parsedEnv[key] ?? process.env[key];
        if (!value) {
            throw new Error(`Property ${key} not found in .env file`);
        }
        // Secrets (e.g. PASSWORD) are stored encrypted as "ENC:<token>" and decrypted here at
        // run time, so callers always receive plaintext. Non-encrypted values pass through.
        return ConfigReader.decrypt(value);
    }

    public static getEnvUrl(): string {
        return this.getProperty("APP_URL");
    }

    /**
     * Decrypt an "ENC:<token>" value produced by the scaffolder (AES-256-GCM).
     * Token layout: "ENC:" + base64( nonce(12) || ciphertext || gcmTag(16) ); the key is the
     * base64 CRED_KEY written into this project's .env. Anything without the ENC: prefix is
     * returned unchanged. Uses Node's built-in crypto — no extra dependency.
     */
    private static decrypt(value: string): string {
        if (!value.startsWith("ENC:")) {
            return value;
        }
        const keyB64 = parsedEnv["CRED_KEY"] ?? process.env["CRED_KEY"];
        if (!keyB64) {
            throw new Error("CRED_KEY not found in .env file; cannot decrypt secret");
        }
        const key = Buffer.from(keyB64, "base64");
        const raw = Buffer.from(value.slice(4), "base64");
        const nonce = raw.subarray(0, 12);
        const tag = raw.subarray(raw.length - 16);
        const ciphertext = raw.subarray(12, raw.length - 16);
        const decipher = crypto.createDecipheriv("aes-256-gcm", key, nonce);
        decipher.setAuthTag(tag);
        return Buffer.concat([decipher.update(ciphertext), decipher.final()]).toString("utf8");
    }
}
