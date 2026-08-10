# Atlas brand

The system, with the reasoning and the measurements. Assets live in
[`brand/`](../brand/README.md).

---

## Positioning

**Atlas — Property Operations.**
*Property intelligence, made operational.*

Atlas orients. The category is crowded with products that do leasing, accounting,
and maintenance; what they rarely do is agree on one record. The brand has to say
**fixed reference point**, not *innovation*, not *simplicity*. Operators do not
want to be delighted at 2am. They want to know where they are.

Three words carry the product story, and they carry the brand too:

| | |
|---|---|
| **Orient** | One governed record: portfolio, property, unit, lease, owner, vendor, resident. |
| **Coordinate** | Work orders, approvals, invoices, and notices connected to real context. |
| **Account** | Every action reviewable, permissioned, and auditable. |

## Naming

- **Atlas** on its own in running text, once context is established.
- **Atlas — Property Operations** for the formal lockup and first mention.
- **Atlas PMOS** only in engineering contexts: the package name, the repository, the
  API title. It is an internal designation, not a customer-facing name.

Never "the Atlas platform", "Atlas™", or "ATLAS" in body text. The all-caps form
belongs to the wordmark alone.

---

## The mark

A four-point compass rose on a visible drafting construction: outer ring, dashed
working radius, axes, and a plotted coordinate.

Why it is drawn this way:

- **The arms break the ring.** Tips extend 10 units past the circle, so the mark reads
  as directional rather than contained.
- **Only north is coloured.** Copper against ivory means the mark always declares a
  heading. A symmetrical four-point star in a single colour reads as a snowflake.
- **The blue point sits off-axis**, on the north-east bearing, away from the arms — a
  plotted position rather than a fifth compass point. It is the live record.
- **The construction stays visible.** Axes and a dashed radius are the language of a
  survey drawing, not a logo. Atlas is an instrument.

Adjacent arms share their waist points at ±7 from centre on the diagonals; that shared
geometry is what gives the silhouette a continuous edge.

**Variants.** Full construction at 32px and above; compact (ring, rose, fix) below;
mono (`currentColor`) wherever the palette is unavailable.

**Clear space** is one ring radius on every side. **Minimum sizes**: mark 16px,
horizontal lockup 120px wide.

---

## Colour

Values are fixed. What matters more is the **role** each one may play, because two of
them cannot legally carry body text.

### Ground and ink

| Token | Hex | Role |
|---|---|---|
| `--ink` | `#17181A` | Primary ground |
| `--ink-deep` | `#111214` | Recessed ground: hero, sidebar, code |
| `--surface` | `#232427` | Raised panels and cards |
| `--cream` | `#F3EEE5` | Primary text on dark; the parchment ground in light mode |
| `--cream-mute` | `#D8D1C7` | Secondary text |
| `--cream-dim` | `#A7A09A` | Labels, metadata, disabled |

### Accent

| Token | Hex | Role |
|---|---|---|
| `--copper` | `#A66345` | Brand accent. Large text, rules, fills, the north arm |
| `--copper-lit` | `#C07D5B` | Interactive text: links, hover |
| `--blue` | `#4867A9` | The coordinate. Focus rings, live-state marks. **Non-text only** |
| `--blue-lit` | `#6A86C4` | Interactive text where blue is required |
| `--parchment` | `#E4D6C2` | Warm neutral for large surfaces and print |

### Measured contrast

Against `--ink` `#17181A`, computed to WCAG 2.1:

| Colour | Ratio | Verdict |
|---|---|---|
| `--cream` `#F3EEE5` | **15.37** | AAA — body text |
| `--parchment` `#E4D6C2` | **12.43** | AAA |
| `--cream-mute` `#D8D1C7` | **11.73** | AAA |
| `--cream-dim` `#A7A09A` | **6.88** | AA — labels and metadata |
| `--warn` `#C2913F` | **6.27** | AA |
| `--good` `#5C9B74` | **5.41** | AA |
| `--copper-lit` `#C07D5B` | **5.35** | AA — the correct link colour |
| `--blue-lit` `#6A86C4` | **4.92** | AA |
| `--bad` `#B8574A` | **3.81** | AA-large only |
| `--copper` `#A66345` | **3.80** | AA-large only |
| `--blue` `#4867A9` | **3.21** | AA-large only |

**The rules that follow from those numbers, not from taste:**

1. `--copper` never carries body text on dark. Headings at 24px and above, rules,
   fills, and icons only. For a link or any text below 24px, use `--copper-lit`.
2. `--blue` is not a text colour on dark at any size below 24px. It is the coordinate,
   the focus ring, and the live-state tick.
3. `--bad` at 3.81 is for status pills and large numerals. Error *prose* uses `--cream`
   with a `--bad` border or icon carrying the meaning — never colour alone, which also
   fails for colour-blind readers.

In light mode the copper darkens to `#8E5138` (5.37 AA) and the blue to `#3D5993`
(5.96 AA). The dark values do not survive on parchment; substituting them is the most
likely accessibility regression in this system.

### Semantic

`--good` `#5C9B74` · `--warn` `#C2913F` · `--bad` `#B8574A` · `--critical` `#CF4A3C`

Status is never carried by colour alone. Every pill has a label.

---

## Typography

| Role | Face | Notes |
|---|---|---|
| Display | **Cormorant Garamond** | Headings, the wordmark, metric numerals |
| Interface | **Inter** | Everything else |
| Mono | System UI mono stack | Identifiers, hashes, code, tabular figures |

Both brand faces degrade to a documented local stack rather than being fetched: the
application's Content Security Policy forbids third-party font hosts, and a page that
silently drops to Times is worse than one designed for its fallback.

### Scale

A 1.25 ratio from a 15px interface base.

| Token | Size | Use |
|---|---|---|
| Display | `clamp(2.2rem, 6vw, 3.4rem)` | Hero, serif |
| H1 | 2rem | Page title, serif |
| H2 | 1.4rem | Section, serif |
| H3 | 1.1rem | Panel title, serif |
| Body | 0.95–1rem | Interface sans |
| Small | 0.85rem | Secondary |
| Label | 0.66–0.72rem | Uppercase, `0.16em` tracking |
| Metric | 2rem | Serif, tabular numerals |

**Tracking.** The wordmark is `0.34em`. Uppercase labels are `0.16em`–`0.24em`.
Lowercase running text is never tracked.

**Numerals are tabular** everywhere a figure appears in a column. A ledger whose digits
do not align is a ledger nobody can scan.

---

## Voice

Atlas writes the way its documentation and its error messages already do.

**Plain, precise, and it gives reasons.** Say what happened and what to do. When a
constraint exists, say why it exists — the reason is usually the most useful part.

**Specific over impressive.** "Debits and credits must agree" beats "enterprise-grade
financial integrity". Concrete numbers beat adjectives.

**Honest about limits.** `docs/FEATURES.md` grades every capability Complete, Partial,
Modelled, or Seam. A roadmap that reads as a feature list is how a buyer discovers the
gap during implementation.

**Calm.** No exclamation marks. No "Oops!". Someone reading an error is already having
a bad day.

| Instead of | Write |
|---|---|
| "Oops! Something went wrong." | "That reference could not be found. Check the identifier and try again." |
| "Powerful, intuitive accounting" | "Double-entry, with the balance invariant enforced by the database." |
| "Blazing fast" | "P95 under 300ms for common reads." |
| "Revolutionary AI-powered insights" | "Suggestions, with a human approval step and the reasoning shown." |

Sentence case for headings and buttons. Serial commas. Spell out a number under ten
unless it is a measurement.

---

## Layout and motion

**Hairlines, not boxes.** Separation comes from a 1px rule at low opacity. The brand is
drafting table, not dashboard chrome.

**The blue tick.** A short blue segment interrupting a hairline rule is the one recurring
ornament — it echoes the coordinate in the mark. Used once per view, at most.

**Spacing** is a 4px scale: 4, 8, 12, 16, 24, 32, 48, 64.

**Motion is restrained.** 120–200ms, ease-out, on state changes only. Nothing animates on
load. `prefers-reduced-motion` disables all of it.

---

## Applying it

- Design tokens: [`app/static/css/atlas.css`](../app/static/css/atlas.css) — the `:root`
  block is the single source for the values above.
- Mark and lockups in the product:
  [`app/templates/components/brand.html`](../app/templates/components/brand.html).
- Master assets and export commands: [`brand/README.md`](../brand/README.md).
- Live specimen: [the brand page](https://ibuilder.github.io/atlas/brand.html).
