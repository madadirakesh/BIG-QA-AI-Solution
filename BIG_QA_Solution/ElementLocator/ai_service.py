import json
import requests
import re

class AIService:
    def __init__(self, ai_tool: str, ai_model: str, api_key: str, api_url: str):
        self.ai_tool = ai_tool.upper()
        self.ai_model = ai_model
        self.api_key = api_key
        self.api_url = api_url

    def generate_locators(self, name_hint: str, outer_html: str, tool: str) -> list:
        if not self.api_key or not self.api_key.strip() or not outer_html:
            return []

        try:
            extra_tooling = ""
            if tool.lower() == "playwright":
                extra_tooling = ", getByTestId, getByText, getByRole, getByLabel"

            prompt = (
                f"Analyze this HTML snippet and suggest the best element locators for a '{tool}' automation script. "
                "Return ONLY a JSON array of objects, where each object has 'name', 'type', 'value', and 'rating' fields. "
                "The 'name' should be a descriptive, camelCase element name based on the HTML attributes (e.g., loginButton, emailInput). "
                f"The 'type' should be one of (CSS, XPath, ID, Name, Link Text, Partial Link, Tag Name{extra_tooling}). "
                "The Xpath is relative Xpath. Use Contains, Text, other property attributes as applicable"
                "The 'rating' MUST be exactly one of: 'Best', 'Good', 'Ok', 'Un-Reliable'. "
                f"HTML: \n{outer_html}"
            )

            headers = {"Content-Type": "application/json"}
            payload = {}
            url = self.api_url

            if self.ai_tool in ["GEMINI", "GOOGLE"]:
                url = f"{self.api_url}{self.api_key}"
                payload = {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": 0.0,
                        "response_mime_type": "application/json"
                    }
                }
            elif self.ai_tool in ["OPENAI", "COPILOT"]:
                headers["Authorization"] = f"Bearer {self.api_key}"
                payload = {
                    "model": self.ai_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.0,
                    "response_format": {"type": "json_object"}
                }
            elif self.ai_tool in ["CLAUDE", "ANTHROPIC"]:
                headers["x-api-key"] = self.api_key
                headers["anthropic-version"] = "2023-06-01"
                payload = {
                    "model": self.ai_model,
                    "max_tokens": 2048,
                    "messages": [{"role": "user", "content": prompt}]
                }

            response = requests.post(url, json=payload, headers=headers)
            
            if response.status_code == 200:
                return self._parse_response(name_hint, response.text)
            else:
                print(f"{self.ai_tool} API Error: {response.status_code} - {response.text}")
                return []
        except Exception as e:
            print(f"Error calling {self.ai_tool}: {e}")
            return []

    def _parse_response(self, name_hint: str, response_text: str) -> list:
        result = []
        try:
            data = json.loads(response_text)
            text_content = ""

            # Extract text based on provider
            if self.ai_tool in ["GEMINI", "GOOGLE"]:
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        text_content = parts[0].get("text", "")
            elif self.ai_tool in ["OPENAI", "COPILOT"]:
                choices = data.get("choices", [])
                if choices:
                    text_content = choices[0].get("message", {}).get("content", "")
            elif self.ai_tool in ["CLAUDE", "ANTHROPIC"]:
                content = data.get("content", [])
                if content:
                    text_content = content[0].get("text", "")

            if not text_content:
                return []

            # Clean up markdown code blocks if present
            text_content = re.sub(r'(?s)^```json\s*', '', text_content)
            text_content = re.sub(r'(?s)\s*```$', '', text_content)
            text_content = text_content.strip()

            locs = json.loads(text_content)
            # Support both array or object with array key
            if isinstance(locs, dict):
                # Try to find common keys like 'locators' or 'elements'
                for key in ["locators", "elements", "results"]:
                    if key in locs and isinstance(locs[key], list):
                        locs = locs[key]
                        break
            
            if isinstance(locs, list):
                for obj in locs:
                    n = obj.get("name", name_hint)
                    t = obj.get("type", "XPath")
                    v = obj.get("value", "")
                    r = obj.get("rating", obj.get("category", "Ok"))
                    result.append({
                        "name": n,
                        "type": t,
                        "value": v,
                        "category": r
                    })
        except Exception as e:
            print(f"Error parsing {self.ai_tool} response: {e}")

        return result
