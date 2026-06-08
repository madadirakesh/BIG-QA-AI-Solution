import os
from dotenv import load_dotenv

load_dotenv()


class ConfigReader:
    """Centralised helper to read configuration from the .env file.

    Identical contract to the Selenium/Python template's ConfigReader so utilities stay portable
    across the two Python templates.
    """

    @staticmethod
    def get(key: str, default: str = None) -> str:
        value = os.getenv(key, default)
        if value is None:
            raise EnvironmentError(
                f"Required environment variable '{key}' is not set in .env"
            )
        return value

    @staticmethod
    def get_app_url() -> str:
        return ConfigReader.get("APP_URL")

    @staticmethod
    def get_browser() -> str:
        # Playwright browser names: chromium | firefox | webkit (defaults to chromium).
        return ConfigReader.get("BROWSER", "chromium")

    @staticmethod
    def is_headless() -> bool:
        return ConfigReader.get("HEADLESS", "false").lower() == "true"
