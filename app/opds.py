"""Minimal OPDS 1.2 acquisition feed (for KyBook / MapleRead / Yomu on iOS)."""
from xml.sax.saxutils import escape

MIME = {
    "pdf": "application/pdf", "epub": "application/epub+zip",
    "mobi": "application/x-mobipocket-ebook", "azw3": "application/x-mobipocket-ebook",
    "fb2": "application/fb2+xml", "cbz": "application/vnd.comicbook+zip",
}


def catalog(books, self_url: str) -> str:
    entries = []
    for b in books:
        mime = MIME.get(b["ext"], "application/octet-stream")
        authors = "".join(
            f"<author><name>{escape(a.strip())}</name></author>"
            for a in b["authors"].split(",") if a.strip()
        )
        entries.append(f"""
  <entry>
    <id>urn:book:{b['sha256']}</id>
    <title>{escape(b['title'])}</title>
    {authors}
    <updated>{(b['created_at'] or '').replace(' ', 'T')}Z</updated>
    <summary type="text">{escape((b['description'] or b['categories'])[:500])}</summary>
    <link rel="http://opds-spec.org/image" href="/api/books/{b['id']}/cover" type="image/jpeg"/>
    <link rel="http://opds-spec.org/acquisition" href="/api/books/{b['id']}/file" type="{mime}"/>
  </entry>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog">
  <id>urn:ebook-manager:catalog</id>
  <title>My shelf</title>
  <updated>2026-01-01T00:00:00Z</updated>
  <link rel="self" href="{escape(self_url)}" type="application/atom+xml;profile=opds-catalog"/>
{''.join(entries)}
</feed>"""
