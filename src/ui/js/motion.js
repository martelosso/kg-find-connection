// One tempo for everything that moves in the workbench (the "slow, methodical exploration" style).
// CSS uses the matching --t-* variables in workbench.css; keep the two in step.

/** The viewer asked the OS for less motion: pops, ripples and pings become still. */
export const REDUCED = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

export const MOTION = {
  camera: 1400,  // ms: every camera glide (step camera, Fit, timeline/path focus)
  grow: 650,     // ms: one branch growing out of its parent
  fade: 900,     // ms: a new node fading in / a planned branch lighting up
  trace: 420,    // ms per hop: legacy stepped trace (kept for callers that time against it)
  pave: 380,     // ms per hop: the path's gold band drawn continuously along each road
  pop: 420,      // ms: a new entity pops from 60% to full size with a gentle overshoot
  ripple: 1200,  // ms: one ring spreading out from a node the agent plans to expand
  fresh: 1500,   // ms: a just-fetched edge stays highlighted before taking its story style
  easing: "easeInOutCubic",  // vis-network's name for the curve the camera uses
};

/** The same ease-in-out cubic the camera and CSS (--ease) use. */
export const easeInOut = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

/** Ease-out with a small overshoot (peaks ≈ 1.06), the toy-like "pop" (CSS --ease-back). */
export const easeOutBack = (t) => {
  const c1 = 1.2, c3 = c1 + 1;
  return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2);
};
