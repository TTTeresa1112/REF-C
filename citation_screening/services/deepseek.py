import json
import os
import re
from typing import Any, Dict

import requests


VALID_RESULTS = {"匹配", "存疑", "领域不符", "引用无关内容"}


def _extract_json(content: str) -> Dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", content)
    candidate = fenced.group(1) if fenced else content
    match = re.search(r"\{[\s\S]*\}", candidate)
    if not match:
        raise ValueError("DeepSeek 未返回 JSON 对象")
    data = json.loads(match.group(0))
    if data.get("result") not in VALID_RESULTS:
        raise ValueError("DeepSeek 返回了未知分类")
    return data


def screen_pair(
    sentence: str,
    metadata: Dict[str, Any],
    context_before: str = "",
    context_after: str = "",
    full_block: str = "",
) -> Dict[str, str]:
    title = metadata.get("title", "")
    abstract = metadata.get("abstract", "")
    if not title and not abstract:
        return {"result": "存疑", "reason": "未能获取文献题名或摘要，信息不足。", "api_called": False}
    if not abstract:
        return {"result": "存疑", "reason": "仅获取到文献题名，缺少摘要，无法可靠判断支持程度。", "api_called": False}

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return {"result": "存疑", "reason": "未配置 DeepSeek API，无法完成内容初筛。", "api_called": False}

    prompt = f"""你是一名谨慎的学术编辑。请判断这篇文献是否大致适合作为【目标引用句】的参考文献。

只能选择以下一种结果：
- 匹配：摘要能够支持正文句子的主要主张。
- 领域不符：文献主题、对象、疾病、材料或研究领域与正文句子明显无关。
- 存疑：信息不足、仅部分支持、条件或对象不同、正文表述强于摘要，或无法可靠判断。
- 引用无关内容：目标句不是需要参考文献支持的正文主张，而是作者姓名、作者单位、机构地址、邮箱、ORCID、通讯作者信息等文头内容；其中的上标或方括号数字是在关联作者与单位，并非参考文献引用。

前后文只用于理解研究对象、条件和代词指向。只判断文献能否支持目标引用句，不要把前后文中的主张算到目标文献头上。
不要因为关键词相同就判定匹配。严格返回 JSON，不要输出其他文字：
{{"result":"匹配|存疑|领域不符|引用无关内容","reason":"一句简短的中文理由"}}

【前文】{context_before[:3000] or '无'}
【目标引用句】{sentence[:5000]}
【后文】{context_after[:3000] or '无'}
【所在段落（用于边界不确定时辅助理解）】{full_block[:6000] or sentence[:5000]}
文献题名：{title[:1500]}
文献摘要：{abstract[:10000]}"""
    try:
        base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
        response = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
                "messages": [{"role": "user", "content": prompt}],
                "thinking": {"type": "disabled"},
                "temperature": 0.1,
                "stream": False,
            },
            timeout=120,
        )
        response.raise_for_status()
        data = _extract_json(response.json()["choices"][0]["message"]["content"])
        return {"result": data["result"], "reason": str(data.get("reason", "")).strip()[:500], "api_called": True}
    except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError) as exc:
        return {"result": "存疑", "reason": f"DeepSeek 判断失败：{str(exc)[:160]}", "api_called": True}
