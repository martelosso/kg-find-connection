// One tempo for everything that moves in the workbench (the "slow, methodical exploration" style).
// CSS uses the matching --t-* variables in workbench.css; keep the two in step.

export const MOTION = {
  camera: 1400,  // ms: every camera glide (step camera, Fit, timeline/path focus)
  grow: 650,     // ms: one branch growing out of its parent
  fade: 900,     // ms: a new node fading in / a planned branch lighting up
  trace: 420,    // ms per hop: a path drawn from the people towards the middle
  fresh: 1500,   // ms: a just-fetched edge stays highlighted before taking its story style
  easing: "easeInOutCubic",  // vis-network's name for the curve the camera uses
};

/** The same ease-in-out cubic the camera and CSS (--ease) use. */
export const easeInOut = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
