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
    return (
f"""
    You are an expert QA Automation Engineer.
    Given the following requirements, generate a complete and professional BDD Gherkin .feature file.
    
    Requirements:
    {requirements}
    
    Rules:
    1. Use standard Gherkin syntax (Feature, Scenario, Given, When, Then, And).
    2. Ensure scenarios cover positive and negative cases if applicable.
    3. Return ONLY the raw content of the .feature file. 
    4. Generate Scenario outlines as applicable
    5. Parameterise the steps for reuse with different data
    6. Do NOT include markdown code fences (```gherkin) or any other conversational text.
    """
    
    
    )

def get_test_case_generation_prompt(requirements: str, template: str, steps_format: str = "single_cell") -> str:
    """
    Returns the prompt for generating formatted test cases from requirements and a template.
    
    # Used in file: BIG_QA_Solution/ScriptGenerator/api/backend.py
    # Used under function: generate_formatted_test_cases
    
    Args:
        requirements: The requirements text.
        template: The template string for formatting the output.
        steps_format: "single_cell" or "separate_rows"
        
    Returns:
        The formatted prompt string.
    """
    if steps_format == "separate_rows":
        return f"""
    You are an expert QA Automation Engineer.
    Your task is to generate and map the provided requirements into formatted test cases where each test step is stored in a separate step object inside a "Steps" array.
    
    Requirements:
    {requirements}
    
    Template / Column Headers:
    {template}
    
    Instructions:
    1. Extract test cases from the Requirements.
    2. Cover all types of scenarios: Positive, Negative, Edge Cases, Boundary conditions, field level validations, Business rule validations, Error handling, Regression impact scenarios.
    3. Return the response as a JSON object with two top-level keys: "test_cases" and "summary".
    4. The value for "summary" MUST be a JSON object containing the following keys:
       - "positive_count": total number of positive test cases/scenarios
       - "negative_count": total number of negative test cases/scenarios
       - "high_priority_count": total number of high priority test cases
       - "medium_priority_count": total number of medium priority test cases
       - "low_priority_count": total number of low priority test cases
    5. The value for "test_cases" MUST be a JSON array of objects.
       Each object in the array represents ONE test case with top-level metadata fields (e.g. ID, Work Item Type, Title, Area Path, Assigned To, State, etc. matching the provided Template/Column Headers excluding step specific columns) AND a nested "Steps" array.
    6. CRITICAL STEP FORMATTING INSTRUCTION:
       - Every test case object in "test_cases" MUST contain a "Steps" key containing a JSON array of step objects.
       - Each step object inside the "Steps" array MUST have the exact following keys:
         - "Test Step": step number integer (1, 2, 3...)
         - "Step Action": step action description string
         - "Step Expected": expected result description string for this specific step
       - Example JSON structure:
         {{
           "test_cases": [
             {{
               "ID": "TC_001",
               "Work Item Type": "Test Case",
               "Title": "Verify successful Lead creation...",
               "Area Path": "Campions Creatio Project",
               "Assigned To": "Sumreet Kaur <SKaur@firstport.co.uk>",
               "State": "Design",
               "Steps": [
                 {{
                   "Test Step": 1,
                   "Step Action": "Launch the application",
                   "Step Expected": "Application should be launched and login screen should be displayed"
                 }},
                 {{
                   "Test Step": 2,
                   "Step Action": "Login to the application and navigate to the lead creation page",
                   "Step Expected": "Lead creation page should be displayed"
                 }},
                 {{
                   "Test Step": 3,
                   "Step Action": "Enter valid data in all fields and click Submit.",
                   "Step Expected": "Lead is saved successfully with status \"New Lead\"."
                 }}
               ]
             }}
           ],
           "summary": {{
             "positive_count": 1,
             "negative_count": 0,
             "high_priority_count": 1,
             "medium_priority_count": 0,
             "low_priority_count": 0
           }}
         }}
    7. Include NO other information. Your entire response must be standard, parseable JSON.
    """
    else:
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
    3. Return the response as a JSON object with two top-level keys: "test_cases" and "summary".
    4. The value for "test_cases" MUST be a JSON array of objects.
       The value for "summary" MUST be a JSON object containing the following keys:
       - "positive_count": total number of positive test cases/scenarios
       - "negative_count": total number of negative test cases/scenarios
       - "high_priority_count": total number of high priority test cases
       - "medium_priority_count": total number of medium priority test cases
       - "low_priority_count": total number of low priority test cases
    5. Each object in the array represents ONE test case.
    6. CRITICAL FATAL INSTRUCTION: The keys in each JSON object in the "test_cases" array MUST STRICTLY be the exact column headers specified in the Template / Sample Format. 
       - You MUST create a distinct, separate JSON key-value pair (node) for EVERY single heading in the provided template.
       - Every column header provided in the Template MUST be a key in every JSON object.
       - Do NOT invent your own keys.
       - NEVER use standard keys like 'Step No', 'Pre-requisite', 'Test Data', 'Action' unless they are explicitly in the template.
       - You MUST map the test cases exactly to whatever keys the user provided in the Template string.
       - Example: if the template provided is ["A", "B", "C"], your JSON must be:
         {{
           "test_cases": [ {{ "A": "...", "B": "...", "C": "..." }} ],
           "summary": {{
             "positive_count": 1,
             "negative_count": 0,
             "high_priority_count": 1,
             "medium_priority_count": 0,
             "low_priority_count": 0
           }}
         }}
       - Always provide steps to be executed with serial number
       - Always generate steps from the beginning of the functional flow  
    7. Include NO other information. Your entire response must be standard, parseable JSON.
    """
