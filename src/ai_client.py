import os
import json
import requests
from typing import Dict, Any
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv(os.path.join(os.path.dirname(__file__), '../.env'))

class MultiModelClient:
    def __init__(self):
        # 供应商 1: 硅基流动
        self.sf_key = os.getenv("SILICON_FLOW_KEY")
        self.sf_url = "https://api.siliconflow.cn/v1/chat/completions"
        
        # 供应商 2: 新 OpenAI 兼容端
        self.op_key = os.getenv("NEW_PROVIDER_KEY")
        self.op_url = "https://sub.jlypx.de/v1/chat/completions"

    def predict(self, model_name: str, system_prompt: str, user_prompt: str, coach_mode: bool = False) -> Dict[str, Any]:
        """根据团队归属选择供应商"""
        
        # 识别所属团队
        is_openai_team = "gpt-5" in model_name or "gpt-4" in model_name
        
        api_key = self.op_key if is_openai_team else self.sf_key
        base_url = self.op_url if is_openai_team else self.sf_url

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
            # 进一步增加超时时间，支持大模型深度思考
            timeout = 120 if coach_mode else 120
            resp = requests.post(base_url, headers=headers, json=payload, timeout=timeout)
            
            if resp.status_code == 200:
                content = resp.json()['choices'][0]['message']['content']
                # 处理可能带 Markdown 格式的响应
                if '```json' in content:
                    content = content.split('```json')[1].split('```')[0]
                elif '```' in content:
                    content = content.split('```')[1].split('```')[0]
                return json.loads(content.strip())
            else:
                return {"error": f"API Error {resp.status_code} from {model_name}"}
        except Exception as e:
            return {"error": str(e)}

client = MultiModelClient()
