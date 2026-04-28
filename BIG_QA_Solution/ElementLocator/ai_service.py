import json
import requests
import re

class AIService:
    def __init__(self, ai_tool: str, ai_model: str, api_key: str, api_url: str):
        self.ai_tool = (ai_tool or "").upper()
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
                "The XPath is relative XPath. Use Contains, Text, and other attributes as applicable. "
                "The 'rating' MUST be exactly one of: 'Best', 'Good', 'Ok', 'Un-Reliable'. "
                f"HTML:\n{outer_html}"
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

            response = requests.post(url, json=payload, headers=headers, timeout=60)
            print("STATUS:", response.status_code)
            print("RAW RESPONSE:", response.text)

            if response.status_code == 200:
                return self._parse_response(name_hint, response.text)

            print(f"{self.ai_tool} API Error: {response.status_code} - {response.text}")
            return []

        except Exception as e:
            print(f"Error calling {self.ai_tool}: {e}")
            return []

    text_content = ""

    text_content = ""
    def _parse_response(self, name_hint: str, response_text: str) -> list:
        result = []
        try:
            data = json.loads(response_text)

            if self.ai_tool in ["GEMINI", "GOOGLE"]:
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        text_content = parts[0].get("text", "")

            elif self.ai_tool in ["OPENAI", "COPILOT"]:
                choices = data.get("choices", [])
                if choices:
                    message = choices[0].get("message", {})
                    text_content = message.get("content", "") or ""

            elif self.ai_tool in ["CLAUDE", "ANTHROPIC"]:
                content = data.get("content", [])
                if content:
                    text_content = content[0].get("text", "")

            if not text_content:
                return []

            text_content = re.sub(r'(?s)^```json\s*', '', text_content)
            text_content = re.sub(r'(?s)\s*```$', '', text_content)
            text_content = text_content.strip()

            locs = json.loads(text_content)

            if isinstance(locs, dict):
                locs = [locs]
            elif isinstance(locs, list):
                pass
            else:
                return []

            for obj in locs:
                if not isinstance(obj, dict):
                    continue
                result.append({
                    "name": obj.get("name", name_hint),
                    "type": obj.get("type", "XPath"),
                    "value": obj.get("value", ""),
                    "category": obj.get("rating", obj.get("category", "Ok"))
                })

        except Exception as e:
            print(f"Error parsing {self.ai_tool} response: {e}")

        if result:
            print(result[0])
        else:
            print("No data returned")

        print("text_content:", repr(text_content))
        return result