"""Validated dependency-version profiles offered at project-creation time.

Single source of truth for the version dropdown (app.py), the placeholder substitution
(bootstrapper_engine.py), and the CI smoke matrix. Only add a profile once it builds green.
"""

# (tool, language, framework) -> profiles; first entry is the default. Keys other than "label"
# are substituted into the template; a template ignores any version key it doesn't reference.
VALIDATED_VERSIONS = {
    ("Playwright", "Java", "Cucumber"): [
        {"label": "Stable — Playwright 1.45 / Cucumber 7.18 / JDK 11",
         "playwright": "1.45.0", "cucumber": "7.18.0", "java": "11"},
        {"label": "Latest — Playwright 1.49 / Cucumber 7.20 / JDK 17",
         "playwright": "1.49.0", "cucumber": "7.20.1", "java": "17"},
    ],
    ("Selenium", "Java", "Cucumber"): [
        {"label": "Stable — Selenium 4.21 / Cucumber 7.18 / JDK 11",
         "selenium": "4.21.0", "cucumber": "7.18.0", "java": "11"},
        {"label": "Latest — Selenium 4.25 / Cucumber 7.20 / JDK 17",
         "selenium": "4.25.0", "cucumber": "7.20.1", "java": "17"},
    ],
}

# Used only when a placeholder has no value in the resolved profile; mirrors the original pins.
FALLBACK_VERSIONS = {"playwright": "1.45.0", "selenium": "4.21.0", "cucumber": "7.18.0", "java": "11"}


def profiles_for(tool, language, framework):
    """Selectable profiles for the dropdown; empty list = template has no version choice."""
    return VALIDATED_VERSIONS.get((tool, language, framework), [])


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
