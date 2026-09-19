"""Metadata pipeline: embedded metadata -> filename parse -> Google Books/OpenLibrary -> AI fallback."""
import hashlib
import os
import re
import textwrap
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
from pypdf import PdfReader

from .ai import ai_extract

EXTS = {"pdf", "epub", "mobi", "azw3", "fb2", "cbz"}


def blank() -> dict:
    return {
        "title": "", "authors": "", "isbn": "", "language": "",
        "categories": "", "description": "", "year": None,
        "cover": None, "cover_ext": None, "sample_text": "",
    }


def from_epub(path: Path, meta: dict) -> None:
    import ebooklib
    from ebooklib import epub

    book = epub.read_epub(str(path))
    dc = lambda k: (book.get_metadata("DC", k) or [("","")])[0][0] or ""
    meta["title"] = dc("title").strip()
    meta["authors"] = ", ".join(a[0] for a in book.get_metadata("DC", "creator"))
    meta["language"] = dc("language").strip()
    meta["description"] = re.sub(r"<[^>]+>", "", dc("description")).strip()
    meta["categories"] = ", ".join(s[0] for s in book.get_metadata("DC", "subject"))
    for val, attrs in book.get_metadata("DC", "identifier"):
        v = str(val).replace("urn:isbn:", "").replace("-", "").strip()
        scheme = attrs.get("scheme", "").lower() if isinstance(attrs, dict) else ""
        if "isbn" in scheme or (v.isdigit() and len(v) in (10, 13)):
            meta["isbn"] = v
            break
    # cover: <meta name="cover" content="id"> or an image named *cover*
    item = None
    for _name, m in book.get_metadata("OPF", "meta"):
        if m.get("name") == "cover":
            item = book.get_item_with_id(m.get("content"))
            break
    if item is None:
        for img in book.get_items_of_type(ebooklib.ITEM_IMAGE):
            if "cover" in img.file_name.lower():
                item = img
                break
    if item is not None:
        meta["cover"] = item.get_content()
        meta["cover_ext"] = Path(item.file_name).suffix.lstrip(".") or "jpg"


def _render_pdf_page(path: Path) -> tuple[bytes, str] | None:
    """Rasterize page 1 to a JPEG — the guaranteed-correct thumbnail. None when
    the renderer fails (encrypted, corrupt, missing page)."""
    try:
        import pymupdf
        doc = pymupdf.open(path)
        try:
            if doc.needs_pass or not doc.page_count:
                return None
            page = doc[0]
            scale = min(480 / page.rect.width, 4) if page.rect.width else 1
            pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            return pix.tobytes("jpeg"), "jpg"
        finally:
            doc.close()
    except Exception:
        return None


def from_pdf(path: Path, meta: dict) -> None:
    reader = PdfReader(str(path))
    m = reader.metadata
    if m:
        meta["title"] = (m.title or "").strip()
        meta["authors"] = (m.author or "").strip()
    # 1. rendered page 1 — always correct (it IS the first page); 2. embedded
    # browser-displayable image as fallback when the renderer fails
    got = _render_pdf_page(path)
    if got:
        meta["cover"], meta["cover_ext"] = got
    else:
        try:
            for img in reader.pages[0].images:
                if img.data[:3] == b"\xff\xd8\xff":
                    meta["cover"], meta["cover_ext"] = img.data, "jpg"
                    break
                if img.data[:8] == b"\x89PNG\r\n\x1a\n":
                    meta["cover"], meta["cover_ext"] = img.data, "png"
                    break
        except Exception:
            pass  # JPX/CCITT etc. don't render in <img> — let enrichment try instead
    try:
        meta["sample_text"] = "".join(
            (p.extract_text() or "") for p in reader.pages[:3]
        )[:4000]
    except Exception:
        pass


def from_filename(name: str) -> tuple[str, str]:
    stem = Path(name).stem.replace("_", " ").strip()
    if " - " in stem:
        left, right = stem.split(" - ", 1)
        return right.strip(), left.strip()  # "Author - Title" convention
    if "." in stem and " " not in stem:
        stem = stem.replace(".", " ")
    return stem.strip(), ""


async def _google(title: str, author: str) -> dict | None:
    q = f'intitle:"{title}"'
    if author:
        q += f' inauthor:"{author}"'
    try:
        async with httpx.AsyncClient(timeout=8) as cx:
            r = await cx.get("https://www.googleapis.com/books/v1/volumes",
                             params={"q": q, "maxResults": 3})
            items = r.json().get("items")
            if not items:
                return None
            vi = items[0].get("volumeInfo", {})
            isbn = next((i["identifier"] for i in vi.get("industryIdentifiers", [])
                         if i["type"] == "ISBN_13"), "")
            return {
                "title": vi.get("title", ""), "authors": ", ".join(vi.get("authors", [])),
                "categories": ", ".join(vi.get("categories", [])),
                "description": vi.get("description", ""),
                "year": int(vi.get("publishedDate", "0")[:4] or 0) or None,
                "isbn": isbn,
                "cover_url": vi.get("imageLinks", {}).get("thumbnail"),
            }
    except Exception:
        return None


async def _openlibrary(title: str, author: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as cx:
            r = await cx.get("https://openlibrary.org/search.json",
                             params={"q": f"{title} {author}".strip(), "limit": 1})
            docs = r.json().get("docs")
            if not docs:
                return None
            d = docs[0]
            return {
                "title": d.get("title", ""), "authors": ", ".join(d.get("author_name", [])[:3]),
                "categories": ", ".join((d.get("subject") or [])[:8]),
                "year": d.get("first_publish_year"),
                "isbn": (d.get("isbn") or [""])[0],
                "cover_url": (f"https://covers.openlibrary.org/b/id/{d['cover_i']}-L.jpg"
                              if d.get("cover_i") else None),
            }
    except Exception:
        return None


def _fill(meta: dict, got: dict) -> None:
    for k in ("title", "authors", "isbn", "language", "categories", "description"):
        if not meta.get(k) and got.get(k):
            meta[k] = str(got[k]).strip()
    if meta.get("year") is None and got.get("year"):
        meta["year"] = got["year"]


def _incomplete(meta: dict) -> bool:
    return any(not meta.get(k) for k in ("authors", "categories", "description", "year"))


async def enrich(meta: dict) -> None:
    """Fill missing fields from Google Books, then OpenLibrary; fetch a cover if needed."""
    got = await _google(meta["title"], meta["authors"]) or {}
    _fill(meta, got)
    cover_url = got.get("cover_url")
    if _incomplete(meta) or meta.get("cover") is None:
        got2 = await _openlibrary(meta["title"], meta["authors"]) or {}
        _fill(meta, got2)
        cover_url = cover_url or got2.get("cover_url")
    if meta.get("cover") is None and cover_url:
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as cx:
                r = await cx.get(cover_url)
                if r.status_code == 200 and r.content[:100]:
                    meta["cover"] = r.content
                    meta["cover_ext"] = cover_url.rsplit(".", 1)[-1].lower() or "jpg"
        except Exception:
            pass


def generated_cover(title: str, authors: str, seed: str) -> bytes:
    """Deterministic SVG placeholder cover (DESIGN.md palette) — last resort so
    every book has a thumbnail. Stored with cover_ext='svg'."""
    h = int(hashlib.sha256((seed or title or "?").encode()).hexdigest(), 16)
    bg = ("#7C2D2D", "#35513D", "#26221C", "#8A6A2F", "#9C4A2F", "#5A5A44")[h % 6]
    ink = "#F6F3EC"
    lines = textwrap.wrap(title or "Untitled", width=18, max_lines=5, placeholder="…")
    parts = []
    y = 300 - (len(lines) - 1) * 28
    for ln in lines:
        parts.append(f'<text x="48" y="{y}" font-size="46" font-weight="600" '
                     f'fill="{ink}">{escape(ln)}</text>')
        y += 56
    for i, ln in enumerate(textwrap.wrap(authors or "", width=32, max_lines=2, placeholder="…")):
        parts.append(f'<text x="48" y="{624 + i * 28}" font-size="22" fill="{ink}" '
                     f'fill-opacity="0.85">{escape(ln)}</text>')
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="480" height="720" viewBox="0 0 480 720">'
           f'<rect width="480" height="720" fill="{bg}"/>'
           f'<rect x="28" y="28" width="424" height="664" fill="none" stroke="{ink}" stroke-opacity="0.4"/>'
           f'<g font-family="Georgia, serif">{"".join(parts)}</g></svg>')
    return svg.encode()


async def build_metadata(path: Path, orig_name: str) -> dict:
    meta = blank()
    ext = path.suffix.lower()
    try:
        if ext == ".epub":
            from_epub(path, meta)
        elif ext == ".pdf":
            from_pdf(path, meta)
    except Exception:
        pass  # corrupt metadata -> fall through to filename/API/AI
    fn_title, fn_author = from_filename(orig_name)
    if not meta["title"]:
        meta["title"] = fn_title
    if not meta["authors"]:
        meta["authors"] = fn_author
    if meta["title"] and meta["title"] != "Unknown title":
        await enrich(meta)
    if (not meta["title"] or not meta["authors"]) and os.getenv("ZAI_API_KEY"):
        got = await ai_extract(orig_name, meta["sample_text"]) or {}
        _fill(meta, got)
    if not meta["title"]:
        meta["title"] = "Unknown title"
    meta.pop("sample_text", None)
    return meta
