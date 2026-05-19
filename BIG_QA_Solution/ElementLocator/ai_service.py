import json
import requests
import re

class AIService:
    def __init__(self, ai_tool: str, ai_model: str, api_key: str, api_url: str):
        self.ai_tool = (ai_tool or "").upper()
        self.ai_model = ai_model
        self.api_key = api_key
        self.api_url = api_url

    def generate_unique_xpath(self, name_hint: str, outer_html: str, tool: str) -> list:
        """Precision-mode: ask AI to produce ONE unique relative XPath."""
        if not self.api_key or not outer_html:
            return []
        try:
            prompt = (
                f"You are an expert test automation engineer. Analyze this HTML element and generate the MOST STABLE, "
                f"UNIQUE relative XPath that will match EXACTLY ONE element on the page for a {tool} automation script.\n"
                "Rules:\n"
                "- MUST start with // (relative XPath only, never /html/body/...)\n"
                "- Prefer attributes in order: @data-testid, @data-automation-id, @aria-label, @id, @name, @placeholder, @title, @type\n"
                "- Use normalize-space() for text matching to handle whitespace\n"
                "- When combining with 'and', child paths MUST use .// (dot-slash), NEVER // (absolute) inside predicates\n"
                "  WRONG: //a[@id='x' and //span[.='y']]   RIGHT: //a[@id='x' and .//span[.='y']]\n"
                "- Do NOT use positional predicates like [1],[2] unless absolutely necessary\n"
                "- Return exactly 3 candidates ranked Best→Good→Ok as a JSON array with fields: name, type, value, rating\n"
                "  Example: [{\"name\": \"searchBox\", \"type\": \"XPath\", \"value\": \"//input[@name='q']\", \"rating\": \"Best\"}]\n"
                "- NEVER return markdown, text, or explanations. ONLY the raw JSON array.\n"
                f"HTML:\n{outer_html}"
            )

            headers = {"Content-Type": "application/json"}
            url = self.api_url
            payload = {}

            if self.ai_tool in ["GEMINI", "GOOGLE"]:
                url = f"{self.api_url}{self.api_key}"
                payload = {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.0, "response_mime_type": "application/json"}
                }
            elif self.ai_tool in ["OPENAI", "COPILOT"]:
                headers["Authorization"] = f"Bearer {self.api_key}"
                payload = {"model": self.ai_model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.0}
            elif self.ai_tool in ["CLAUDE", "ANTHROPIC"]:
                headers["x-api-key"] = self.api_key
                headers["anthropic-version"] = "2023-06-01"
                payload = {"model": self.ai_model, "max_tokens": 1024, "messages": [{"role": "user", "content": prompt}]}

            response = requests.post(url, json=payload, headers=headers, timeout=20)
            if response.status_code == 200:
                return self._parse_response(name_hint, response.text)
            else:
                error_msg = f"API Error {response.status_code}: {response.text[:150]}"
                print(f"AI unique XPath error: {error_msg}")
                raise Exception(error_msg)
        except Exception as e:
            print(f"AI unique XPath exception: {e}")
            raise Exception(f"AI Service Error: {str(e)}")

    def generate_locators(self, name_hint: str, outer_html: str, tool: str) -> list:
        if not self.api_key or not self.api_key.strip() or not outer_html:
            return []

        try:
            extra_tooling = ""
            if tool.lower() == "playwright":
                extra_tooling = ", getByTestId, getByText, getByRole, getByLabel"

            prompt = (
                f"Analyze this HTML snippet and suggest the best element locators for a '{tool}' automation script.\n"
                "Return ONLY a JSON array of objects, where each object has 'name', 'type', 'value', and 'rating' fields.\n"
                "The 'name' should be a descriptive, camelCase element name based on the HTML attributes (e.g., loginButton, emailInput).\n"
                f"The 'type' should be one of (CSS, XPath, ID, Name, Link Text, Partial Link, Tag Name{extra_tooling}).\n"
                "XPath rules:\n"
                "  - Use relative XPath (starts with //).\n"
                "  - When using 'and' with child element paths inside a predicate, ALWAYS use .// (dot-slash), never // (absolute).\n"
                "  - WRONG: //a[@id='x' and //span[.='y']]   CORRECT: //a[@id='x' and .//span[.='y']]\n"
                "  - Use normalize-space() for text matching, contains() only when exact match impossible.\n"
                "The 'rating' MUST be exactly one of: 'Best', 'Good', 'Ok', 'Un-Reliable'.\n"
                "Example: [{\"name\": \"submitBtn\", \"type\": \"XPath\", \"value\": \"//button[@type='submit']\", \"rating\": \"Best\"}]\n"
                "NEVER return markdown, text, or explanations. ONLY the raw JSON array.\n"
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

            response = requests.post(url, json=payload, headers=headers, timeout=60)
            if response.status_code == 200:
                return self._parse_response(name_hint, response.text)

            print(f"{self.ai_tool} API Error: {response.status_code} - {response.text}")
            return []

        except Exception as e:
            print(f"Error calling {self.ai_tool}: {e}")
            return []

    @staticmethod
    def _sanitise_xpath(value: str) -> str:
        """
        Auto-fix common AI XPath mistakes:
        - 'and //tag' → 'and .//tag'  (absolute path inside predicate)
        - '[ //tag'   → '[.//tag'
        """
        import re as _re
        # Fix: 'and //x' → 'and .//x'
        value = _re.sub(r'\band (//)\b', r'and .\1', value)
        # Fix: '[ //x' → '[.//x'
        value = _re.sub(r'\[ (//)\b', r'[.\1', value)
        return value

    def _parse_response(self, name_hint: str, response_text: str) -> list:
        result = []
        text_content = ""
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

            # Extract the first JSON array or object using regex
            match = re.search(r'(\[.*\]|\{.*\})', text_content, re.DOTALL)
            if match:
                text_content = match.group(1)
            else:
                text_content = text_content.strip()

            locs = json.loads(text_content)

            if isinstance(locs, dict):
                # Try to find common keys like 'locators' or 'elements'
                for key in ["locators", "elements", "results"]:
                    if key in locs and isinstance(locs[key], list):
                        locs = locs[key]
                        break
                # If it's a single locator object
                if isinstance(locs, dict):
                    if "value" in locs or "xpath" in locs.get("type", "").lower() or "xpath" in locs:
                        locs = [locs]
                    elif "selector" in locs:
                        locs = [locs]
            
            if isinstance(locs, list):
                for obj in locs:
                    n = obj.get("name", name_hint)
                    t = obj.get("type", "XPath")
                    v = obj.get("value", obj.get("xpath", obj.get("selector", "")))
                    r = obj.get("rating", obj.get("category", "Ok"))
                    if v:
                        # Auto-sanitise XPath absolute-path-in-predicate bug
                        if t.lower() in ("xpath", "relative xpath"):
                            v = AIService._sanitise_xpath(v)
                        result.append({
                            "name": n,
                            "type": t,
                            "value": v,
                            "category": r
                        })
        except Exception as e:
            print(f"Error parsing {self.ai_tool} response: {e}")

        return result