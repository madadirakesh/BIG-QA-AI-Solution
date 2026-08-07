"""Validated dependency-version profiles offered at project-creation time.

Single source of truth for the version dropdown (app.py), the placeholder substitution
(bootstrapper_engine.py), and the CI smoke matrix.

Each profile's keys (other than "label") are substituted into the template wherever the matching
{{<KEY>_VERSION}} placeholder appears — e.g. "playwright": "1.49.0" fills {{PLAYWRIGHT_VERSION}}.
A template simply ignores any version key it does not reference, and the substitution in
bootstrapper_engine is data-driven, so adding a new key here needs no engine change.

Validation note: Java/TypeScript/C# profiles are exact version sets. Python templates differ:
their requirements files treat the catalog values as minimum tested floors and let pip resolve a
newer compatible release for the user's interpreter/OS. That keeps Python project generation
working across newer CPython builds without hard-coding one exact wheel set per platform.
"""

# (tool, language, framework) -> profiles; first entry is the default shown/selected in the UI.
VALIDATED_VERSIONS = {
    ("Playwright", "Java", "Cucumber"): [
        {"label": "Stable — Playwright 1.45 / Cucumber 7.18 / JDK 11",
         "playwright": "1.45.0", "cucumber": "7.18.0", "java": "11"},
        {"label": "Latest — Playwright 1.61 / Cucumber 7.34 / JDK 17",
         "playwright": "1.61.0", "cucumber": "7.34.4", "java": "17"},
    ],
    ("Selenium", "Java", "Cucumber"): [
        {"label": "Stable — Selenium 4.21 / Cucumber 7.18 / JDK 11",
         "selenium": "4.21.0", "cucumber": "7.18.0", "java": "11"},
        {"label": "Latest — Selenium 4.46 / Cucumber 7.34 / JDK 17",
         "selenium": "4.46.0", "cucumber": "7.34.4", "java": "17"},
    ],
    # ── TypeScript ──────────────────────────────────────────────────────────────────────────
    # Fills {{PLAYWRIGHT_VERSION}} / {{CUCUMBER_VERSION}} / {{TYPESCRIPT_VERSION}} in package.json.
    ("Playwright", "TypeScript", "Cucumber"): [
        {"label": "Stable — Playwright 1.45 / Cucumber 11.2 / TS 5.2",
         "playwright": "1.45.0", "cucumber": "11.2.0", "typescript": "5.2.2", "node": "18"},
        {"label": "Latest — Playwright 1.61 / Cucumber 13.1 / TS 7.0",
         "playwright": "1.61.1", "cucumber": "13.1.1", "typescript": "7.0.2", "node": "20"},
    ],
    # ── Python ──────────────────────────────────────────────────────────────────────────────
    # Behave stays pinned; Playwright/Selenium are minimum tested floors in requirements.txt.
    ("Playwright", "Python", "Behave"): [
        {"label": "Stable — Playwright 1.45+ / Behave 1.2.6",
         "playwright": "1.45.0", "python": "3.10"},
        {"label": "Latest — Playwright 1.61+ / Behave 1.2.6",
         "playwright": "1.61.0", "python": "3.12"},
    ],
    ("Selenium", "Python", "Behave"): [
        {"label": "Stable — Selenium 4.21+ / Behave 1.2.6",
         "selenium": "4.21.0", "python": "3.10"},
        {"label": "Latest — Selenium 4.46+ / Behave 1.2.6",
         "selenium": "4.46.0", "python": "3.12"},
    ],
    # ── C# ──────────────────────────────────────────────────────────────────────────────────
    # Fills {{SELENIUM_VERSION}} / {{REQNROLL_VERSION}} in the .csproj.
    ("Selenium", "C#", "Reqnroll"): [
        {"label": "Stable — Selenium 4.21 / Reqnroll 2.1 / NUnit 4.2",
         "selenium": "4.21.0", "reqnroll": "2.1.0", "dotnet": "8.0"},
        {"label": "Latest — Selenium 4.46 / Reqnroll 3.3 / NUnit 4.6",
         "selenium": "4.46.0", "reqnroll": "3.3.4", "dotnet": "8.0"},
    ],
}

# Used only when a template references a {{KEY_VERSION}} placeholder the resolved profile happens
# not to define — a safety net so a stray placeholder never resolves to an empty string.
FALLBACK_VERSIONS = {
    "playwright": "1.61.0",
    "selenium": "4.46.0",
    "cucumber": "7.34.4",
    "java": "11",
    "typescript": "7.0.2",
    "reqnroll": "3.3.4",
    "python": "3.12",
    "node": "20",
    "dotnet": "8.0",
}

# The UI / DB use a few spellings for the same language (e.g. the web modal sends "Typescript",
# the engine normalises to "TypeScript"). Normalise here so profiles_for() / resolve_versions()
# resolve the same catalog row no matter which caller (the dropdown API vs the generator) asks.
_LANGUAGE_ALIASES = {
    "typescript": "TypeScript",
    "javascript": "TypeScript",
    "js": "TypeScript",
    "ts": "TypeScript",
    "js / ts": "TypeScript",
}


def _normalize_language(language):
    return _LANGUAGE_ALIASES.get((language or "").strip().lower(), language)


def profiles_for(tool, language, framework):
    """Selectable profiles for the dropdown; empty list = template has no version choice."""
    return VALIDATED_VERSIONS.get((tool, _normalize_language(language), framework), [])


def resolve_versions(tool, language, framework, label=None):
    """Resolve a profile label to its version dict; falls back to the default on None/unknown."""
    combos = profiles_for(tool, language, framework)
    if not combos:
        return {}
    if label:
        for combo in combos:
            if combo["label"] == label:
                return combo
    return combos[0]
