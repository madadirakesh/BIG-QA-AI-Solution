import * as dotenv from "dotenv";
import * as path from "path";

// Load .env file from the root directory
dotenv.config({ path: path.join(__dirname, "../../.env") });

export class ConfigReader {
    public static getProperty(key: string): string {
        const value = process.env[key];
        if (!value) {
            throw new Error(`Property ${key} not found in .env file`);
        }
        return value;
    }

    public static getEnvUrl(): string {
        return this.getProperty("APP_URL");
    }
}