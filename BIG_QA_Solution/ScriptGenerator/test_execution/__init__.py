"""
Test Execution module.

Runs manual test cases (from an uploaded Excel workbook, Jira or Zephyr Scale)
in a real browser through Playwright MCP, captures evidence, and publishes the
results back to wherever the test cases came from.

Lifecycle:
    source adapter -> parse/validate -> normalize (models.TestCase)
    -> plan (step_parser, then ai_planner only for what the parser could not read)
    -> engine (playwright_mcp) -> evidence -> aggregate -> publishers -> dashboard
"""
