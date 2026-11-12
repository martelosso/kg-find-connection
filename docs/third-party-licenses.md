# Third-party frontend libraries

These libraries are vendored so the workshop UI runs without network access. Keep their license notices intact when moving or updating them.

| Library | Version | Location | License |
|---|---|---|---|
| CodeMirror 5 | 5.65.16 | `src/ui/vendor/codemirror/` (`codemirror.min.js`, `codemirror.min.css`) | MIT. Full text in `src/ui/vendor/codemirror/LICENSE` |
| vis-network | 10.1.2 | `src/ui/vendor/vis-network.min.js` | Dual-licensed Apache-2.0 or MIT. Notice in the file header |

Sources:
- CodeMirror: `https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/`; LICENSE from `https://cdn.jsdelivr.net/npm/codemirror@5.65.16/LICENSE`
- vis-network: https://visjs.github.io/vis-network/

When the workbench moves to `src/ui/vendor/` (Milestone 5), update the locations above.
