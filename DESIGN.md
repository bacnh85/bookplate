# DESIGN.md — Ebook Manager ("Shelf")

```yaml
colors:
  paper: "#F6F3EC"       # page background, warm
  card: "#FFFDF8"        # book cards, header surface
  ink: "#26221C"         # primary text
  ink-soft: "#5C5648"    # secondary text (APCA Lc>=75 on paper)
  line: "#E3DDD0"        # hairline rules
  accent: "#7C2D2D"      # oxblood — CTAs, links, active marks
  accent-ink: "#FFFDF8"  # text on accent
  danger: "#8A3B3B"
typography:
  display: "Georgia, 'Palatino Linotype', Palatino, serif"
  body: "system-ui, -apple-system, 'Segoe UI', sans-serif"
  scale: [0.8125rem, 0.875rem, 1rem, 1.25rem, 1.625rem, 2.5rem]  # sm base lg xl display
rounded: {sm: 4px, md: 8px}
spacing: 8px grid — 4, 8, 16, 24, 32, 48
elevation:
  none: none
  xs: 0 1px 2px rgba(38,34,28,0.05)          # books at rest — whisper shadow
  sm: 0 1px 3px rgba(38,34,28,0.08)          # cards
  md: 0 4px 16px rgba(38,34,28,0.12)         # dialog, book hover lift
components:
  button: primary(accent)/ghost(ink on paper)/danger; states default/hover(-dk)/active/translate/focus-visible 2px accent outline offset 2/disabled 40%
  input: 1px line, focus accent border; on card surface
  card: bg card, elevation-sm, radius md, hover raise to md + translateY(-2px)
  badge: ext label, 1px line, small caps
  book3d: cover as a physical book, Apple-style — art FULL-BLEED (object-fit contain), case = per-tile `--cover-c` from books.cover_color = the artwork's EDGE-band colour (6% outer frame average) so bars/backing melt into the art; ::before 16px translucent ink-gradient spine overlay (art shows through) + ridge light; no page block; asymmetric radius 2px/6px; hover = existing card raise
  tile-meta: row under cover — progress text left (ink-soft, tabular-nums: "57%" / "Finished" / "New" badge in accent small caps), ghost ⋯ menu right (borderless, ink-soft, hover card bg, focus-visible 2px accent outline offset 2)
do:
  - serif display for wordmark, book titles, headings
  - hairline rules instead of boxy borders
dont:
  - no glassmorphism, no gradients, no glow
  - no blue/purple, no pure #fff/#000
```

Direction: a reading room, not an admin panel — warm paper, ink, oxblood cloth accent.
Signature: the shelf — cover tiles with serif titles, hairline-ruled like a catalogue.
