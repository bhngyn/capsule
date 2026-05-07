# Design language — Capsule

This is the canonical reference for how Capsule looks, feels, and behaves. Read it before adding screens, components, or styles. When code disagrees with this document, change the code.

---

## 0. The thesis

Capsule is used by investigators in four+ languages — including right-to-left Arabic — under varying levels of stress, sometimes against deadlines that will be measured against the integrity of the captured artifacts. The interface has to do three things at once:

1. **Stay legible across scripts and locales.** Words shift width, direction, and shape. Visuals don't.
2. **Communicate state at a glance.** Is my last capture intact? What mode am I in? Is something waiting on me?
3. **Disappear.** The investigator's attention belongs to their case, not to our chrome.

We achieve this by leading with **visuals** (icons, color, shape, illustrations, thumbnails) and using **words as captions, not as the primary information channel.** Every translatable string is a liability when the translation lags or disagrees with the source; every icon is an asset that survives unchanged across all locales.

The aesthetic target sits between **Linear** (calm, considered, dark-first), **Notion** (structured, friendly), and **Stripe Dashboard** (information-dense without being cramped) — but with stronger preservation/seal/witness imagery and deliberate cultural neutrality.

---

## 1. Brand identity

### Name and tagline

**Capsule** — *Capture the web, with proof.*

Capsule reads in any of our target languages without baggage. It evokes preservation, containment, and time-capsule permanence without leaning on legal-or-courtroom imagery that would feel heavy in casual journalism contexts.

### Logo: bell jar over a browser-window specimen

A glass cloche on a plinth, with a small browser-window specimen preserved underneath:

```
       ●           ← finial
      ╱─╲
     ╱   ╲         ← bell-jar dome (straight sides, rounded shoulders)
    │ ┌─┐ │        ← browser-window specimen with a 3-dot title bar
    │ │·│ │
    └─────┘        ← plinth
```

The metaphor — a museum cloche preserving a moment of the live web — is the brand. We deliberately avoid a rounded-rectangle silhouette (button-like) and avoid leaning on the literal name twice (capsule + seal). The form is volumetric, organic, museum-coded.

**Color contract.** The mark is one neutral graphite ink (`zinc-300` on dark, `zinc-700` on light). A single accent dot inside the window's title bar uses the app accent (`teal-600`). This satisfies §2's "icon + color + shape, never color alone" rule: even with the dot stripped, the bell-jar silhouette and the browser-window inclusion still read.

**Glass highlight.** A thin diagonal stroke on the upper-left of the dome implies a light source. Under RTL it mirrors to the upper-right (light-source direction tracks reading direction); the rest of the mark is symmetric and never flips.

**Variants** (live in `app/static/icons/brand/`):

| File                | Use                                              | Notes                                                                 |
|---------------------|--------------------------------------------------|-----------------------------------------------------------------------|
| `logo.svg`          | Primary asset; embed via `<img>` or inline       | Includes glass highlight; uses `currentColor` + `rgb(var(--accent))`  |
| `logo-mono.svg`     | PDF case reports, anything printed/embossed      | Single ink, no highlight, no accent — print-safe on white             |
| `logo-favicon.svg`  | Browser tab favicon                              | 16-px-tuned silhouette: drops finial, dots, highlight; bumps strokes  |
| `logomark.svg`      | README / external surfaces with the wordmark     | Mark + "Capsule" in Inter SemiBold; adapts to OS theme via media query |

For the app header we **inline** the SVG directly into `index.html` so the title-bar accent dot picks up `rgb(var(--accent))` live as the user toggles modes — no asset reload, no file path. The standalone file is the canonical spec; the inline copy is the runtime instance.

**Never gradient.** Flat strokes and a single subtle plinth fill. The brand-mark exception in §11 (gradient permitted on the brand fill at large sizes) is consciously not exercised here — the bell jar reads better as line art at every size.

App icon (when packaged for OS docks): the same mark on an accent-tinted, rounded-2xl square background.

### Voice (when words are unavoidable)

- Direct, calm, unhurried. Verbs over nouns. No exclamation points.
- "Capture this link" beats "Submit URL for processing."
- Error messages own the problem ("We couldn't reach this site") rather than blaming the user.
- Never use "just" ("just click here") — it implies the user should already know.

---

## 2. Color

The palette is intentionally narrow. The base is monochrome neutral; the accent does the work.

### Base (light)
| Role               | Token             | Hex     |
|--------------------|-------------------|---------|
| Page background    | `bg-zinc-50`      | #FAFAFA |
| Surface (card)     | `bg-white`        | #FFFFFF |
| Surface (elevated) | `bg-zinc-50`      | #FAFAFA |
| Border             | `border-zinc-200` | #E4E4E7 |
| Body text          | `text-zinc-900`   | #18181B |
| Secondary text     | `text-zinc-500`   | #71717A |

### Base (dark)
| Role               | Token             | Hex     |
|--------------------|-------------------|---------|
| Page background    | `bg-zinc-950`     | #09090B |
| Surface (card)     | `bg-zinc-900`     | #18181B |
| Surface (elevated) | `bg-zinc-800`     | #27272A |
| Border             | `border-zinc-800` | #27272A |
| Body text          | `text-zinc-100`   | #F4F4F5 |
| Secondary text     | `text-zinc-400`   | #A1A1AA |

### Accent

A single fixed accent color: Tailwind `teal-600` (#0D9488) — calm, considered, distinct from default-blue defaults. Exposed as `--accent` (RGB triplet) and consumed via `bg-accent` / `text-accent` / `border-accent` Tailwind tokens.

The accent is used sparingly: primary buttons, the active item indicator, the integrity-verified badge fill, the progress strip's filled phases. Never as a background wash.

### Status (always paired with icon + shape, never color alone)

| State    | Token              | Hex     | Icon          | Shape                |
|----------|--------------------|---------|---------------|----------------------|
| Verified | `emerald-600`      | #059669 | shield-check  | rounded-full pill    |
| Pending  | `amber-500`        | #F59E0B | shield-question | rounded-full pill  |
| Mismatch | `rose-600`         | #E11D48 | shield-x      | rounded-full pill    |
| Info     | `blue-600`         | #2563EB | info          | rounded-md notice    |

In dark mode, status colors shift one stop lighter (emerald-500, amber-400, rose-500) for comfort.

### Cultural neutrality check

Avoid pure green as a primary accent in MENA-region UIs (strong religious/political associations). Avoid red as a default/idle color (alarm associations universal). Avoid pure black-on-white maximum-contrast (clinical/intimidating). Our zinc-on-zinc with teal accent passes all three.

---

## 3. Typography

### Fonts (bundled, no CDN)

- **Latin script:** Inter (variable). Weights 400 / 500 / 600 / 700.
- **Arabic script:** Noto Sans Arabic. Weights matched.

Both ship as `woff2` in `app/static/fonts/`. Inter is the body default; Noto Sans Arabic loads automatically when `<html lang>` is `ar` (or any Arabic-script locale we add later).

### Scale (Tailwind tokens)

| Use             | Token       | Size / leading |
|-----------------|-------------|----------------|
| Caption         | `text-xs`   | 12 / 16        |
| Body small      | `text-sm`   | 14 / 20        |
| **Body**        | `text-base` | 16 / 26        |
| Lede            | `text-lg`   | 18 / 28        |
| H3              | `text-xl`   | 20 / 28        |
| H2              | `text-2xl`  | 24 / 32        |
| H1              | `text-3xl`  | 30 / 36        |

Body leading is intentionally generous (26 instead of the default 24) — Arabic and CJK glyphs sit taller and need the breathing room. Don't tighten below `leading-7` for body.

### Numerals

`font-variant-numeric: tabular-nums` for any column of numbers (file sizes, durations, hash digits, timestamps). Otherwise the column "wobbles" as digits change.

Hash and URL strings use a monospace stack (`font-mono`) at `text-sm` so users can compare characters reliably.

### Bidi handling

Wrap any user-content field (title, uploader, URL, description) in `<bdi>`. Without it, an Arabic UI containing a Latin URL — or vice versa — will render with the punctuation jumping to the wrong end.

---

## 4. Iconography

### Primary set: Lucide

Lucide is the foundation. Stroke 1.5. Default size matches the surrounding text (1em).

We bundle a curated subset to keep the asset weight reasonable. The full list lives in `app/static/icons/lucide/manifest.json`. Adding an icon means adding it to the manifest and re-running the bundler.

### Custom domain icons

We extend Lucide's vocabulary with concept-specific marks:

| Concept              | Visual                                              |
|----------------------|-----------------------------------------------------|
| Capsule (brand)      | bell jar over a browser-window specimen (see §1)    |
| Capture-kind: media  | film (Lucide)                                       |
| Capture-kind: page   | layout-template (Lucide)                            |
| Phase: page          | globe (Lucide)                                      |
| Phase: media         | download-cloud (Lucide)                             |
| Phase: hash          | hash (Lucide)                                       |
| Phase: sign          | shield-check (Lucide)                               |
| Cookies / auth       | key (Lucide)                                        |
| Case folder          | folder (Lucide)                                     |
| Integrity verified   | shield-check inside circular seal frame (custom)    |
| Integrity pending    | shield-question, dotted seal frame (custom)         |
| Integrity mismatch   | shield-x, broken seal frame (custom)                |

The seal frame is the integrity vocabulary. **A sealed shield is universal across cultures.** Users learn it once and read it instantly forever after.

### Platform marks

YouTube, Twitter/X, TikTok, Instagram, Facebook, LinkedIn, Reddit, Vimeo, SoundCloud, Bandcamp, Bilibili, Threads, plus a generic `globe` for anything else. Stored as monochrome SVG at `app/static/icons/platforms/`. Tinted via `currentColor`. We draw them in a consistent stroke width so they sit alongside Lucide without looking pasted-in.

### Direction-implying icons

Chevrons, arrows, progress markers — anything that has a left-or-right meaning — get the `.icon-directional` class, which mirrors via `transform: scaleX(-1)` under `[dir="rtl"]`. **Brand and platform icons never mirror.**

### When to use an icon-only button

Allowed in toolbars and table-row actions when the icon is unambiguous from context: folder (open), download, three-dots-vertical (more), x (close), trash (delete). Always with `aria-label` from the i18n bundle. Never use icon-only for primary actions on a screen.

---

## 5. Spatial system

4-pixel grid. The full spacing scale: 4 / 8 / 12 / 16 / 24 / 32 / 48 / 64.

### Containers
| Layer            | Max width         |
|------------------|-------------------|
| App shell        | full viewport     |
| Page content     | `max-w-7xl` (1280)|
| Read-heavy text  | `max-w-3xl` (768) |
| Modal            | `max-w-lg` (512)  |

### Cards

- Padding: `p-4` (16) compact, `p-6` (24) standard.
- Corners: `rounded-xl` (12). The capsule motif rhymes with this radius — it's intentional.
- Border: 1px (`border-zinc-200` light, `border-zinc-800` dark).
- Shadow: `shadow-sm`. Never heavier. We are not a glass-morphism app.
- Gutter in grids: `gap-6` (24) standard, `gap-8` (32) for hero grids.

---

## 6. Component vocabulary

The interface is built from a small alphabet of visual elements. Familiarize yourself with these — when in doubt, compose existing pieces, don't invent.

### `AppShell`

```
┌──────────────────────────────────────────────┐
│ ▣ Capsule                              AR ⚙ │  ← header (h-14)
├──────────────────────────────────────────────┤
│                                              │
│              {main content}                  │
│                                              │
├──────────────────────────────────────────────┤
│ ●● 2 active    🛡 all clear                  │  ← status bar (h-10)
└──────────────────────────────────────────────┘
```

Fixed header: brand mark, language picker, settings cog. Fixed status bar: active-job indicator, library-integrity pulse. Both bars are 1px-bordered, surface-1 background.

### `CaseCard`

```
┌──────────────────────────┐
│ ┌─┐ ┌─┐ ┌─┐ ┌─┐          │  ← thumbnail mosaic (recent 4)
│ └─┘ └─┘ └─┘ └─┘          │
│                          │
│ Operation Sundial   ⬤   │  ← title • status pill
│ 47 captures · 2 days ago │  ← meta (text-sm zinc-500)
└──────────────────────────┘
```

### `CaptureCard`

```
┌──────────────────────────┐
│ ▶ {platform}             │  ← top-right: platform mark
│ ┌──────────────────────┐ │
│ │     thumbnail or     │ │
│ │   page screenshot    │ │
│ └──────────────────────┘ │
│ 🛡 Title here           │  ← integrity badge + title (1 line, bdi)
│ uploader · 12.4 MB · ⋮  │  ← meta + three-dot menu
└──────────────────────────┘
```

Aspect: media items 16:9; page-only items 4:3 cropped from the screenshot. Integrity badge: top-left, small. Platform mark: top-right, small.

### `ProgressStrip`

The capture-phase visualization. **Not a percent bar.** Four icons, evenly spaced, with a connector line between them that fills as phases complete:

```
●━━━━━●━━━━━○━━━━━○
🌐    ⬇     #     🛡
page  media hash sign
```

States per icon: dim (pending) → accent (active, with subtle pulse) → solid (done). The connecting line is a 2px rule that fills accent-colored as each phase advances. Total width adapts to container.

A user looking at this for half a second understands: *page is captured, media is downloading, hash and sign haven't started.* No words required.

### `PastePreview`

Slides in below the URL input on paste, height-animated 200ms. Contains:

- Detected platform mark (top-left, large)
- Optimistic thumbnail (only if user opted into prefetch — otherwise a folder-with-globe placeholder)
- Redirect chain as a small icon chain: `🌐 → 🌐 → 🌐` with hover-tooltip showing each URL
- Authenticated-domain chip if applicable: `🔑 Authenticated as twitter.com` in accent color
- Capture-kind preview chip: `🎞 media + page` or `📄 page only`
- Primary "Capture" button on the trailing edge

This card is the experiential heart of the app. It deserves microinteraction polish.

### `IntegrityBadge`

A shield icon inside a circular seal frame. Three states (icon + frame style + color all redundant):

- **Verified:** `shield-check` + solid frame + emerald
- **Pending:** `shield-question` + dotted frame + amber
- **Mismatch:** `shield-x` + broken frame + rose

Compact pill; click to expand into a small tooltip showing the SHA-256 prefix and last-verified timestamp.

### `AuditTimeline`

Vertical timeline. Each entry is a small card with: action icon, action verb (translatable), timestamp (local TZ via `Intl.DateTimeFormat`), entity link. Connected by a 2px accent rule. Top of the list shows a green "Chain verified" banner with the last verification time. If the chain breaks, a rose banner pinpoints the broken row.

### `EmptyState`

Centered. Pattern: illustration (200–280px wide, single-color tinted to `--accent`) + one-line headline + one CTA button. Never two CTAs. Never a paragraph of explanation — that's what onboarding is for.

```
       [unDraw illustration]
    
    No cases yet
    
    [+ Create your first case]
```

---

## 7. Motion

- Default transition: 200ms `cubic-bezier(0.4, 0, 0.2, 1)`.
- Page snapshot fade-in on capture completion: 400ms opacity + 4px translate-y.
- Progress strip phase advance: 300ms linear connector fill, with a 600ms gentle pulse on the newly-active icon.
- Mode switch: 200ms cross-fade across all `--accent` surfaces simultaneously.
- Modal in/out: 150ms scale 0.96→1 + opacity.
- Hover: subtle. Cards lift `shadow-sm`→`shadow-md` over 150ms. Buttons darken accent by 5%.

**Reduced motion:** when `prefers-reduced-motion: reduce` is set, replace any translation or scaling with opacity-only transitions. Progress strip still advances (functional), but without the pulse.

---

## 8. RTL and multilingual

### The "Test in Arabic first" rule

When you build a screen, **render it in Arabic first.** If it works in Arabic, it works everywhere. If you only test in English, you are accumulating RTL debt.

### Layout

Every layout uses logical CSS properties (`ms-*`, `me-*`, `ps-*`, `pe-*`, `start-*`, `end-*`, `border-s-*`, `border-e-*`). Never `ml`, `mr`, `pl`, `pr`, `left`, `right` for layout. Tailwind's RTL plugin is enabled.

### Typography

Arabic is taller and more horizontally compact than Latin at the same point size. Body leading is generous (26 / 16) to accommodate. Avoid forcing line-heights below `leading-7` for body text.

### Bidi

User content (titles, URLs, descriptions, uploader names) is wrapped in `<bdi>`. UI labels (buttons, headings) follow document direction.

### Numerals

We do NOT override locale-default numeral systems. `Intl.NumberFormat('ar')` produces Arabic-Indic digits (٠١٢٣٤٥٦٧٨٩). That's correct. Don't force Western digits.

### Direction-implying icons

`.icon-directional` class flips horizontally under `[dir="rtl"]`. Apply to: chevron-left/right, arrow-left/right, send, undo, redo. Apply NOT to: brand mark, platform marks, gauge needles, locks, shields, abstract icons.

---

## 9. Empty states & illustrations

Empty states are not afterthoughts. They are the moments a user sees the most when they're new, and the moments that decide whether the app feels considered or thrown together.

### Source: unDraw

Single-color illustrations from [unDraw](https://undraw.co/), tinted at runtime to the active accent via the `--accent` CSS variable. Bundled SVGs in `app/static/icons/illustrations/`.

Required illustrations:

- `cases-empty.svg` — investigator at a desk, no folders yet
- `library-empty.svg` — empty shelves
- `audit-empty.svg` — quiet ledger
- `error-generic.svg` — neutral "something off" mark, NOT alarmist
- `verify-success.svg` — sealed envelope
- `onboarding-1.svg` ... `onboarding-4.svg` — the four onboarding step heroes

### Anti-patterns in illustration

- **No faces with strong cultural cues.** unDraw's faceless figures are what we want.
- **No legal-courtroom imagery** (gavels, scales). They cue intimidation.
- **No locks/keys as the dominant motif.** Cookies/auth uses the key icon; full-frame lock illustrations imply the data is locked away from the user, which it isn't.

---

## 11. Anti-patterns (don't)

- **Skeuomorphism.** No fake court-stamps, fake stone tablets, fake legal-pad textures, fake leather. Modern, flat, considered.
- **Glassmorphism / heavy blurs.** No `backdrop-filter: blur(20px)` panels.
- **Neon / saturation surges.** Our accents are `teal-600` and `indigo-700`, not the 400 variants.
- **Gradient backgrounds on primary surfaces.** Gradients permitted only on the brand-mark fill at large display sizes (logo splash) — never on cards, buttons, or backgrounds.
- **Drop shadows beyond `shadow-md`.** No glowing buttons, no halo around cards.
- **Emoji as UI.** 🚀 has no place in a tool that produces court evidence.
- **Confirmation modals for low-stakes actions.** Use undo affordances instead. Reserve modals for genuinely destructive operations (delete a case, replace signing key).
- **Toasts for everything.** Reserve toasts for ephemeral, non-blocking confirmations (export complete, link copied). Never for errors that require user action — those are inline cards.
- **Tooltips as primary information.** Tooltips supplement; they don't carry essential meaning.
- **Color-only status.** Always icon + shape too.

---

## 12. Implementation conventions

- All translatable strings go through `t("key")`. No exceptions in committed code.
- All status, button, and chip elements get an icon. Icons get `aria-hidden="true"` when accompanied by text; icons-only get `aria-label` from i18n.
- All padding/margin uses logical Tailwind classes (`ps-*`, `pe-*`, etc.).
- All user-content rendering uses `<bdi>`.
- All animations check `prefers-reduced-motion`.
- All custom colors trace back to a Tailwind token. Don't introduce new hex codes; extend the theme.
- All illustrations are tinted via `currentColor` or the `--accent` CSS variable, never hard-coded.

When in doubt: **strip until calm, then add only what carries meaning.**
