#!/usr/bin/env node
// Behavioral tests for the AI action cards in web/app.js — no DOM, no deps:
// slices the shipped aiActionCard source verbatim and drives it with a
// scripted api() stub. Run: node scripts/test_ai_ui.mjs
import { readFileSync } from "node:fs";

const src = readFileSync(new URL("../web/app.js", import.meta.url), "utf8");
const slice = (from, to) => {
  const a = src.indexOf(from), b = src.indexOf(to, a);
  if (a < 0 || b < 0) throw new Error(`slice not found: ${from}`);
  return src.slice(a, b);
};
const code = slice("const esc = (s)", "\n") + "\n"
  + slice("function aiActionCard(a)", "\nfunction aiMetaCard(a)") + "\n"
  + slice("function aiMetaCard(a)", "/* ---------- reader");

class El {
  constructor(tag) { this.tag = tag; this.children = []; this._html = ""; this.disabled = false; this.textContent = ""; this.type = ""; }
  set className(v) { this._cls = v; } get className() { return this._cls; }
  set innerHTML(v) { this._html = v; this.children = []; } get innerHTML() { return this._html; }
  append(...n) { this.children.push(...n); }
  remove() {} setAttribute() {}
}
const calls = [];
let script = [];
const api = async (path, opts = {}) => {
  calls.push({ path, method: opts.method || "GET", json: opts.json });
  const step = script.shift() ?? { ok: {} };
  if (step.throw) { const e = new Error(step.throw.message); e.status = step.throw.status; throw e; }
  return step.ok;
};
const globals = {
  esc: null, aiActionCard: null,
  document: { createElement: (t) => new El(t), createDocumentFragment: () => new El("#frag") },
  $: () => new El("#err"),
  SOURCES: { zlib: { queue: "/api/zlib/queue" }, annas: { queue: "/api/annas/queue" } },
  api,
  refreshQueue: () => {}, loadCollections: () => {},
};
new Function("G", `with (G) { ${code}\n G.esc = esc; G.aiActionCard = aiActionCard; }`)(globals);
// aiActionCard appends [main, apply, dismiss] (or a fragment for unknown types)
const card = (a) => {
  calls.length = 0;  // per-card call log
  const row = globals.aiActionCard(a);
  const apply = row.children[1];
  const main = row.children[0];
  return { apply, main };
};
let failures = 0;
const check = (name, cond) => { console.log(`[${cond ? "PASS" : "FAIL"}] ${name}`); if (!cond) failures++; };

// 1. queue POST resolves failed → retry → still failed: honest error, no "Queued ✓"
script = [{ ok: { status: "failed", id: 9, error: "quota exhausted" } },
          { ok: { status: "failed", id: 9, error: "quota exhausted" } }];
{
  const { apply, main } = card({ type: "queue", source: "zlibrary", id: "42", name: "Book" });
  await apply.onclick();
  check("instant-fail job shows error, not Queued ✓",
    !main.innerHTML.includes("Queued ✓") && main.innerHTML.includes("quota exhausted"));
  check("failed job was retried once", calls.some((c) => c.path.endsWith("/retry")));
}

// 2. queue POST resolves failed → retry requeues: success state, retry called
script = [{ ok: { status: "failed", id: 9, error: "x" } }, { ok: { status: "queued", id: 9 } }];
{
  const { apply, main } = card({ type: "queue", source: "annas", id: "md5", name: "B" });
  await apply.onclick();
  check("retry-then-queued shows Queued ✓", main.innerHTML.includes("Queued ✓"));
  check("annas card hits the annas queue", calls[0].path === "/api/annas/queue");
}

// 3. collection_create 409 (name exists) → resolve existing id, add books, complete
script = [{ throw: { status: 409, message: "conflict" } },
          { ok: [{ id: 7, name: "X" }] },
          { ok: {} }];
{
  const { apply, main } = card({ type: "collection_create", name: "X", book_ids: [3] });
  await apply.onclick();
  check("409 resolved via GET /api/collections", calls[1].path === "/api/collections" && calls[1].method === "GET");
  check("books filed into the existing id", calls[2].path === "/api/collections/7/books");
  check("done state acknowledges reuse", main.innerHTML.includes("Added to existing ✓"));
}

// 4. collection_create non-409 error still surfaces as a retryable failure
script = [{ throw: { status: 500, message: "boom" } }];
{
  const { apply } = card({ type: "collection_create", name: "Y", book_ids: [] });
  await apply.onclick();
  check("non-409 create error does not apply", calls.length === 1);
}

// 5. remetadata card: apply POSTs with hint, renders before→after title
script = [{ ok: { before: { title: "ebook.pdf" }, after: { title: "Real Book", year: 1999 },
                  changed: ["title", "year"] } }];
{
  const { apply, main } = card({ type: "remetadata", book_id: 3, query: "A - Real Book" });
  await apply.onclick();
  check("remetadata POST carries the query hint",
    calls[0].path === "/api/books/3/remetadata" && calls[0].json.query === "A - Real Book");
  check("remetadata card shows before → after", main.innerHTML.includes("ebook.pdf →")
    && main.innerHTML.includes("Real Book"));
}

// 6. refetch_cover: POST, honest "no better cover" when updated:false
script = [{ ok: { updated: false } }];
{
  const { apply, main } = card({ type: "refetch_cover", book_id: 5 });
  await apply.onclick();
  check("refetch_cover calls the cover endpoint", calls[0].path === "/api/books/5/cover");
  check("no better cover is stated honestly", main.innerHTML.includes("No better cover found"));
}

// 7. update_meta: PATCHes whitelisted fields, shows updated state
script = [{ ok: { title: "New" } }, { ok: {} }];
{
  const { apply, main } = card({ type: "update_meta", book_id: 7, fields: { title: "New" } });
  await new Promise((r) => setTimeout(r, 10));  // diff GET fills async
  await apply.onclick();
  check("update_meta PATCHes the book", calls[1].method === "PATCH"
    && calls[1].path === "/api/books/7" && calls[1].json.title === "New");
  check("update_meta shows updated state", main.innerHTML.includes("Fields updated ✓"));
}

process.exit(failures ? 1 : 0);