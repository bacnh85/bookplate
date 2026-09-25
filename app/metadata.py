"""Metadata pipeline: embedded metadata -> filename parse -> Google Books/OpenLibrary -> AI fallback."""
import base64
import hashlib
import html
import re
import textwrap
import zipfile
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import escape

import httpx
from pypdf import PdfReader

from . import settings
from .ai import ai_enabled, ai_extract

EXTS = {"pdf", "epub", "mobi", "azw", "azw3", "prc", "fb2", "cbz"}


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


def _u32(b: bytes, off: int) -> int:
    return int.from_bytes(b[off:off + 4], "big") if len(b) >= off + 4 else 0


def _mobi_exth(buf: bytes) -> dict:
    """EXTH + MOBI header subset, numbering ported from web/foliate-js/mobi.js.
    Returns {'version','resourceStart','encoding','items'} — items is
    {type: [bytes, ...]} (repeatable types keep every value)."""
    if buf[16:20] != b"MOBI":
        raise ValueError("not a MOBI header")
    mobi_len, version = _u32(buf, 20), _u32(buf, 36)
    resource_start, exth_flag = _u32(buf, 108), _u32(buf, 128)
    items: dict[int, list[bytes]] = {}
    if exth_flag & 0x40:  # EXTH follows the MOBI header + 16-byte padding
        p = mobi_len + 16
        if buf[p:p + 4] != b"EXTH":
            raise ValueError("missing EXTH header")
        count, p = _u32(buf, p + 8), p + 12
        for _ in range(count):
            if p + 8 > len(buf):
                break
            rtype, rlen = _u32(buf, p), _u32(buf, p + 4)
            if rlen < 8 or p + rlen > len(buf):
                break
            items.setdefault(rtype, []).append(buf[p + 8:p + rlen])
            p += rlen
    return {"version": version, "resourceStart": resource_start,
            "encoding": "utf-8" if _u32(buf, 28) == 65001 else "cp1252",
            "items": items}


def _mobi_text(items: dict, rtype: int, encoding: str) -> str:
    return (items.get(rtype) or [b""])[0].decode(encoding, "ignore").strip()


def _mobi_uint(items: dict, rtype: int) -> int | None:
    """EXTH uint value; None when the record is absent or carries the
    0xFFFFFFFF 'unset' sentinel. Zero is a real value (coverOffset 0 = the
    first resource record)."""
    vals = items.get(rtype)
    if not vals:
        return None
    val = _u32(vals[0], 0)
    return None if val == 0xFFFFFFFF else val


def _image_kind(data: bytes) -> str | None:
    """Browser-displayable image extension from magic bytes, else None."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def from_mobi(path: Path, meta: dict) -> None:
    """MOBI/KF8 (mobi, azw, azw3, prc) metadata + cover from record 0's EXTH.
    Combo MOBI6/KF8 files (typical AZW3) store their real headers at the record
    named by EXTH 121 — read that half for metadata and for the coverOffset.
    The resource BASE, however, stays record 0's `resourceStart` (see below)."""
    raw = path.read_bytes()
    if len(raw) < 78:
        return
    num_records = int.from_bytes(raw[76:78], "big")
    offs = [int.from_bytes(raw[78 + i * 8:82 + i * 8], "big") for i in range(num_records)]

    def record(i: int) -> bytes:
        if not 0 <= i < len(offs):
            raise IndexError(f"record {i} out of range")
        end = offs[i + 1] if i + 1 < len(offs) else len(raw)
        return raw[offs[i]:end]

    hd0 = _mobi_exth(record(0))
    hd = hd0
    if hd0["version"] < 8:
        boundary = _mobi_uint(hd0["items"], 121)
        if boundary is not None and 0 < boundary < num_records:
            hd = _mobi_exth(record(boundary))  # KF8 half of a combo file
    items, enc = hd["items"], hd["encoding"]
    meta["title"] = _mobi_text(items, 503, enc)
    meta["authors"] = ", ".join(v.decode(enc, "ignore").strip()
                                for v in items.get(100, []) if v.strip())
    meta["description"] = re.sub(r"<[^>]+>", "", _mobi_text(items, 103, enc))
    meta["isbn"] = _mobi_text(items, 104, enc).replace("-", "")
    meta["categories"] = ", ".join(v.decode(enc, "ignore").strip()
                                   for v in items.get(105, []) if v.strip())
    date = _mobi_text(items, 106, enc)
    if m := re.search(r"(1[5-9]\d{2}|20\d{2})", date):
        meta["year"] = int(m.group(1))
    # cover: EXTH 201 offset, then 202 thumbnail; the record is a raw image
    # (image records are never PalmDOC-compressed) — magic-check before trusting.
    # The resource base stays record 0's `resourceStart` even when the KF8 half
    # supplied the offset — exactly what web/foliate-js/mobi.js does (combo files
    # lay all images out after both text halves, and rec0 knows the whole layout).
    # ponytail: HUFF/CDIC-compressed covers and DRM-scrambled ones are skipped —
    # enrichment/generated-cover fallback already covers that gap.
    off = next((o for o in (_mobi_uint(items, 201), _mobi_uint(items, 202))
                if o is not None), None)
    if off is not None:
        try:
            data = record(hd0["resourceStart"] + off)
        except IndexError:
            return
        if ext := _image_kind(data):
            meta["cover"], meta["cover_ext"] = data, ext


def from_fb2(path: Path, meta: dict) -> None:
    """FB2 (FictionBook XML): <description> fields + base64 <binary> cover."""
    root = ElementTree.parse(path).getroot()
    for el in root.iter():  # some files declare an FB2 namespace; make tags plain
        if isinstance(el.tag, str) and el.tag.startswith("{"):
            el.tag = el.tag.split("}", 1)[1]
    ti = root.find("./description/title-info")
    if ti is None:
        return
    meta["title"] = (ti.findtext("book-title") or "").strip()
    authors = []
    for a in ti.findall("author"):
        name = " ".join(x for x in ((a.findtext("first-name") or "").strip(),
                                    (a.findtext("last-name") or "").strip()) if x)
        if name:
            authors.append(name)
    meta["authors"] = ", ".join(authors)
    meta["language"] = (ti.findtext("lang") or "").strip()
    meta["categories"] = ", ".join((g.text or "").strip() for g in ti.findall("genre"))
    date_el = ti.find("date")
    date_text = ((date_el.text or "") + " " + (date_el.get("value") or "")) if date_el is not None else ""
    if m := re.search(r"(1[5-9]\d{2}|20\d{2})", date_text):
        meta["year"] = int(m.group(1))
    # publish-info is a SIBLING of title-info, both under description
    meta["isbn"] = (root.findtext("./description/publish-info/isbn") or "").replace("-", "")
    # cover: the <binary> referenced by <coverpage>/<image l:href="#id">, else the
    # first image <binary>; ids are also addressable by the book id
    cover_id = ""
    img = ti.find("./coverpage/image")
    if img is not None:
        cover_id = (img.get("{http://www.w3.org/1999/xlink}href") or img.get("href") or "").lstrip("#")
    binaries = root.findall("binary")
    chosen = next((b for b in binaries if b.get("id") == cover_id), None) if cover_id else None
    if chosen is None:
        chosen = next((b for b in binaries
                       if (b.get("content-type") or "").startswith("image/")), None)
    if chosen is None:
        return
    try:
        data = base64.b64decode("".join((chosen.text or "").split()))
    except Exception:
        return
    if ext := _image_kind(data):
        meta["cover"], meta["cover_ext"] = data, ext


_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".jxl", ".avif", ".svg")


def from_cbz(path: Path, meta: dict) -> None:
    """CBZ: ComicInfo.xml metadata when present, first image as cover."""
    with zipfile.ZipFile(path) as z:
        info = None
        ci = next((n for n in z.namelist() if n.lower().endswith("comicinfo.xml")), None)
        if ci:
            try:
                info = ElementTree.fromstring(z.read(ci))
            except Exception:
                info = None
        names = [n for n in z.namelist()
                 if not n.startswith("__MACOSX/") and n.lower().endswith(_IMAGE_EXTS)]
        if not names:
            return
        names.sort(key=lambda n: [int(t) if t.isdigit() else t.lower()
                                  for t in re.split(r"(\d+)", n)])
        try:
            meta["cover"], meta["cover_ext"] = z.read(names[0]), Path(names[0]).suffix.lstrip(".")
        except (KeyError, OSError):
            pass
    if info is None:
        return
    title = (info.findtext("Title") or "").strip()
    if not title:
        series, number = (info.findtext("Series") or "").strip(), (info.findtext("Number") or "").strip()
        title = f"{series} #{number}" if series and number else series
    if title:
        meta["title"] = html.unescape(title)
    meta["authors"] = ", ".join(x.strip() for x in (info.findtext("Writer") or "").split(",") if x.strip())
    if summary := (info.findtext("Summary") or "").strip():
        meta["description"] = html.unescape(summary)
    if year := (info.findtext("Year") or "").strip():
        meta["year"] = int(year) if year.isdigit() else None
    if genres := (info.findtext("Genre") or "").strip():
        meta["categories"] = genres


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


def cover_color(data: bytes, ext: str | None) -> str | None:
    """Apple-style 3D case colour: the artwork's EDGE colour, so letterbox bars
    and the backing melt invisibly into the art (full-bleed look). None → CSS ink."""
    try:
        ext = (ext or "").lower()  # EPUB item names keep original case ('cover.PNG')
        if ext == "svg":  # generated covers: palette colour is in the markup
            import re as _re
            m = _re.search(r'rect[^>]*fill="(#[0-9a-fA-F]{6})"', data[:512].decode("utf-8", "ignore"))
            if m:
                return m.group(1).lower()
        import pymupdf
        doc = pymupdf.open(stream=data, filetype=ext or "jpg")
        pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(0.15, 0.15))
        s, n, w, h = pix.samples, pix.n, pix.width, pix.height
        bw = max(1, round(min(w, h) * 0.06))  # outer 6% band = the art's edge
        tot = [0, 0, 0]
        cnt = 0
        for y in range(h):
            edge_row = y < bw or y >= h - bw
            for x in range(w):
                if not edge_row and bw <= x < w - bw:
                    continue
                o = (y * w + x) * n
                tot[0] += s[o]; tot[1] += s[o + 1]; tot[2] += s[o + 2]; cnt += 1
        rgb = [c / cnt for c in tot]
        lum = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
        if lum < 48:  # too-dark case swallows the spine shade — lift it
            f = 60 / max(lum, 1)
            rgb = [min(255, c * f) for c in rgb]
        return "#%02x%02x%02x" % tuple(int(c) for c in rgb)
    except Exception:
        return None


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
        elif ext in (".mobi", ".azw", ".azw3", ".prc"):
            from_mobi(path, meta)
        elif ext == ".fb2":
            from_fb2(path, meta)
        elif ext == ".cbz":
            from_cbz(path, meta)
    except Exception:
        pass  # corrupt metadata -> fall through to filename/API/AI
    fn_title, fn_author = from_filename(orig_name)
    if not meta["title"]:
        meta["title"] = fn_title
    if not meta["authors"]:
        meta["authors"] = fn_author
    if meta["title"] and meta["title"] != "Unknown title":
        await enrich(meta)
    if (not meta["title"] or not meta["authors"]) and ai_enabled():
        got = await ai_extract(orig_name, meta["sample_text"]) or {}
        _fill(meta, got)
    if not meta["title"]:
        meta["title"] = "Unknown title"
    meta.pop("sample_text", None)
    return meta
