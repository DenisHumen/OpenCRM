# OpenCRM — Design mockups

Interactive HTML prototype built from [docs/05-crm-design.md](../05-crm-design.md) and [docs/06-showcase-design.md](../06-showcase-design.md).

**Start here:** open `Index.dc.html` in a browser — it links every screen. All navigation, tabs, toggles, search and the lightbox are clickable. Dashed placeholders accept drag-and-dropped images.

## Screens

| File | Screen |
|---|---|
| `Index.dc.html` | Map of all mockups |
| `Auth.dc.html` | Sign in / Register / "Awaiting approval" |
| `Dashboard.dc.html` | Dashboard: greeting, metrics, views chart, recent boards/clients |
| `Clients.dc.html` | Client list with live search and filters |
| `Client.dc.html` | Client card: pinned contacts, history feed, files, boards |
| `Boards.dc.html` | Boards grid with status filters |
| `BoardEditor.dc.html` | Board editor: works grid + settings + Share block |
| `Staff.dc.html` | Staff (root): signup requests, active, deactivated |
| `Settings.dc.html` | Site settings (root): brand, contacts, showcase defaults |
| `Profile.dc.html` | Profile: name, language, password |
| `Showcase.dc.html` | Public showcase: masonry, hover captions, lightbox |
| `Pin.dc.html` | PIN entry (correct code in the mockup: `4821`) |
| `Closed.dc.html` | "Access closed" service page |
| `Mobile.dc.html` | Showcase/PIN/Closed at 390px in an iPhone frame |

`Sidebar.dc.html` is the shared CRM sidebar component; `image-slot.js` and `ios-frame.jsx` are helper components; `support.js` is the component runtime. Keep the whole folder together — screens reference these files relatively.

## Design decisions locked in these mockups

- Palette exactly per design notes: bg `#262624`, sidebar `#1F1E1C`, surface `#2B2A27`, hover `#34322E`, border `#3B3A36`, text `#F5F4EF`/`#9C998F`/`#706D64`, accent `#6C8EEF`, brand `#D97757`, success `#4CAF6E`, warning `#E8A23D`, danger `#E5695E`, primary button `#FAF9F5`.
- Type: Source Serif 4 (page titles, showcase board title) + Inter (UI). Loaded from Google Fonts in mockups; self-host in production.
- Radii 12/8/6, 1px borders, no shadows; hovers lighten to `--surface-2` in 150ms.
- Showcase: deeper bg `#161514`, lightbox `#0E0D0C`, masonry columns 3/2/1, cascade reveal, `prefers-reduced-motion` respected.
- Sample brand "Formwork Studio" everywhere — replace via Site settings in the real app.
