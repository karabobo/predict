import os
import json
import requests
from typing import Dict, Any

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional during lightweight import checks
    def load_dotenv(*_args, **_kwargs):
        return False


def _load_env_fallback(env_path: str) -> None:
    """Minimal .env loader for environments without python-dotenv."""
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass

# 加载 .env 文件
_ENV_PATH = os.path.join(os.path.dirname(__file__), "../.env")
load_dotenv(_ENV_PATH)
_load_env_fallback(_ENV_PATH)

class MultiModelClient:
    def __init__(self):
        # Provider 1: SiliconFlow official OpenAI-compatible endpoint
        self.sf_key = os.getenv("SILICON_FLOW_KEY")
        self.sf_base_url = os.getenv("SILICON_FLOW_BASE_URL", "https://api.siliconflow.com/v1").rstrip("/")
        
        # Provider 2: alternate OpenAI-compatible endpoint
        self.op_key = os.getenv("NEW_PROVIDER_KEY")
        self.op_base_url = os.getenv("NEW_PROVIDER_BASE_URL", "https://sub.jlypx.de/v1").rstrip("/")

    def _choose_provider(self, model_name: str) -> tuple[str | None, str]:
        is_openai_team = "gpt-5" in model_name or "gpt-4" in model_name
        if is_openai_team:
            return self.op_key, f"{self.op_base_url}/chat/completions"
        return self.sf_key, f"{self.sf_base_url}/chat/completions"

    def _extract_json(self, content: str) -> Dict[str, Any]:
        text = content.strip()
        if "```json" in text:
            text = text.split("```json", 1)[1].split("```", 1)[0]
        elif "```" in text:
            text = text.split("```", 1)[1].split("```", 1)[0]
        return json.loads(text.strip())

    def predict(self, model_name: str, system_prompt: str, user_prompt: str, coach_mode: bool = False) -> Dict[str, Any]:
        """Route prediction requests to the configured provider."""
        api_key, url = self._choose_provider(model_name)
        if not api_key:
            return {"error": f"Missing API key for model {model_name}"}

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.7 if coach_mode else 0.3
        }
        
        try:
            timeout = int(os.getenv("MODEL_TIMEOUT_SECONDS", "120"))
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            
            if resp.status_code == 200:
                content = resp.json()['choices'][0]['message']['content']
                return self._extract_json(content)
            else:
                snippet = resp.text[:240].replace("\n", " ")
                return {"error": f"API Error {resp.status_code} from {model_name}: {snippet}"}
        except Exception as e:
            return {"error": str(e)}

client = MultiModelClient()
