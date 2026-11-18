// Entity pictures for the graph: each entity's Wikidata image (P18), via GET /api/images, loaded
// from Wikimedia Commons. Best effort by design: an entity without an image, a lookup that fails
// or a picture that does not load simply stays drawn as before (its pictogram). Nothing here ever
// blocks or changes the graph's facts.
//
// DOM-free apart from the default loader, so it runs in Node tests with injected fetch/load.

/** Load one picture; resolves with the decoded image, rejects when it does not load. */
export function loadPicture(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.decoding = "async";
    img.referrerPolicy = "no-referrer";
    img.onload = () => (img.naturalWidth > 0 ? resolve(img) : reject(new Error("empty image")));
    img.onerror = () => reject(new Error("image did not load"));
    img.src = url;
  });
}

export class ImageStore {
  /**
   * @param fetchImages (qids) => Promise<{images: {qid: {file, thumb, page} | null}, pending: [qid]}>
   * @param loadImage   (url) => Promise<image>   (default: loadPicture)
   * @param onReady     (qid) => void, called when a picture becomes drawable
   */
  constructor({ fetchImages, loadImage = loadPicture, onReady = () => {}, delay = 150, batch = 300,
                retryMs = 30000, maxRetries = 3, schedule = (fn, ms) => setTimeout(fn, ms) } = {}) {
    Object.assign(this, { fetchImages, loadImage, onReady, delay, batch, retryMs, maxRetries, schedule });
    this.state = new Map();   // qid -> {status: queued|asked|none|loading|ready|failed, img?, info?}
    this.tries = new Map();   // qid -> lookups that came back pending
    this.queue = new Set();
    this.timer = null;
  }

  /** Ask for the pictures of these entities (already known ones are skipped). */
  want(ids) {
    for (const id of ids) {
      if (!this.state.has(id)) { this.state.set(id, { status: "queued" }); this.queue.add(id); }
    }
    if (this.queue.size && this.timer === null) this.timer = this.schedule(() => this.flush(), this.delay);
  }

  /** The drawable picture of an entity, or null (none, not yet, or failed). */
  get(id) {
    const s = this.state.get(id);
    return s && s.status === "ready" ? s.img : null;
  }

  /** {file, page} of a drawn picture (for the credit line), or null. */
  info(id) {
    const s = this.state.get(id);
    return s && s.status === "ready" ? s.info : null;
  }

  async flush() {
    this.timer = null;
    const ids = [...this.queue];
    this.queue.clear();
    for (let i = 0; i < ids.length; i += this.batch) await this._ask(ids.slice(i, i + this.batch));
  }

  async _ask(ids) {
    for (const id of ids) this.state.set(id, { status: "asked" });
    let res;
    try {
      res = await this.fetchImages(ids);
    } catch {
      res = { images: {}, pending: ids };  // server unreachable: the same as "not known yet"
    }
    const images = res?.images || {};
    const loads = [];
    for (const id of ids) {
      if (!(id in images)) continue;
      const pic = images[id];
      if (!pic || !pic.thumb) { this.state.set(id, { status: "none" }); continue; }
      const entry = { status: "loading", info: { file: pic.file, page: pic.page } };
      this.state.set(id, entry);
      loads.push(Promise.resolve().then(() => this.loadImage(pic.thumb)).then(
        (img) => { entry.status = "ready"; entry.img = img; this.onReady(id); },
        () => { entry.status = "failed"; }));
    }
    const later = ids.filter((id) => !(id in images));
    for (const id of later) {
      const n = (this.tries.get(id) || 0) + 1;
      this.tries.set(id, n);
      this.state.delete(id);  // unknown again: a later want() (or the retry below) asks once more
    }
    const retry = later.filter((id) => this.tries.get(id) < this.maxRetries);
    if (retry.length) this.schedule(() => this.want(retry), this.retryMs);
    await Promise.all(loads);
  }
}
