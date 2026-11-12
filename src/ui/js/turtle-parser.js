// Turtle tokenizer/parser and CodeMirror mode, extracted VERBATIM from graph-view/live-editor.html.
//
// This is NOT a full RDF Turtle parser. It maps statements to source offsets for highlighting.
// The backend generates compact Turtle that stays inside the supported subset and validates it
// with RDFLib; sandbox edits are parsed and validated by RDFLib on the server, never here.
// Offsets are JavaScript string indexes (UTF-16 code units), the same units as backend spans.

const RDF_TYPE = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#type';
const RDFS     = 'http://www.w3.org/2000/01/rdf-schema#';
const OWL      = 'http://www.w3.org/2002/07/owl#';
const XSD      = 'http://www.w3.org/2001/XMLSchema#';
const SCHEMA_TYPES = new Set([OWL+'Class',OWL+'ObjectProperty',OWL+'DatatypeProperty',OWL+'Ontology']);

const TOK_RE = new RegExp([
  '#[^\\n]*',
  '"""(?:[^\\\\]|\\\\.)*?"""',
  '"(?:[^"\\\\]|\\\\.)*"',
  '<[^>]*>',
  '\\^\\^[\\w-]+:[\\w.-]+',
  '\\^\\^<[^>]*>',
  '@[a-zA-Z][a-zA-Z-]*',
  '[\\w-]*:[\\w.-]+',
  '[\\w-]*:',
  '[.,;]',
  '-?\\d+\\.\\d+',
  '-?\\d+',
  '[A-Za-z_][\\w-]*',
].join('|'), 'g');

export function parseTurtle(src) {
  const toks = [];
  let m; TOK_RE.lastIndex = 0;
  // Tokens keep their source offsets so triples can be mapped back to editor text.
  while ((m = TOK_RE.exec(src)) !== null)
    if (!m[0].startsWith('#')) toks.push({ s:m[0], start:m.index, end:m.index + m[0].length });

  const pfx = {}, triples = [];
  let i = 0;

  function resolve(pn) {
    const k = pn.indexOf(':');
    const ns = pn.slice(0, k), loc = pn.slice(k + 1);
    return (ns in pfx) ? pfx[ns] + loc : pn;
  }

  // Returns a term plus the source range [start, end) it was read from.
  function term() {
    const tk = toks[i];
    if (!tk) throw new Error('Unexpected end of input.');
    const t = tk.s, start = tk.start;
    const done = (o) => { i++; return { ...o, start, end: toks[i-1].end }; };
    if (t.startsWith('<'))              return done({ type:'iri', value:t.slice(1,-1) });
    if (t === 'a')                      return done({ type:'iri', value:RDF_TYPE });
    if (t === 'true' || t === 'false')  return done({ type:'literal', value:t, datatype:XSD+'boolean' });
    if (t.startsWith('"')) {
      i++;
      const q = t.startsWith('"""') ? 3 : 1;
      let val = t.slice(q, t.length - q).replace(/\\(.)/g, (_,c) =>
        ({n:'\n',t:'\t',r:'\r','"':'"','\\':'\\'}[c]||c));
      let lang = null, dt = null;
      const nx = toks[i]?.s;
      if (nx && nx.startsWith('@'))   { lang = nx.slice(1); i++; }
      else if (nx && nx.startsWith('^^')) {
        const raw = nx.slice(2);
        dt = raw.startsWith('<') ? raw.slice(1,-1) : resolve(raw);
        i++;
      }
      return { type:'literal', value:val, lang, datatype:dt, start, end: toks[i-1].end };
    }
    if (/^-?\d+\.\d+$/.test(t)) return done({ type:'literal', value:t, datatype:XSD+'decimal' });
    if (/^-?\d+$/.test(t))      return done({ type:'literal', value:t, datatype:XSD+'integer' });
    if (t.includes(':'))        return done({ type:'iri', value:resolve(t) });
    throw new Error(`Unexpected token "${t}".`);
  }

  while (i < toks.length) {
    const t0 = toks[i].s;
    if (t0 === '@prefix' || t0 === 'prefix') {
      i++;
      const label = toks[i].s; i++;
      const iri   = toks[i].s; i++;
      pfx[label.slice(0,-1)] = iri.slice(1,-1);
      if (toks[i]?.s === '.') i++;
      continue;
    }
    if (t0 === '@base' || t0 === 'base') { i++; i++; if (toks[i]?.s==='.') i++; continue; }

    const subj = term();
    const first = triples.length;
    loop: while (true) {
      const pred = term();
      let obj = term();
      triples.push({ subj, pred, obj });
      while (toks[i]?.s === ',') { i++; obj = term(); triples.push({ subj, pred, obj }); }
      switch (toks[i]?.s) {
        case ';': i++; if (toks[i]?.s === '.') { i++; break loop; } break;
        case '.': i++; break loop;
        default:  break loop;
      }
    }
    // Every triple knows the whole statement it came from, for graph -> editor marking.
    const stmt = [subj.start, toks[i-1].end];
    for (let k = first; k < triples.length; k++) triples[k].stmt = stmt;
  }
  return triples;
}

export { RDF_TYPE, RDFS };

export function defineTurtleMode(CodeMirror) {
  CodeMirror.defineMode('turtle', function () {
    return {
      startState: () => ({ inString: false }),
      token(stream) {
        if (stream.match(/^\s*#.*/)) return 'comment';
        if (stream.eatSpace()) return null;
        if (stream.match('"""')) {
          while (!stream.match('"""')) { if (stream.next() === null) break; }
          return 'string';
        }
        if (stream.peek() === '"') {
          stream.next();
          while (!stream.eol()) {
            const ch = stream.next();
            if (ch === '\\') { stream.next(); continue; }
            if (ch === '"') break;
          }
          return 'string';
        }
        if (stream.match(/<[^>]*>/)) return 'string-2';
        if (stream.match(/@prefix\b/) || stream.match(/@base\b/)) return 'keyword';
        if (stream.match(/^(?:prefix|base)\b/i)) return 'keyword';
        if (stream.match(/@[a-zA-Z][a-zA-Z-]*/)) return 'meta';
        if (stream.match(/\^\^/)) return 'operator';
        if (stream.match(/^a(?=[\s.,;]|$)/)) return 'atom';
        if (stream.match(/^(?:true|false)\b/)) return 'atom';
        if (stream.match(/^-?\d+\.?\d*(?:[eE][+-]?\d+)?/)) return 'number';
        if (stream.match(/^[a-zA-Z_][\w-]*:[a-zA-Z_][\w.-]*/)) return 'variable-2';
        if (stream.match(/^[a-zA-Z_][\w-]*:/)) return 'def';
        if (stream.match(/^[.,;]/)) return 'punctuation';
        stream.next();
        return null;
      },
    };
  });
}
