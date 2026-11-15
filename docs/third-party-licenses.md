# Third-party frontend libraries

These libraries are vendored so the workshop UI runs without network access. Keep their license notices intact when moving or updating them.

| Library | Version | Location | License |
|---|---|---|---|
| CodeMirror 5 | 5.65.16 | `src/ui/vendor/codemirror/` (`codemirror.min.js`, `codemirror.min.css`) | MIT. Full text in `src/ui/vendor/codemirror/LICENSE` |
| vis-network | 10.1.2 | `src/ui/vendor/vis-network.min.js` | Dual-licensed Apache-2.0 or MIT. Notice in the file header |
| Nunito Sans (font) | fontsource 5.0.13 | `src/ui/vendor/fonts/` (latin + latin-ext woff2, weights 400/600/700/800) | SIL Open Font License 1.1. Full text in `src/ui/vendor/fonts/OFL.txt` |

Sources:
- Nunito Sans: `https://cdn.jsdelivr.net/npm/@fontsource/nunito-sans@5.0.13/files/` (woff2) and `…/LICENSE`
- CodeMirror: `https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/`; LICENSE from `https://cdn.jsdelivr.net/npm/codemirror@5.65.16/LICENSE`
- vis-network: https://visjs.github.io/vis-network/

When the workbench moves to `src/ui/vendor/` (Milestone 5), update the locations above.
