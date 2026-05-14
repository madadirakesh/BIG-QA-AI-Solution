def get_locator_generation_prompt(tool: str, extra_tooling: str, outer_html: str) -> str:
    """
    Returns the prompt for generating element locators based on HTML snippet.
    
    # Used in file: BIG_QA_Solution/ElementLocator/ai_service.py
    # Used under function: generate_locators
    
    Args:
        tool: The automation tool (e.g., 'playwright', 'selenium').
        extra_tooling: Extra tooling locators string (e.g., ', getByTestId').
        outer_html: The HTML snippet to analyze.
        
    Returns:
        The formatted prompt string.
    """
    return (
        f"Analyze this HTML snippet and suggest the best element locators for a '{tool}' automation script. "
        "Return ONLY a JSON array of objects, where each object has 'name', 'type', 'value', and 'rating' fields. "
        "The 'name' should be a descriptive, camelCase element name based on the HTML attributes (e.g., loginButton, emailInput). "
        f"The 'type' should be one of (CSS, XPath, ID, Name, Link Text, Partial Link, Tag Name{extra_tooling}). "
        "The XPath is relative XPath. Use Contains, Text, and other attributes as applicable. "
        "The 'rating' MUST be exactly one of: 'Best', 'Good', 'Ok', 'Un-Reliable'. "
        f"HTML:\n{outer_html}"
    )
