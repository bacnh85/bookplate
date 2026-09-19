"""AI metadata fallback via any OpenAI-compatible endpoint (default Z.AI GLM)."""
import json
import re

import httpx

from . import settings


def ai_enabled() -> bool:
    """Gate before ai_extract: explicit off switch wins, then key presence."""
    return settings.get("ai.enabled", "") != "0" and bool(settings.get("ai.api_key"))


async def ai_extract(filename: str, sample_text: str) -> dict | None:
    """Returns {title, author, category} or None. Needs an API key (admin Settings
    or env); ai.enabled='0' is an explicit off switch."""
    if settings.get("ai.enabled", "") == "0":
        return None
    key = settings.get("ai.api_key")
    if not key:
        return None
    base = settings.get("ai.base_url", "https://api.z.ai/api/openai/v1").rstrip("/")
    # default per user choice 2026-09-17; not yet validated against a real key —
    # if the model isn't on the account, responses come back as router 404s; set ai.model
    model = settings.get("ai.model", "glm-5.3-flash")
    text = (sample_text or "")[:2500]
    try:
        async with httpx.AsyncClient(timeout=25) as cx:
            r = await cx.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content":
                            "Extract book metadata. Reply with ONLY a JSON object: "
                            '{"title": str, "author": str, "category": str}'},
                        {"role": "user", "content":
                            f"Filename: {filename}\n\nFirst pages:\n{text or '(no text)'}"},
                    ],
                    "temperature": 0.1,
                },
            )
            content = r.json()["choices"][0]["message"]["content"]
            m = re.search(r"\{.*\}", content, re.S)
            return json.loads(m.group(0)) if m else None
    except Exception:
        return None
