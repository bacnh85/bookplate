"""AI helpers via any OpenAI-compatible endpoint (default Z.AI GLM):
metadata fallback extraction + the assistant chat (tool loop + actions parser)."""
import json
import re

import httpx

from . import settings


class AIUnavailable(Exception):
    """Endpoint unreachable / rejected the request; maps to HTTP 503."""
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def ai_enabled() -> bool:
    """Gate before ai_extract/ai_chat: explicit off switch wins, then key presence."""
    # whitespace-only key must count as unconfigured (matches _cfg's strip)
    return settings.get("ai.enabled", "") != "0" and bool(settings.get("ai.api_key").strip())


def _cfg() -> tuple[str, str, str]:
    key = settings.get("ai.api_key").strip()  # pasted keys can carry trailing ws
    # z.ai's OpenAI-compatible surface lives under /api/paas/v4 — /api/openai/v1
    # is a router 404 for every request (validated 2026-09-20 against a live key)
    base = settings.get("ai.base_url", "https://api.z.ai/api/paas/v4").rstrip("/")
    # glm-4.6 validated against a live z.ai key 2026-09-20; glm-5.3-flash is not
    # on this account — if a model 404s, set ai.model in Admin → Settings
    model = settings.get("ai.model", "glm-4.6")
    return key, base, model


async def ai_extract(filename: str, sample_text: str) -> dict | None:
    """Returns {title, author, category} or None. Needs an API key (admin Settings
    or env); ai.enabled='0' is an explicit off switch."""
    if settings.get("ai.enabled", "") == "0":
        return None
    if not ai_enabled():
        return None
    key, base, model = _cfg()
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


# ---------- assistant chat ----------

# The single read-only tool: search the configured stores for real rows that map
# 1:1 onto the queue actions. Mutations are NEVER tools — they come back as
# ```actions proposals the user confirms in the UI.
TOOLS = [{
    "type": "function",
    "function": {
        "name": "search_store",
        "description": "Search a configured ebook store (Z-Library or Anna's Archive) "
                       "for real downloadable books. Returns rows: id, name, authors, "
                       "year, extension, size, cover, language, rating.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "title, author, or topic"},
                "source": {"type": "string", "enum": ["zlibrary", "annas"],
                           "description": "which store; omit for the default"},
            },
            "required": ["query"],
        },
    },
}]


async def _complete(cx: httpx.AsyncClient, url: str, headers: dict, model: str,
                    msgs: list[dict], tools: bool) -> dict:
    """One chat/completions call → the assistant message dict."""
    body = {"model": model, "messages": msgs, "temperature": 0.4}
    if tools:
        body["tools"] = TOOLS
    r = await cx.post(url, headers=headers, json=body)
    if r.status_code != 200:
        raise AIUnavailable(f"AI endpoint HTTP {r.status_code}: {(r.text or '')[:300]}",
                            status=r.status_code)
    try:
        return r.json()["choices"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise AIUnavailable(f"AI endpoint returned junk: {(r.text or '')[:200]}") from e


async def _run_tool(search_store, args: dict) -> str:
    """search_store tool execution; every failure becomes a relayable string."""
    try:
        source = args.get("source") if args.get("source") in ("zlibrary", "annas") else None
        rows = await search_store(str(args.get("query") or "")[:200], source)
        if not isinstance(rows, list):
            rows = [str(rows)[:500]]
        return json.dumps(rows[:8])
    except Exception as e:  # adapter errors (unconfigured, quota, mirror down)
        return json.dumps({"error": str(e)[:300]})


def _str(v) -> str:
    return str(v if v is not None else "").strip()


def _ids(v) -> list[int]:
    out = []
    for x in (v if isinstance(v, list) else [])[:50]:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            pass
    return out


def _valid_action(a) -> dict | None:
    """Whitelist + coerce one proposed action; anything odd is dropped."""
    if not isinstance(a, dict):
        return None
    t = a.get("type")
    if t == "queue":
        # source is required: defaulting an unknown id to a store would queue a
        # doomed job that burns a real download attempt
        if not _str(a.get("id")) or a.get("source") not in ("zlibrary", "annas"):
            return None
        return {"type": "queue",
                "source": a.get("source"),
                "id": _str(a.get("id")), "name": _str(a.get("name")),
                "authors": _str(a.get("authors")), "cover": _str(a.get("cover")),
                "extension": _str(a.get("extension")).lower(), "size": _str(a.get("size"))}
    if t == "collection_create":
        if not _str(a.get("name")):
            return None
        return {"type": t, "name": _str(a.get("name")), "book_ids": _ids(a.get("book_ids"))}
    if t == "collection_add":
        try:
            cid = int(a.get("collection_id"))
        except (TypeError, ValueError):
            return None
        return {"type": t, "collection_id": cid, "book_ids": _ids(a.get("book_ids"))}
    if t in ("remetadata", "refetch_cover"):
        try:
            bid = int(a.get("book_id"))
        except (TypeError, ValueError):
            return None
        out = {"type": t, "book_id": bid}
        if t == "remetadata":
            out["query"] = _str(a.get("query"))[:200]
        return out
    if t == "update_meta":
        try:
            bid = int(a.get("book_id"))
        except (TypeError, ValueError):
            return None
        fields = a.get("fields")
        clean: dict = {}
        for k, v in (fields.items() if isinstance(fields, dict) else []):
            if k not in ("title", "authors", "categories", "year", "description",
                         "language", "isbn"):
                continue  # whitelist — the model may not invent columns
            if k == "year":
                try:
                    clean[k] = int(v)
                except (TypeError, ValueError):
                    pass
            elif _str(v):
                clean[k] = _str(v)[:300]
        if not clean:
            return None
        return {"type": t, "book_id": bid, "fields": clean}
    return None


def split_actions(text: str) -> tuple[str, list[dict]]:
    """Extract the last ```actions fenced JSON from a reply; return (clean_text,
    validated_actions). Absent or malformed block ⇒ ([], text unchanged) — never
    an error: prose must survive a model that ignores the contract."""
    last = None
    for last in re.finditer(r"```actions\s*(.*?)\s*```", text, re.S):
        pass
    if last is None:
        return text.strip(), []
    clean = (text[:last.start()] + text[last.end():]).strip()
    try:
        data = json.loads(last.group(1))
    except ValueError:
        return clean, []
    items = data.get("actions") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return clean, []
    return clean, [a for a in (_valid_action(x) for x in items[:20]) if a]


async def ai_chat(system: str, messages: list[dict], search_store) -> dict:
    """One assistant turn: completion loop with the search_store tool (max 3
    rounds), then split trailing ```actions out of the reply. `search_store`
    (injected by main.py) is async (query, source|None) -> list[dict]."""
    if not ai_enabled():
        raise AIUnavailable("AI assist is not configured (Admin → Settings)")
    key, base, model = _cfg()
    headers = {"Authorization": f"Bearer {key}"}
    url = f"{base}/chat/completions"
    msgs = [{"role": "system", "content": system}, *messages]
    try:
        async with httpx.AsyncClient(timeout=90) as cx:
            use_tools = True
            try:
                msg = await _complete(cx, url, headers, model, msgs, True)
            except AIUnavailable as e:
                # endpoints without tool support reject the schema with 400/422
                # (some 404/501); anything else (401/429/5xx) is a real failure —
                # structured status check, not error-text substring matching
                if e.status not in (400, 404, 422, 501):
                    raise
                use_tools = False  # endpoint has no tool-calling — plain chat
                msg = await _complete(cx, url, headers, model, msgs, False)
            rounds = 0
            while use_tools and msg.get("tool_calls") and rounds < 3:
                rounds += 1
                msgs.append({"role": "assistant", "content": msg.get("content"),
                             "tool_calls": msg["tool_calls"]})
                for tc in msg["tool_calls"]:
                    try:
                        args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    out = await _run_tool(search_store, args if isinstance(args, dict) else {})
                    msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                 "content": out})
                msg = await _complete(cx, url, headers, model, msgs, True)
            if use_tools and msg.get("tool_calls"):
                # round budget exhausted mid-tool-chain: force a prose answer
                # (no tools in the schema; a stubborn model may still stall — ceiling)
                msg = await _complete(cx, url, headers, model, msgs, False)
    except httpx.HTTPError as e:
        raise AIUnavailable(f"AI endpoint unreachable: {e}") from e
    reply, actions = split_actions(str(msg.get("content") or ""))
    return {"reply": reply or "(empty reply)", "actions": actions}
