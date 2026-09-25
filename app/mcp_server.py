"""MCP server (streamable HTTP) mounted at /mcp — token-authed agent access.

External agents authenticate with a per-user API token (`bp_…`, see auth.py).
The ASGI middleware gates unauthenticated requests with 401; each tool resolves
the user from its MCP Context request headers — the session manager runs tools
outside the original request's task context, so a ContextVar set in middleware
would NOT be visible here. Tools therefore run with exactly the ownership
checks of the REST endpoints they wrap.
"""
import asyncio
import hashlib
import inspect

from mcp.server.fastmcp import Context, FastMCP
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from . import db

mcp = FastMCP("bookplate", streamable_http_path="/")


class TokenMiddleware:
    """401 everything without a valid `Authorization: Bearer bp_…` token."""

    def __init__(self, app):
        self.app = app

    def _user(self, scope):
        auth = Headers(scope=scope).get("Authorization", "")
        if not auth.startswith("Bearer bp_"):
            return None
        h = hashlib.sha256(auth[7:].encode()).hexdigest()
        with db.conn() as con:
            return con.execute(
                "SELECT u.* FROM api_tokens t JOIN users u ON u.id=t.user_id "
                "WHERE t.token_hash=?", (h,)).fetchone()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            user = self._user(scope)
            if not user or user["status"] != "active":
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _tool_user(ctx: Context) -> dict:
    """Resolve the token user from the request the tool call arrived on."""
    req = ctx.request_context.request
    auth = req.headers.get("Authorization", "")
    if auth.startswith("Bearer bp_"):
        h = hashlib.sha256(auth[7:].encode()).hexdigest()
        with db.conn() as con:
            row = con.execute(
                "SELECT u.* FROM api_tokens t JOIN users u ON u.id=t.user_id "
                "WHERE t.token_hash=?", (h,)).fetchone()
        if row and row["status"] == "active":
            return dict(row)
    raise PermissionError("no authenticated user for this MCP request")


async def _call(fn, *a, **kw):
    """Route functions are a sync/async mix — await only when awaitable."""
    out = fn(*a, **kw)
    return await out if inspect.isawaitable(out) else out


@mcp.tool()
async def search_library(query: str = "", ctx: Context = None) -> list[dict]:
    """Search the user's visible books (title/authors/categories/ISBN full-text).
    Empty query lists the 200 most recent books (results are capped at 200)."""
    books = await _call(_lazy("list_books"), q=query, user=_tool_user(ctx))
    return books[:200]  # bounded response — list_books itself has no LIMIT


def _lazy(name):
    """Import app.main at call time — main.py imports this module at startup."""
    import importlib
    return getattr(importlib.import_module("app.main"), name)


@mcp.tool()
async def get_book(book_id: int, ctx: Context = None) -> dict:
    """Full metadata for one visible book."""
    return await _call(_lazy("get_book"), book_id=book_id, user=_tool_user(ctx))


@mcp.tool()
async def list_collections(ctx: Context = None) -> list[dict]:
    """The user's collections with book counts."""
    return await _call(_lazy("list_collections"), user=_tool_user(ctx))


@mcp.tool()
async def create_collection(name: str, book_ids: list[int] | None = None,
                            ctx: Context = None) -> dict:
    """Create a named collection and optionally file book ids into it."""
    from .main import BookIdReq, CollectionReq
    user = _tool_user(ctx)
    out = await _call(_lazy("create_collection"), CollectionReq(name=name), user=user)
    for bid in (book_ids or []):
        await _call(_lazy("collection_add_book"), out["id"], BookIdReq(book_id=bid), user=user)
    return out


@mcp.tool()
async def add_to_collection(collection_id: int, book_ids: list[int],
                            ctx: Context = None) -> dict:
    """Add book ids to an existing collection."""
    from .main import BookIdReq
    user = _tool_user(ctx)
    for bid in book_ids:
        await _call(_lazy("collection_add_book"), collection_id, BookIdReq(book_id=bid), user=user)
    return {"ok": True, "added": len(book_ids)}


@mcp.tool()
async def search_store(query: str, source: str = "") -> list[dict]:
    """Search a download store (Z-Library or Anna's Archive) for books to queue.
    source: 'zlibrary', 'annas', or '' for whichever is configured."""
    from . import settings
    from .zlib_accounts import configured
    src = source or ("zlibrary" if configured() else "annas")
    zlib, annas = _lazy("zlib"), _lazy("annas")
    if src == "zlibrary":
        return await zlib.search(query, count=8)
    return await annas.search(query, count=8)


@mcp.tool()
async def queue_book(source: str, id: str, name: str = "", authors: str = "",
                     cover: str = "", extension: str = "", size: str = "",
                     ctx: Context = None) -> dict:
    """Queue a store result for download (fields come from search_store rows)."""
    from .main import RemetaReq  # noqa: F401  (models already imported in main)
    from .main import ZlibQueueReq
    req = ZlibQueueReq(id=id, name=name, authors=authors, cover=cover,
                       extension=extension, size=size)
    fn = _lazy("zlib_enqueue") if source == "zlibrary" else _lazy("annas_enqueue")
    return await _call(fn, req, user=_tool_user(ctx))


@mcp.tool()
async def list_queue(ctx: Context = None) -> list[dict]:
    """The user's download jobs with statuses."""
    return (await _call(_lazy("zlib_queue"), user=_tool_user(ctx)))["jobs"]


@mcp.tool()
async def remetadata(book_id: int, query: str = "", ctx: Context = None) -> dict:
    """Re-run metadata extraction/enrichment for a shelf book (fix junk titles).
    query: optional 'Author - Title' hint. Returns before/after metadata."""
    from .main import RemetaReq
    return await _call(_lazy("remetadata_book"), book_id, RemetaReq(query=query), user=_tool_user(ctx))


@mcp.tool()
async def refetch_cover(book_id: int, ctx: Context = None) -> dict:
    """Re-fetch a book's thumbnail (embedded art / PDF page-1 / enrichment)."""
    return await _call(_lazy("refetch_cover"), book_id, user=_tool_user(ctx))


@mcp.tool()
async def update_book(book_id: int, title: str = "", authors: str = "",
                      categories: str = "", year: int | None = None,
                      description: str = "", language: str = "",
                      isbn: str = "", ctx: Context = None) -> dict:
    """Explicitly set metadata fields on a shelf book. Empty strings are ignored."""
    from .main import BookPatch
    patch = BookPatch(title=title or None, authors=authors or None,
                      categories=categories or None, year=year,
                      description=description or None, language=language or None,
                      isbn=isbn or None)
    return await _call(_lazy("update_book"), book_id, patch, user=_tool_user(ctx))


mcp_app = TokenMiddleware(mcp.streamable_http_app())
