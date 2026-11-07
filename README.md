# <img alt="PDF Occlusion logo" src="logo.svg" height="60" align="absmiddle"> ‎ ‎ ‎PDF Occlusion

[![Anki](https://img.shields.io/badge/Anki-23.10%2B-836EAA?style=flat-square)](https://apps.ankiweb.net)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-836EAA?style=flat-square)](https://github.com/pdf-occlusion/anki-pdf-occlusion)
[![License](https://img.shields.io/badge/license-MIT-836EAA?style=flat-square)](LICENSE)
[![Website](https://img.shields.io/badge/website-pdf--occlusion.github.io-836EAA?style=flat-square)](https://pdf-occlusion.github.io)

**PDF Occlusion** is an Anki add-on that creates image occlusion cards directly from PDF lecture slides. No more importing one image at a time. Open any PDF (or several at once), draw boxes over what you want to memorize, and generate cards in bulk. Built for studying from slide-heavy lectures.

## Install

Add-on code `783821131`, then restart Anki.

## Quick Start

1. `Tools → PDF Occlusion`, or `Ctrl+Shift+P` in the card editor.
2. `Open PDF` — one file or several. The arrow beside it lists recent sessions.
3. Drag boxes over what you want to memorize, or press `F` and drag over a region to have them placed for you.
4. Pick a deck and click `Create All Cards`.

## Auto-occlude

Press `Auto-occlude` and every label, term and key phrase in the PDF is boxed for you: lecture slides, atlases, textbook figures, vocabulary lists, maps, flowcharts. Look it over, change what you want, and create the cards as usual.

The same window sets how it occludes: the subject; figure labels (all or only the important ones); bullet text; numbers and doses; bold or colored text; slide titles; pictures without labels; how many cards; lists, tables and flowcharts; repeated labels; term and meaning lists; pages to skip; the notes PDF; and your own instructions. Your choices are kept for every PDF. Your first 100 pages are free; more are on the [pricing page](https://pdf-occlusion.github.io/pricing/).

## Shortcuts

| Key | Action |
|-----|--------|
| `D` / `V` / `T` | Draw / Select / Text tool |
| `F` | Detect — then drag over the area to scan |
| `P` | Peek through the masks |
| `G` / `U` | Group / ungroup selection |
| `N` | Note on selected box(es) |
| `Del` / `Backspace` | Remove selected box(es) |
| `Esc` | Clear selection |
| `←` / `→` | Slides — nudges selected boxes instead |
| `PgUp` / `PgDn` | Slides (always) |
| `Ctrl+A` | Select all boxes on the slide |
| `Ctrl+C` / `Ctrl+V` | Copy / paste boxes, across slides too |
| `Ctrl+Z` / `Ctrl+Shift+Z` | Undo / redo |
| `Ctrl+` / `Ctrl-` / `Ctrl+0` | Zoom in / out / fit |
| `Shift`+drag box | Move a grouped box's whole group |
| `Shift`+draw | New box joins the selection's group |
| `Shift`+rotate | Snap to 15° |
| Pinch / `Ctrl`+scroll | Zoom at the pointer |
| `Ctrl+Shift+V` | Cloze composer |
| `Ctrl+Shift+C` | Wrap selection as a cloze |
| `Ctrl+Return` | File the cloze card |
| `Ctrl+Shift+P` | Open from the card editor |
| `g` | **While reviewing** — show all / hide all |

## Configuration

`Tools → Add-ons → PDF Occlusion → Config`

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `occlusion_mode` | `string` | `"ao"` | `"ao"` = Hide All, Show One · `"oa"` = Hide One, Show One |
| `mask_color` | RGB | `[120, 120, 120]` | Boxes not being tested |
| `highlight_color` | RGB | `[131, 110, 170]` | The box being tested |
| `mask_opacity` | int 0–255 | `255` | Mask opacity **on cards** |
| `editing_mask_opacity` | int 0–255 | `150` | Mask opacity **in the editor**. At `255` a box hides its content as you draw it; `P` peeks through |
| `show_all_shortcut` | `string` | `"g"` | Key that reveals the whole slide while reviewing. `""` for none |
| `render_dpi_scale` | `float` | `2.0` | Render resolution — higher = sharper cards, more memory |
| `default_zoom` | `"fit"` / `float` | `"fit"` | Zoom when a PDF opens |
| `default_deck` | `string` | `""` | Deck preselected in the picker |
| `note_type_name` | `string` | `"PDF Occlusion"` | Note type to create or reuse |
| `add_editor_button` | `bool` | `true` | Show the toolbar button in the card editor |
| `close_after_creating` | `bool` | `true` | Close the dialog once cards are created |
| `pdf_hosting` | object | off | Publish the Notes / Slides PDFs so the card buttons open on your phone: `folder` you serve on the web (e.g. a GitHub Pages clone), its `base_url`, and `git_push` to commit and push them for you with your own git credentials. Off = cards keep local paths, desktop only. Published PDFs are public |

## Open the PDFs on your phone

Every card has **Slides** and **Notes** buttons that open the lecture's PDFs. Out of the box they point at the files on your computer, so they only work in desktop Anki and nothing leaves your machine. To make them work in AnkiMobile and AnkiWeb too, give the add-on a folder that is published on the web:

1. Make a GitHub repository with GitHub Pages turned on, and clone it to your computer.
2. In Anki, `Tools → Add-ons → PDF Occlusion → Config`, and fill in `pdf_hosting`:
   ```json
   "pdf_hosting": {
       "folder": "~/Developer/my-pdfs/lectures",
       "base_url": "https://YOUR-NAME.github.io/my-pdfs/lectures/",
       "git_push": true
   }
   ```
3. From then on, `Create All Cards` copies each PDF into that folder, commits it, pushes it, and gives the cards its web address.

The add-on never asks for or stores a GitHub password or token. It runs `git push` in that folder, so it pushes with whatever login your clone already uses, and only to the repository you chose. Anyone with a PDF's exact address can open it, so only publish what you are allowed to share. Set `git_push` to `false` to commit and push yourself.

## Contact

- Ravi Bandaru: ravi14.bandaru@gmail.com
- Johanna Lee: johanna.lee@students.jefferson.edu

## License

MIT. See the included `LICENSE` file.
