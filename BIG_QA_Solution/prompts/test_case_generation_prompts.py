def get_bdd_scenario_generation_prompt(requirements: str) -> str:
    """
    Returns the prompt for generating BDD scenarios from requirements.
    
    # Used in file: BIG_QA_Solution/ScriptGenerator/api/backend.py
    # Used under function: generate_bdd_scenarios
    
    Args:
        requirements: The requirements text.
        
    Returns:
        The formatted prompt string.
    """
    return f"""
    You are an expert QA Automation Engineer.
    Given the following requirements, generate a complete and professional BDD Gherkin .feature file.
    
    Requirements:
    {requirements}
    
    Rules:
    1. Use standard Gherkin syntax (Feature, Scenario, Given, When, Then, And).
    2. Ensure scenarios cover positive and negative cases if applicable.
    3. Return ONLY the raw content of the .feature file. 
    4. Do NOT include markdown code fences (```gherkin) or any other conversational text.
    """

def get_test_case_generation_prompt(requirements: str, template: str) -> str:
    """
    Returns the prompt for generating formatted test cases from requirements and a template.
    
    # Used in file: BIG_QA_Solution/ScriptGenerator/api/backend.py
    # Used under function: generate_formatted_test_cases
    
    Args:
        requirements: The requirements text.
        template: The template string for formatting the output.
        
    Returns:
        The formatted prompt string.
    """
    return f"""
    You are an expert QA Automation Engineer.
    Your task is to generate and map the provided requirements into the provided test case template format.
    
    Requirements:
    {requirements}
    
    Template Format:
    {template}
    
    Instructions:
    1. Extract test cases from the Requirements.
    2. Cover all types of scenarios: Positive, Negative, Edge Cases, Boundary conditions, field level validations, Business rule validations, Error handling, Regression impact scenarios
    3. Return the test cases as a JSON object with a single key "test_cases".
    4. The value for "test_cases" MUST be a JSON array of objects.
    5. Each object in the array represents ONE test case.
    6. CRITICAL FATAL INSTRUCTION: The keys in each JSON object MUST STRICTLY be the exact column headers specified in the Template / Sample Format. 
       - You MUST create a distinct, separate JSON key-value pair (node) for EVERY single heading in the provided template.
       - Every column header provided in the Template MUST be a key in every JSON object.
       - Do NOT invent your own keys.
       - NEVER use standard keys like 'Step No', 'Pre-requisite', 'Test Data', 'Action' unless they are explicitly in the template.
       - You MUST map the test cases exactly to whatever keys the user provided in the Template string.
       - Example: if the template provided is ["A", "B", "C"], your JSON must be {{ "test_cases": [ {{ "A": "...", "B": "...", "C": "..." }} ] }}
       - Always provide steps to be executed with serial number
       - Always generate steps from the beginning of the functional flow  
    7. Include NO other information. Your entire response must be standard, parseable JSON.
    """
