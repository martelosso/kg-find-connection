### Visual Direction — Mini Motorways Aesthetic

Style the graph visualization using the minimalist visual language of **Mini Motorways (Dinosaur Polo Club)**.

This is strictly an **aesthetic reference**, not a thematic one. Do not introduce cars, buildings, cities, traffic, or other game-specific elements. The application remains a knowledge graph traversal interface.

**Design principles:**

- **Color palette:** Soft, muted pastel backgrounds with carefully selected, contrasting accent colors. Use a cohesive, limited palette inspired by Mini Motorways.
- **Graph edges:** Thick, smooth, rounded connections resembling the game's road styling. Use rounded line caps and corners, with gentle curves where appropriate.
- **Nodes and icons:** Simple, flat geometric shapes and minimalist pictograms. Use solid fills, clean silhouettes, and consistent proportions. Avoid detailed illustrations.
- **Shapes:** Favor circles, rounded squares, and softly rounded geometric forms. Maintain a friendly, precise, almost toy-like appearance.
- **Typography:** Clean, understated sans-serif typography with excellent readability. Labels should feel integrated into the visualization.
- **Depth and texture:** Primarily flat design, with little to no shadows, gradients, outlines, or decorative textures.
- **Spacing:** Generous negative space, balanced compositions, and a clear visual hierarchy.
- **Animation:** Smooth, restrained transitions with gentle easing. Traversal paths should animate progressively, with subtle color changes and node highlights.

**Graph-specific styling:**

- Keep the graph's actual topology and semantics intact.
- Render connections as smooth, road-like paths.
- Distinguish active, visited, and unexplored paths primarily through color, opacity, and stroke weight.
- Use minimalist icons to distinguish entity types (people, organizations, locations, etc.).
- Keep labels legible and avoid visual clutter.

**Avoid:**

- City-building imagery or gameplay elements.
- Neon colors, glowing effects, glassmorphism, or strong gradients.
- Generic network-graph aesthetics with thin, tangled lines and tiny dots.
- Excessive shadows, borders, or technical dashboard styling.

**Desired result:** A knowledge graph explorer that looks as though its visual system was designed by the artists behind Mini Motorways: soft colors, elegant geometry, beautiful connections, minimal icons, and polished motion.

The goal is to capture the game's **graphic design, not its subject matter**.
---

### Implemented as (branch `visual-experiment`, 2026-11-15)

Decided with the user: dark asphalt roads on a cream ground, no arrowheads (direction stays in the hop label, the tooltip and the Turtle), Nunito Sans bundled offline, and the whole workbench in scope (graph, chrome, motion). Colour *meanings* did not change; only their values did.

- **Tokens** (`src/ui/css/workbench.css` `:root`): land `--bg` #f1eadb; roads `--road` (asphalt trail), `--road-mid` (normal), `--road-dim` (unexplored); teal/coral people, lavender organizations, leaf works, sky places; mint = new/next, periwinkle = agent, gold = strongest path on an asphalt rim (`--highway-rim`), pink = sandbox. Shape tokens: `--r-card`, `--r-ctl`, `--r-pill`, `--font-ui`. No box-shadows anywhere.
- **Entities** (`src/ui/js/glyphs.js` + `GraphView._renderNode`, a vis `shape: "custom"` renderer): circles for people, rounded squares otherwise, a white pictogram per kind (person, organization, work, place, position), a darker toy "lip" instead of a shadow, rings for state (active, dashed = planned, mint = ranked, ink = selected). Labels are drawn by the renderer; non-people use small tracked caps. The ①②③ exploration order is a small asphalt tag at the top right. The same shapes appear in the legend and on the step card's chips (`entityIcon`).
- **Roads** (`GraphView._edgeStyle`): straight, round caps and joins, no arrows; parallel statements curve gently to alternate sides. The strongest path is redrawn on top of all roads by `_drawPathRoads` (vis draws edges in insertion order, which buried it), with upright relation names and the path's entity names above it.
- **Motion** (`src/ui/js/motion.js`): new entities pop in with a small overshoot (`easeOutBack`, `pop`); a planned node sends out one ripple (`ripple`); the path is paved continuously, hop by hop (`pave`); the compute_paths ping uses flat gold pellets (no glow); the get_search_status beam is a flat translucent wedge; "#k" tags pop in. `prefers-reduced-motion` makes these still.
- **Spotlight on the connection** (path moments and the result step; `setStory({spotlight})`, `GraphView._spotStyle`): only the path and the two people stay in full. Every other road becomes a faint thin line without a name, and every other entity a small plain pale dot without pictogram, name or order tag. The replaced path stays dashed. Relation names sit on the road as cream signs with a gold outline, in walking direction like the card ("acted in", from the hop labels), sized against the camera zoom. Path names alternate below and above the line. When the two people are too close for every hop to get 190 canvas units, `straightenPath` glides them apart.
