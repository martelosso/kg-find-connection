// Flat entity shapes and their minimal pictograms, shared by the graph canvas, the legend and the step
// card's chips so an entity looks the same everywhere (docs/visual-design.md: flat, geometric, toy-like).
//
// A pictogram is a Path2D in a unit box (-1..1), filled in one colour on top of the entity's shape.

const PATHS = new Map();

function build(kind) {
  const p = new Path2D();
  switch (kind) {
    case "person": {  // head and shoulders
      p.arc(0, -0.34, 0.3, 0, 2 * Math.PI);
      p.moveTo(-0.6, 0.66);
      p.bezierCurveTo(-0.6, 0.12, -0.3, -0.02, 0, -0.02);
      p.bezierCurveTo(0.3, -0.02, 0.6, 0.12, 0.6, 0.66);
      p.closePath();
      break;
    }
    case "organization": {  // three columns under a roof line
      p.moveTo(-0.68, -0.26); p.lineTo(0, -0.66); p.lineTo(0.68, -0.26); p.closePath();
      for (const x of [-0.55, -0.11, 0.33]) p.rect(x, -0.14, 0.22, 0.56);
      p.rect(-0.7, 0.48, 1.4, 0.16);
      break;
    }
    case "work": {  // a play triangle
      p.moveTo(-0.3, -0.56); p.lineTo(0.6, 0); p.lineTo(-0.3, 0.56); p.closePath();
      break;
    }
    case "place": {  // a map pin with a hole (filled with the even-odd rule)
      p.moveTo(0, 0.74);
      p.bezierCurveTo(-0.16, 0.46, -0.52, 0.16, -0.52, -0.18);
      p.arc(0, -0.18, 0.52, Math.PI, 0);
      p.bezierCurveTo(0.52, 0.16, 0.16, 0.46, 0, 0.74);
      p.closePath();
      p.moveTo(0.2, -0.18);
      p.arc(0, -0.18, 0.2, 0, 2 * Math.PI);
      break;
    }
    case "position": {  // a five-pointed star
      for (let i = 0; i < 10; i++) {
        const a = -Math.PI / 2 + (i * Math.PI) / 5, r = i % 2 ? 0.3 : 0.7;
        i ? p.lineTo(r * Math.cos(a), r * Math.sin(a) + 0.04) : p.moveTo(r * Math.cos(a), r * Math.sin(a) + 0.04);
      }
      p.closePath();
      break;
    }
    default:  // anything else: a small dot
      p.arc(0, 0, 0.28, 0, 2 * Math.PI);
  }
  return p;
}

function glyphPath(kind) {
  if (!PATHS.has(kind)) PATHS.set(kind, build(kind));
  return PATHS.get(kind);
}

/** People (and the two endpoints) are circles; everything else is a rounded square. */
export const isRound = (kind) => kind === "person" || kind === "source" || kind === "target";

/** The glyph a role or kind shows: the two endpoints are people. */
export const glyphKind = (kind) => (kind === "source" || kind === "target" ? "person" : kind);

/** `#rrggbb` mixed towards `#rrggbb` `to` by t (0..1); other colour strings pass through unchanged. */
export function mix(hex, to, t) {
  const a = parse(hex), b = parse(to);
  if (!a || !b) return hex;
  const c = a.map((v, i) => Math.round(v + (b[i] - v) * t));
  return `#${c.map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}
const parse = (hex) => {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec((hex || "").trim());
  return m ? [1, 2, 3].map((i) => parseInt(m[i], 16)) : null;
};
/** Same hue, darker (the toy "lip" under a shape). */
export const darken = (hex, t = 0.14) => mix(hex, "#000000", t);

/** Trace the outline of an entity shape (circle or rounded square) centred at x, y with half-size r. */
export function shapePath(ctx, round, x, y, r) {
  ctx.beginPath();
  if (round) { ctx.arc(x, y, r, 0, 2 * Math.PI); return; }
  const k = r * 0.62;  // corner radius: soft, not a circle
  ctx.roundRect(x - r, y - r, 2 * r, 2 * r, k);
}

/** Draw `img` cropped to fill the entity shape of half-size r (centre crop, like CSS object-fit: cover). */
function drawPhoto(ctx, img, round, x, y, r, grey) {
  const w = img.naturalWidth || img.width, h = img.naturalHeight || img.height;
  if (!w || !h) return false;
  const side = Math.min(w, h);
  // Portraits: crop towards the top, where the face usually is; landscapes: the centre.
  const sx = (w - side) / 2, sy = h > w ? (h - side) * 0.2 : (h - side) / 2;
  ctx.save();
  shapePath(ctx, round, x, y, r);
  ctx.clip();
  if (grey) {  // a greyed-out entity: a washed-out grey photo over its grey disc, so it stays in the background
    if ("filter" in ctx) ctx.filter = "grayscale(1)";
    ctx.globalAlpha *= 0.45;
  }
  try {
    ctx.drawImage(img, sx, sy, side, side, x - r, y - r, 2 * r, 2 * r);
  } catch {
    ctx.restore();
    return false;
  }
  ctx.restore();
  return true;
}

/**
 * Draw a flat entity: an optional darker "lip" under it, the solid shape, its pictogram, and an
 * optional ring (state: active, planned, selected). No shadows, gradients or outlines otherwise.
 * With `photo` (the entity's Wikidata image, loaded), the picture replaces the pictogram inside a
 * rim of the entity's colour, so kind, side and path colours keep their meaning.
 */
export function drawEntity(ctx, { kind, x, y, r, fill, glyph = "#ffffff", glyphScale = 0.62, lip = true,
                                  ring = null, ringWidth = 3, ringGap = 3, ringDash = null,
                                  photo = null, photoGrey = false }) {
  const round = isRound(kind);
  if (lip) {
    ctx.fillStyle = darken(fill, 0.16);
    shapePath(ctx, round, x, y + Math.max(1.5, r * 0.16), r);
    ctx.fill();
  }
  ctx.fillStyle = fill;
  shapePath(ctx, round, x, y, r);
  ctx.fill();
  const rim = Math.max(1.5, r * 0.14);
  if (photo && drawPhoto(ctx, photo, round, x, y, r - rim, photoGrey)) glyph = null;
  if (glyph) {
    ctx.save();
    ctx.translate(x, y);
    const s = r * glyphScale;
    ctx.scale(s, s);
    ctx.fillStyle = glyph;
    ctx.fill(glyphPath(glyphKind(kind)), "evenodd");
    ctx.restore();
  }
  if (ring) {
    ctx.save();
    ctx.strokeStyle = ring;
    ctx.lineWidth = ringWidth;
    if (ringDash) ctx.setLineDash(ringDash);
    shapePath(ctx, round, x, y, r + ringGap + ringWidth / 2);
    ctx.stroke();
    ctx.restore();
  }
}

/** A small <canvas> showing one entity (legend entries, step-card chips). `px` is its CSS size. */
export function entityIcon(kind, fill, px = 16, { lip = false, glyph = "#ffffff" } = {}) {
  const dpr = (typeof devicePixelRatio === "number" && devicePixelRatio) || 1;
  const c = document.createElement("canvas");
  c.width = Math.round(px * dpr); c.height = Math.round(px * dpr);
  c.style.width = `${px}px`; c.style.height = `${px}px`;
  c.setAttribute("aria-hidden", "true");
  const ctx = c.getContext("2d");
  ctx.scale(dpr, dpr);
  const r = px / 2 - (lip ? 1.5 : 0.5);
  drawEntity(ctx, { kind, x: px / 2, y: px / 2 - (lip ? 1 : 0), r, fill, lip, glyph, glyphScale: 0.64 });
  return c;
}
