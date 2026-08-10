# Atlas brand assets

Master files. Everything else in the repository derives from these — if you change
the geometry here, update `app/templates/components/brand.html` to match.

| File | Use |
|---|---|
| `atlas-mark.svg` | The mark, full construction. 32px and above. |
| `atlas-mark-compact.svg` | Below 32px. Drops the axes and dashed radius, thickens the ring. |
| `atlas-mark-mono.svg` | One ink. Inherits `currentColor`. Embossing, engraving, stamps. |
| `atlas-logo-horizontal.svg` | Default lockup. Headers, letterheads, wide placements. |
| `atlas-logo-stacked.svg` | Square and portrait placements. Includes the tagline. |

Applied copies live at `app/static/brand/` (favicon, social card) and inline in
`app/templates/components/brand.html`, where the mark inherits theme variables so it
holds on both the dark and the parchment ground.

## The mark

A four-point compass rose on a visible drafting construction. Three decisions carry it:

- **The arms break the ring.** Tips reach 10 units past the outer circle, so the mark
  reads as directional rather than contained — Atlas orients you, it does not enclose you.
- **North is the only coloured arm.** Copper against ivory means the mark always declares
  a heading. A symmetrical four-point star in one colour is a snowflake.
- **The blue point is off-axis.** On the north-east bearing, away from the arms, so it
  reads as a plotted position — the live record — rather than a fifth compass point.

Adjacent arms share their waist points at ±7 from centre on the diagonals. That shared
geometry is what gives the silhouette a continuous edge instead of four separate darts.

## Typography in the lockups

The lockups use **live text**, not outlines, so they stay editable and searchable. That
means they render correctly only where Cormorant Garamond and Inter are available; the
fallback stack keeps the proportions close but is not the brand.

Before distributing a logo outside a controlled environment — a press kit, a partner, a
print vendor — convert the text to paths:

```bash
inkscape atlas-logo-horizontal.svg \
  --export-text-to-path \
  --export-filename=atlas-logo-horizontal-outlined.svg
```

Raster exports, when a vendor insists:

```bash
# 1024px PNG on transparent ground
inkscape atlas-mark.svg -w 1024 -h 1024 -o atlas-mark-1024.png

# Favicon set from the compact variant
for size in 16 32 48 180 512; do
  inkscape atlas-mark-compact.svg -w $size -h $size -o favicon-$size.png
done
```

SVG is the source of truth. Do not edit a raster export and treat it as a master.

## Minimum sizes and clear space

- **Mark:** 16px minimum, using the compact variant below 32px.
- **Horizontal lockup:** 120px wide minimum. Below that, use the mark alone.
- **Clear space:** the ring radius (one third of the mark's height) on every side. Nothing
  — no text, no rule, no edge of a card — enters it.

## Misuse

Do not recolour the arms, rotate the mark, stretch either axis independently, add effects,
place the full-construction mark on a busy photograph, or reproduce the lockup with text
that is not the brand typefaces. If a surface cannot carry the palette, use
`atlas-mark-mono.svg`; that is what it exists for.

Full specification, including colour roles and measured contrast: [`docs/BRAND.md`](../docs/BRAND.md).
