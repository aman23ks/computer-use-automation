(() => {
  if (window.__cua) return;

  const BLOCK = new Set(["address", "article", "aside", "blockquote", "center", "dd", "div", "dl", "dt", "fieldset",
    "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
    "section", "table", "tbody", "thead", "tfoot", "tr", "ul"]);
  const SKIP = new Set(["script", "style", "noscript", "head", "template", "meta", "link"]);
  const CONTROL_SEL = "input:not([type=hidden]),select,textarea,button,a[href],a[onclick],[onclick],[role=button],[role=link],[tabindex]";

  const norm = (s) => (s || "").replace(/[\s ]+/g, " ").trim();
  const key = (s) => norm(s).replace(/[:*\s]+$/, "").trim().toLowerCase();
  const textOf = (el) => norm(el.innerText !== undefined ? el.innerText : el.textContent);

  function styleHidden(el) {
    const st = el.ownerDocument.defaultView.getComputedStyle(el);
    return st.display === "none" || st.visibility === "hidden";
  }

  function isVisible(el) {
    if (!el.isConnected || styleHidden(el)) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  }

  function roleOf(el) {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit.split(/\s+/)[0];
    const tag = el.tagName.toLowerCase();
    switch (tag) {
      case "a": return el.hasAttribute("href") || el.hasAttribute("onclick") ? "link" : "generic";
      case "button": return "button";
      case "select": return el.multiple || el.size > 1 ? "listbox" : "combobox";
      case "textarea": return "textbox";
      case "input": {
        const t = (el.getAttribute("type") || "text").toLowerCase();
        if (["button", "submit", "reset", "image"].includes(t)) return "button";
        if (t === "checkbox" || t === "radio") return t;
        if (t === "hidden") return "none";
        return "textbox";
      }
      case "td": return "cell";
      case "th": return "columnheader";
      case "img": return "img";
      case "h1": case "h2": case "h3": case "h4": case "h5": case "h6": return "heading";
      default: return el.hasAttribute("onclick") ? "button" : "generic";
    }
  }

  function accName(el) {
    const doc = el.ownerDocument;
    const lb = el.getAttribute("aria-labelledby");
    if (lb) {
      const t = norm(lb.split(/\s+/).map((id) => { const n = doc.getElementById(id); return n ? textOf(n) : ""; }).join(" "));
      if (t) return t;
    }
    const al = norm(el.getAttribute("aria-label"));
    if (al) return al;
    const tag = el.tagName.toLowerCase();
    if (tag === "input" || tag === "select" || tag === "textarea") {
      const t = (el.getAttribute("type") || "").toLowerCase();
      if (tag === "input" && ["button", "submit", "reset"].includes(t)) {
        return norm(el.value) || (t === "submit" ? "Submit" : t === "reset" ? "Reset" : "");
      }
      if (tag === "input" && t === "image") return norm(el.getAttribute("alt") || el.value);
      if (el.id) {
        const l = doc.querySelector(`label[for="${CSS.escape(el.id)}"]`);
        if (l) return textOf(l);
      }
      const wrap = el.closest("label");
      if (wrap) return textOf(wrap);
      return norm(el.getAttribute("title") || el.getAttribute("placeholder"));
    }
    if (tag === "img") return norm(el.getAttribute("alt") || el.getAttribute("title"));
    const role = roleOf(el);
    if (["link", "button", "cell", "columnheader", "heading", "option", "tab", "menuitem"].includes(role)) {
      return textOf(el).slice(0, 200) || norm(el.getAttribute("title"));
    }
    return norm(el.getAttribute("title"));
  }

  function hasControl(el) {
    return !!el.querySelector("input:not([type=hidden]),select,textarea,button");
  }

  function proximityLabel(el) {
    const cell = el.closest("td,th");
    if (cell) {
      for (let prev = cell.previousElementSibling; prev; prev = prev.previousElementSibling) {
        const t = textOf(prev);
        if (t && !hasControl(prev)) return t.slice(0, 80);
      }
    }
    if (el.tagName === "TD" || el.tagName === "TH") return "";
    for (let n = el.previousSibling; n; n = n.previousSibling) {
      if (n.nodeType === 3 && norm(n.textContent)) return norm(n.textContent).slice(0, 80);
      if (n.nodeType === 1) {
        if (n.matches("input,select,textarea,button")) break;
        const t = textOf(n);
        if (t) return t.slice(0, 80);
      }
    }
    return "";
  }

  function isHeaderRow(row) {
    const cells = Array.from(row.cells);
    return cells.length > 1 && cells.every((c) => c.tagName === "TH" || c.querySelector("b,strong"));
  }

  function tableContext(el) {
    const cell = el.closest("td,th");
    if (!cell) return null;
    const row = cell.parentElement;
    const table = row.closest("table");
    const rows = Array.from(table.rows);
    if (rows.length < 2 || !isHeaderRow(rows[0]) || row === rows[0]) return null;
    const headers = Array.from(rows[0].cells).map((c) => textOf(c));
    if (headers.length !== row.cells.length) return null;
    return { table, rows, row, headers, colIdx: cell.cellIndex, cell };
  }

  function isInteractive(el) {
    if (!el.matches(CONTROL_SEL)) return false;
    if (el.tagName === "TD" || el.tagName === "TR" || el.tagName === "TABLE" || el.tagName === "BODY") return false;
    if (el.hasAttribute("tabindex") && !el.matches("input,select,textarea,button,a") && el.getAttribute("tabindex") === "-1") return false;
    return true;
  }

  function cssPath(el) {
    const doc = el.ownerDocument;
    const parts = [];
    for (let n = el; n && n.nodeType === 1 && n !== doc.body && n !== doc.documentElement; n = n.parentElement) {
      let i = 1;
      for (let s = n.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === n.tagName) i++;
      parts.unshift(`${n.tagName.toLowerCase()}:nth-of-type(${i})`);
    }
    return "body > " + parts.join(" > ");
  }

  function allVisible(pred) {
    return Array.from(document.querySelectorAll("body *")).filter((el) => pred(el) && isVisible(el));
  }

  function resolve(s) {
    switch (s.kind) {
      case "field_name":
        return Array.from(document.querySelectorAll(`${s.tag}[name="${CSS.escape(s.name)}"]`)).filter(isVisible);
      case "role_name":
        return allVisible((el) => roleOf(el) === s.role && key(accName(el)) === key(s.name));
      case "label":
        return allVisible((el) => roleOf(el) === s.role && key(proximityLabel(el)) === key(s.label));
      case "text":
        return allVisible((el) => roleOf(el) === s.role && key(textOf(el)) === key(s.text));
      case "css": {
        try { return Array.from(document.querySelectorAll(s.selector)).filter(isVisible); } catch (e) { return []; }
      }
      case "table_cell": {
        const out = [];
        for (const table of document.querySelectorAll("table")) {
          const rows = Array.from(table.rows);
          if (rows.length < 2 || !isHeaderRow(rows[0])) continue;
          const headers = Array.from(rows[0].cells).map((c) => key(textOf(c)));
          const col = headers.indexOf(key(s.column));
          const kcol = headers.indexOf(key(s.row_key_column));
          if (col < 0 || kcol < 0) continue;
          for (const row of rows.slice(1)) {
            if (row.cells.length !== headers.length || key(textOf(row.cells[kcol])) !== key(s.row_key)) continue;
            const cell = row.cells[col];
            if (!s.inner_role) { if (isVisible(cell)) out.push(cell); continue; }
            for (const d of cell.querySelectorAll("*")) if (roleOf(d) === s.inner_role && isVisible(d)) out.push(d);
          }
        }
        return out;
      }
      default:
        return [];
    }
  }

  function candidates(el) {
    const out = [];
    const role = roleOf(el);
    const tag = el.tagName.toLowerCase();
    const add = (strategy) => {
      const r = resolve(strategy);
      out.push({ strategy, count: r.length, matches: r.length === 1 && r[0] === el });
    };
    const fieldName = el.getAttribute("name");
    if (fieldName && ["input", "select", "textarea", "button"].includes(tag)) add({ kind: "field_name", tag, name: fieldName });
    const name = accName(el);
    if (name) add({ kind: "role_name", role, name });
    const ctx = tableContext(el);
    const label = ctx ? "" : proximityLabel(el);
    if (label) add({ kind: "label", role, label });
    if (ctx) {
      const column = ctx.headers[ctx.colIdx];
      for (let j = 0; j < ctx.headers.length; j++) {
        if (j === ctx.colIdx || !ctx.headers[j]) continue;
        const keyText = textOf(ctx.row.cells[j]);
        if (!keyText) continue;
        const dupes = ctx.rows.slice(1).filter((r) => r.cells[j] && key(textOf(r.cells[j])) === key(keyText)).length;
        if (dupes !== 1) continue;
        add({ kind: "table_cell", column, row_key_column: ctx.headers[j], row_key: keyText, inner_role: el === ctx.cell ? null : role });
      }
    }
    add({ kind: "css", selector: cssPath(el) });
    return out;
  }

  function info(el, ref) {
    const r = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    const d = {
      ref, role: roleOf(el), name: accName(el), label: tableContext(el) ? "" : proximityLabel(el), tag, type,
      field: el.getAttribute("name") || "", disabled: !!el.disabled,
      bbox: { x: r.x, y: r.y, width: r.width, height: r.height },
    };
    const form = el.form || el.closest("form");
    d.form = form ? (form.getAttribute("method") || "get").toLowerCase() : null;
    d.submits = (tag === "button" && (type === "" || type === "submit")) || (tag === "input" && (type === "submit" || type === "image"));
    if (tag === "input" || tag === "textarea") {
      d.secret = type === "password";
      d.value = d.secret ? (el.value ? "<masked>" : "") : el.value;
      if (type === "checkbox" || type === "radio") d.checked = el.checked;
    }
    if (tag === "select") {
      d.options = Array.from(el.options).map((o) => norm(o.text));
      d.selected = el.selectedIndex >= 0 ? norm(el.options[el.selectedIndex].text) : "";
    }
    return d;
  }

  const state = { refs: new Map(), masks: [] };

  function token(d) {
    const parts = [`${d.ref} ${d.role}`];
    if (d.name) parts.push(JSON.stringify(d.name));
    if (d.label && d.label !== d.name) parts.push(`label=${JSON.stringify(d.label)}`);
    if (d.field) parts.push(`field=${d.field}`);
    if (d.secret) parts.push("password");
    if (d.value !== undefined) parts.push(`value=${JSON.stringify(d.value)}`);
    if (d.options) parts.push(`selected=${JSON.stringify(d.selected)} options=${JSON.stringify(d.options)}`);
    if (d.checked !== undefined) parts.push(`checked=${d.checked}`);
    if (d.disabled) parts.push("disabled");
    return `[${parts.join(" ")}]`;
  }

  function sensitiveCell(c, labelRx) {
    if (!labelRx) return false;
    const table = c.closest("table");
    if (table && table.rows.length > 1 && table.rows[0] === c.parentElement && isHeaderRow(c.parentElement)) return false;
    const ctx = tableContext(c);
    if (ctx) return labelRx.test(ctx.headers[ctx.colIdx] || "");
    return labelRx.test(proximityLabel(c));
  }

  function snapshot(start, sensitiveLabels) {
    const labelRx = sensitiveLabels ? new RegExp(sensitiveLabels, "i") : null;
    state.refs = new Map();
    let n = start;
    const elements = [];
    const lines = [];
    let cur = "";
    const flush = () => { const t = norm(cur); if (t) lines.push(t); cur = ""; };
    const register = (el) => {
      const ref = `e${n++}`;
      state.refs.set(ref, el);
      const d = info(el, ref);
      elements.push(d);
      return d;
    };
    const walk = (node) => {
      if (node.nodeType === 3) { cur += node.textContent; return; }
      if (node.nodeType !== 1) return;
      const el = node;
      const tag = el.tagName.toLowerCase();
      if (SKIP.has(tag) || styleHidden(el)) return;
      if (tag === "input" && (el.getAttribute("type") || "").toLowerCase() === "hidden") return;
      if (isInteractive(el)) {
        if (isVisible(el)) cur += ` ${token(register(el))} `;
        return;
      }
      if (tag === "tr" && !el.querySelector("table")) {
        flush();
        const cells = Array.from(el.cells).map((c) => {
          if (c.querySelector(CONTROL_SEL)) {
            const saved = cur; cur = "";
            for (const ch of c.childNodes) walk(ch);
            const t = norm(cur); cur = saved; return t;
          }
          const t = textOf(c);
          if (!t) return "";
          return `[${register(c).ref}]${sensitiveCell(c, labelRx) ? "[MASKED]" : t}`;
        });
        lines.push(`| ${cells.join(" | ")} |`);
        return;
      }
      if (tag === "br") { flush(); return; }
      if (labelRx && !el.children.length && !el.closest("td,th") && labelRx.test(proximityLabel(el))) {
        cur += " [MASKED] ";
        return;
      }
      const block = BLOCK.has(tag);
      if (block) flush();
      for (const ch of el.childNodes) walk(ch);
      if (block) flush();
    };
    if (document.body) walk(document.body);
    flush();
    return { url: location.href, title: document.title, lines, elements, landmarks: landmarks(), next: n };
  }

  function landmarks() {
    const out = [];
    for (const el of document.querySelectorAll("body *")) {
      if (out.length >= 5) break;
      if (el.children.length > 0 && !["B", "STRONG", "FONT", "H1", "H2", "H3"].includes(el.tagName)) continue;
      const t = textOf(el);
      if (!t || t.length > 60 || !isVisible(el)) continue;
      const size = parseFloat(el.ownerDocument.defaultView.getComputedStyle(el).fontSize);
      if (/^H[1-3]$/.test(el.tagName) || size >= 17) if (!out.includes(t)) out.push(t);
    }
    return out;
  }

  function checkText(c) {
    const body = document.body ? norm(document.body.innerText) : "";
    const hit = c.regex ? new RegExp(c.regex, "i").test(body) : body.toLowerCase().includes(norm(c.text).toLowerCase());
    return c.present === false ? !hit : hit;
  }

  function matchText(regex) {
    const body = document.body ? norm(document.body.innerText) : "";
    const m = body.match(new RegExp(regex, "i"));
    return m ? m[0] : null;
  }

  function describeElement(el) {
    const ref = `h${state.refs.size + 1000}`;
    state.refs.set(ref, el);
    return { info: info(el, ref), candidates: candidates(el) };
  }

  function actionable(el) {
    for (let n = el; n && n !== document.body; n = n.parentElement) {
      if (isInteractive(n)) return n;
    }
    const cell = el.closest && el.closest("td,th");
    return cell || el;
  }

  function markSensitive(patterns, values, sensitiveLabels) {
    clearMasks();
    const labelRx = sensitiveLabels ? new RegExp(sensitiveLabels, "i") : null;
    for (const c of document.querySelectorAll("td,th")) if (textOf(c) && sensitiveCell(c, labelRx)) state.masks.push(c);
    if (labelRx) {
      for (const el of document.querySelectorAll("body *")) {
        if (!el.children.length && !el.closest("td,th") && textOf(el) && labelRx.test(proximityLabel(el))) state.masks.push(el);
      }
    }
    const regs = patterns.map((p) => new RegExp(p));
    const hit = (t) => t && (regs.some((r) => r.test(t)) || values.some((v) => v && t.includes(v)));
    const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (hit(node.textContent) && node.parentElement) state.masks.push(node.parentElement);
    }
    for (const input of document.querySelectorAll("input,textarea")) {
      if (input.type === "password" || hit(input.value)) state.masks.push(input);
    }
    for (const el of state.masks) el.setAttribute("data-cua-mask", "1");
    return state.masks.length;
  }

  function clearMasks() {
    for (const el of state.masks) el.removeAttribute("data-cua-mask");
    state.masks = [];
  }

  function recordHuman(ev) {
    if (typeof window.__cuaHuman !== "function" || !ev.isTrusted) return;
    const el = actionable(ev.target);
    const entry = { type: ev.type, at: Date.now(), ...describeElement(el) };
    if (ev.type === "change") {
      entry.value = el.type === "password" ? null : el.tagName === "SELECT" ? norm(el.options[el.selectedIndex]?.text) : el.value;
    }
    window.__cuaHuman(entry);
  }
  document.addEventListener("click", recordHuman, true);
  document.addEventListener("change", recordHuman, true);

  window.__cua = {
    snapshot,
    resolve,
    candidates,
    checkText,
    matchText,
    markSensitive,
    clearMasks,
    byRef: (ref) => state.refs.get(ref) || null,
    describeRef: (ref) => { const el = state.refs.get(ref); return el ? { info: info(el, ref), candidates: candidates(el) } : null; },
    describeAt: (x, y) => { const el = document.elementFromPoint(x, y); return el ? describeElement(actionable(el)) : null; },
    read: (el, attribute) => (attribute === "value" ? el.value : textOf(el)),
    roleOf,
    landmarks,
    describeHandle: (el) => describeElement(el),
    describeActive: () => {
      const el = document.activeElement;
      return el && el !== document.body && document.hasFocus() && !/^I?FRAME$/.test(el.tagName) ? describeElement(el) : null;
    },
  };
})();
