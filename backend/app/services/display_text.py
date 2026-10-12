"""Bounded JSON copy generation, independent of resource-specific business logic."""

import json

import httpx

from app.config import settings


def generate_json(instructions, payload, model="ali-qwen3.8-flash"):
    if not settings.AI_GATEWAY_API_KEY:
        raise RuntimeError("Display generation credentials unavailable")
    with httpx.Client(timeout=45.0) as client:
        response = client.post(f"{settings.AI_GATEWAY_BASE_URL.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.AI_GATEWAY_API_KEY}"},
            json={"model": model, "temperature": 0.2, "max_tokens": 2500,
                  "messages": [{"role": "system", "content": instructions},
                               {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]})
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(content)
