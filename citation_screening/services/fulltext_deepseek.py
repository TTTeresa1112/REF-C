import json
import os
import re
from typing import Any, Dict

import requests


def check_fulltext_paragraph(claim: Dict[str, Any], paragraph: Dict[str, Any]) -> Dict[str, Any]:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return {"decision": "无法判断", "reason": "未配置 DeepSeek API。", "api_called": False}
    prompt = f"""你是一名谨慎的学术编辑。请判断下面的【全文候选段落】是否能够明确支持【目标引用句】的主要主张。

只能返回 JSON：
{{"decision":"支持|不支持|无法判断","reason":"一句简短中文理由"}}

判定“支持”必须满足：研究对象、干预/暴露、条件和结论方向与目标句一致。仅主题相关或关键词相同不能判定支持。
如果目标句比段落结论更强、研究对象不同或条件不一致，返回“无法判断”或“不支持”。
前后文只用于理解目标句，不是需要该文献支持的额外主张。

【前文】{claim.get('context_before', '')[:2500] or '无'}
【目标引用句】{claim.get('sentence_text', '')[:4500]}
【后文】{claim.get('context_after', '')[:2500] or '无'}
【文献题名】{claim.get('title', '')[:1200]}
【全文章节】{paragraph.get('section', '正文')[:300]}
【全文候选段落】{paragraph.get('text', '')[:9000]}"""
    try:
        base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
        response = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
                "messages": [{"role": "user", "content": prompt}],
                "thinking": {"type": "disabled"},
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
                "stream": False,
            },
            timeout=120,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        match = re.search(r"\{[\s\S]*\}", content)
        data = json.loads(match.group(0) if match else content)
        decision = data.get("decision")
        if decision not in {"支持", "不支持", "无法判断"}:
            raise ValueError("未知全文判断结果")
        return {"decision": decision, "reason": str(data.get("reason", ""))[:500], "api_called": True}
    except (requests.RequestException, KeyError, ValueError, json.JSONDecodeError) as exc:
        return {"decision": "无法判断", "reason": f"全文判断失败：{str(exc)[:160]}", "api_called": True}
