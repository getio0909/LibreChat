import json
from typing import Any

import httpx

from .config import StudioRules, StudioSettings

# 概念规划器结构化JSON输出Schema定义
CONCEPT_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "concepts": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "title": {"type": "STRING"},
                    "positioning": {"type": "STRING"},
                    "description": {"type": "STRING"},
                    "materials": {"type": "STRING"},
                    "key_details": {"type": "STRING"},
                    "image_prompt": {"type": "STRING"},
                },
                "required": [
                    "title",
                    "positioning",
                    "description",
                    "materials",
                    "key_details",
                    "image_prompt",
                ],
            },
        }
    },
    "required": ["concepts"],
}


# 组装概念规划器的系统提示词（按请求方案数渲染planner_system、planner_rules与无参考策略）
def build_planner_system_prompt(
    rules: StudioRules, count: int, has_references: bool
) -> str:
    sections = [
        rules.planner_system.replace("{count}", str(count)),
        rules.planner_rules.replace("{count}", str(count)),
    ]
    if not has_references:
        sections.append(rules.no_reference_rules.replace("{count}", str(count)))
    return "\n".join(sections)


# 校验并标准化单个概念方案字段
def _normalize_concept_item(raw_item: Any, index: int) -> dict[str, str]:
    if not isinstance(raw_item, dict):
        raise TypeError(f"Concept item at index {index} must be an object")
    title = str(raw_item.get("title") or "").strip()
    positioning = str(raw_item.get("positioning") or "").strip()
    description = str(raw_item.get("description") or "").strip()
    materials = str(raw_item.get("materials") or "").strip()
    key_details = str(raw_item.get("key_details") or "").strip()
    image_prompt = str(raw_item.get("image_prompt") or "").strip()

    if not all((title, positioning, description, materials, key_details, image_prompt)):
        raise ValueError(f"Concept item at index {index} is missing required fields")
    return {
        "title": title[:200],
        "positioning": positioning[:1800],
        "description": description[:1800],
        "materials": materials[:1800],
        "key_details": key_details[:1800],
        "image_prompt": image_prompt[:3500],
    }


# 调用云端规划接口生成指定数量的结构化中文概念方案（不自动重试）
async def plan_concepts(
    settings: StudioSettings,
    rules: StudioRules,
    brief: str,
    count: int = 3,
    reference_ids: list[str] | None = None,
    reference_data_uris: list[str] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, str]]:
    cleaned_brief = brief.strip() if isinstance(brief, str) else ""
    if not cleaned_brief or len(cleaned_brief) > 4000 or "\x00" in cleaned_brief:
        raise ValueError("Invalid design brief")
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 3:
        raise ValueError("count must be an integer between 1 and 3")

    ref_ids = list(reference_ids or [])
    if len(ref_ids) > 10:
        raise ValueError("At most 10 reference_ids are allowed")

    system_text = build_planner_system_prompt(
        rules, count=count, has_references=bool(ref_ids)
    )
    user_payload = {
        "brief": cleaned_brief,
        "count": count,
        "references": ref_ids,
    }

    parts: list[dict[str, Any]] = [
        {"text": json.dumps(user_payload, ensure_ascii=False)},
    ]
    for uri in reference_data_uris or []:
        if "," in uri and uri.startswith("data:"):
            header, b64_data = uri.split(",", 1)
            mime_type = header.removeprefix("data:").split(";", 1)[0] or "image/jpeg"
            parts.append({"inlineData": {"mimeType": mime_type, "data": b64_data}})

    body: dict[str, Any] = {
        "systemInstruction": {
            "parts": [{"text": system_text}],
        },
        "contents": [
            {
                "role": "user",
                "parts": parts,
            }
        ],
        "generationConfig": {
            "thinkingConfig": {
                "thinkingLevel": settings.planner_thinking_level,
            },
            "responseMimeType": "application/json",
            "responseSchema": CONCEPT_RESPONSE_SCHEMA,
        },
    }

    url = f"{settings.gemini_base_url.rstrip('/')}/models/{settings.planner_model}:generateContent"
    headers = {
        "x-goog-api-key": settings.gemini_api_key.strip(),
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(float(settings.planner_timeout_seconds)),
            follow_redirects=False,
        ) as client:
            response = await client.post(url, json=body, headers=headers)
            raw_bytes = response.content
    except (httpx.TimeoutException, TimeoutError):
        raise RuntimeError(
            "Concept planning timed out; no automatic retry was performed"
        ) from None
    except (httpx.HTTPError, OSError):
        raise RuntimeError(
            "Concept planning service connection failed; no automatic retry was performed"
        ) from None

    if response.status_code != 200:
        raise RuntimeError(
            f"Concept planning request failed with status {response.status_code}; no automatic retry was performed"
        )

    if len(raw_bytes) > 4 * 1024 * 1024:
        raise RuntimeError("Concept planning response exceeded size limit")

    try:
        payload = json.loads(raw_bytes)
        candidates = payload["candidates"]
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("No candidates in planner response")
        candidate_parts = candidates[0]["content"]["parts"]
        if not isinstance(candidate_parts, list) or not candidate_parts:
            raise ValueError("No content parts in planner response")
        non_thought_texts = [
            p["text"]
            for p in candidate_parts
            if isinstance(p, dict)
            and not p.get("thought")
            and isinstance(p.get("text"), str)
        ]
        raw_text = (
            non_thought_texts[-1] if non_thought_texts else candidate_parts[-1]["text"]
        )
        start = raw_text.find("{")
        end = raw_text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Planner output did not contain a JSON object")
        parsed_json = json.loads(raw_text[start : end + 1])
        concepts_raw = parsed_json.get("concepts")
        if not isinstance(concepts_raw, list) or len(concepts_raw) != count:
            raise ValueError(f"Expected {count} concepts from planner")
        return [
            _normalize_concept_item(item, idx) for idx, item in enumerate(concepts_raw)
        ]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Concept planner returned an invalid or incomplete structured response"
        ) from exc
