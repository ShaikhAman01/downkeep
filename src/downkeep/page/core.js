import { ask } from "./ask.js";

export const app = document.querySelector("#app");
export const side = document.querySelector("#side");
export const main = document.querySelector("#main");
export const panel = document.querySelector("#stage");
export const list = document.querySelector("#list");
export const sum = document.querySelector("#sum");
export const tabs = document.querySelector("#tabs");
export const again = document.querySelector("#again");
const status = document.querySelector("#status");
const CHUNK = 300;
const DOUBLE = 400;
const KINDS = {
  zip: "zip tar gz tar.gz tgz 7z rar xz bz2 zst",
  document: "pdf doc docx odt txt md rtf epub csv xls xlsx ods",
  image: "png jpg jpeg gif webp svg heic bmp tiff avif",
  media: "mp4 mkv mov webm avi mp3 wav flac ogg m4a",
  slides: "ppt pptx odp key",
};
const KIND = new Map(Object.entries(KINDS).flatMap(([kind, names]) => names.split(" ").map((name) => [name, kind])));

export const state = { tab: "clutter", version: "", system: null, report: null, checked: null, history: [], moved: false, acting: false, visit: 0 };
// Set by app.js, so that the screens don't import each other
export const go = {};

export const plural = (count, one, many = `${one}s`) => `${count.toLocaleString()} ${count === 1 ? one : many}`;
export const capital = (text) => text.charAt(0).toUpperCase() + text.slice(1);
export const clean = (text) => text.replace(/[\p{C}\p{Zl}\p{Zp}]/gu, "�");
export const kindOf = (text) => KIND.get(text.split(".").at(-1).toLowerCase()) ?? "other";
export const total = (items, field) => items.reduce((sum, item) => sum + item[field], 0);
export const sentence = (text) => (text && !/[.!?]$/.test(text) ? `${text}.` : text);
export const baseName = (path) => path.split(/[\\/]/).filter(Boolean).at(-1) ?? path;
export const bin = () => (state.system?.trash && !state.system.restore ? "Recycle Bin" : "Trash");
export const small = matchMedia("(max-width: 48rem)");

export function human(size) {
  if (size < 1000) return `${size} B`;
  let value = size;
  for (const unit of ["KB", "MB", "GB", "TB"]) {
    value /= 1000;
    if (value < 999.5 || unit === "TB") return `${value >= 99.95 ? Math.round(value) : value.toFixed(1).replace(".0", "")} ${unit}`;
  }
}

export function moment(seconds) {
  const when = new Date(seconds * 1000);
  const day = (date) => new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const ago = Math.round((day(new Date()) - day(when)) / 86_400_000);
  const time = when.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (ago === 0) return `Today, ${time}`;
  if (ago === 1) return `Yesterday, ${time}`;
  if (ago > 1 && ago < 7) return `${when.toLocaleDateString([], { weekday: "long" })}, ${time}`;
  return `${when.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" })}, ${time}`;
}

function fill(element, attributes, children) {
  for (const [name, value] of Object.entries(attributes)) {
    if (name.startsWith("on")) element.addEventListener(name.slice(2), value);
    else if (value != null) element.setAttribute(name, value);
  }
  element.append(...children.filter((child) => child != null));
  return element;
}

export const h = (tag, attributes = {}, ...children) => fill(document.createElement(tag), attributes, children);
export const v = (tag, attributes = {}, ...children) =>
  fill(document.createElementNS("http://www.w3.org/2000/svg", tag), attributes, children);
export const bdi = (text) => h("bdi", { dir: "ltr" }, clean(text));
let shownAt = 0;

export function button(style, text, onclick, more = {}) {
  // The second click of a double click must not confirm what the first one opened
  const press = style === "main-act"
    ? (event) => { if (!event.detail || performance.now() - shownAt > DOUBLE) onclick(event); }
    : onclick;
  return h("button", { class: style, type: "button", onclick: press, ...more }, ...[text].flat());
}

export function icon(kind) {
  if (kind === "dots") {
    return v("svg", { width: 20, height: 22, viewBox: "0 0 20 22", class: "k-dots", "aria-hidden": "true" },
      ...[4, 10, 16].map((cx) => v("circle", { cx, cy: 11, r: 1.8 })));
  }
  return v("svg", { width: 20, height: 22, viewBox: "0 0 20 22", "aria-hidden": "true" },
    v("path", { d: "M3 1h9l6 6v12a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V2a1 1 0 0 1 1-1z", class: `k-${kind}` }),
    v("path", { d: "M12 1l6 6h-5a1 1 0 0 1-1-1z", class: "fold" }));
}

export const folderIcon = () => v("svg", { width: 22, height: 20, viewBox: "0 0 22 20", "aria-hidden": "true" },
  v("path", { d: "M2 4a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v2H2z", class: "back" }),
  v("path", { d: "M1 8.500a1.500 1.500 0 0 1 1.500-1.500h17a1.500 1.500 0 0 1 1.500 1.600l-.8 8a2 2 0 0 1-2 1.900H3.800a2 2 0 0 1-2-1.900z", class: "front" }));

const label = (text, x, y, width, style) => v("text", { x, y, class: style, "data-width": width }, clean(text));

const tick = (x, y) => [
  v("circle", { cx: x, cy: y, r: 27, class: "front" }),
  v("path", { d: `M${x - 11} ${y + 1}l8 8 14-16`, class: "tick" }),
];

export function picture(text, ...parts) {
  return v("svg", { viewBox: "0 0 560 320", role: "img", "aria-label": clean(text) }, ...parts.flat());
}

export function sheet(x, text, note) {
  const kind = kindOf(text);
  return v("g", { class: "rise" },
    v("path", { d: `M${x + 10} 30h150l30 30v200a10 10 0 0 1-10 10h-170a10 10 0 0 1-10-10v-220a10 10 0 0 1 10-10z`, class: "paper" }),
    v("path", { d: `M${x + 160} 30l30 30h-22a8 8 0 0 1-8-8z`, class: `k-${kind}`, "fill-opacity": 0.55 }),
    v("rect", { x: x + 18, y: 56, width: 40, height: 8, rx: 4, class: `k-${kind}` }),
    ...[154, 132, 145, 92].map((width, row) => v("rect", { x: x + 18, y: 82 + row * 16, width, height: 6, rx: 3, class: "rule" })),
    label(text, x + 18, 234, 154, "ink"),
    label(note, x + 18, 255, 154, "minor ink-soft"));
}

function folder(text, note, names) {
  return [
    v("path", { d: "M340 84a12 12 0 0 1 12-12h52l16 18h108a12 12 0 0 1 12 12v158a12 12 0 0 1-12 12H352a12 12 0 0 1-12-12z", class: "back" }),
    ...names.map((name, row) => v("g", { class: "rise" },
      v("rect", { x: 372 - row * 8, y: 44 + row * 34, width: 150 + row * 8, height: 120, rx: 7, class: "paper paper-edge" }),
      v("rect", { x: 384 - row * 8, y: 57 + row * 34, width: 10, height: 10, rx: 2, class: `k-${kindOf(name)}` }),
      label(name, 402 - row * 8, 67 + row * 34, 112 + row * 8, "minor ink"))),
    v("path", { d: "M330 176a12 12 0 0 1 12-13h196a12 12 0 0 1 12 13l-9 90a14 14 0 0 1-14 12H353a14 14 0 0 1-14-12z", class: "front" }),
    label(text, 358, 232, 166, "front-ink"),
    label(note, 358, 254, 166, "minor front-ink"),
  ];
}

export function zipPicture(item, where) {
  const teeth = Array.from({ length: 9 }, (_, row) => v("rect", { x: row % 2 ? 86 : 98, y: 40 + row * 13, width: 12, height: 8, rx: 2, class: "k-zip" }));
  return picture(`Every file in ${item.name} was found in ${where}`,
    v("g", { class: "rise" },
      v("path", { d: "M20 40a10 10 0 0 1 10-10h136a10 10 0 0 1 10 10v210a10 10 0 0 1-10 10H30a10 10 0 0 1-10-10z", class: "paper" }),
      v("rect", { x: 78, y: 30, width: 40, height: 136, class: "tape" }),
      ...teeth,
      v("rect", { x: 84, y: 156, width: 28, height: 36, rx: 6, class: "k-zip" }),
      v("rect", { x: 93, y: 172, width: 10, height: 13, rx: 3, class: "paper" }),
      label(item.name, 34, 224, 130, "ink"),
      label(plural(item.files, "file"), 34, 245, 130, "minor ink-soft")),
    v("path", { d: "M190 160h44M306 160h24", class: "link-dots" }),
    folder(where, `${item.files.toLocaleString()} of ${item.files.toLocaleString()} found`, item.examples.toReversed()),
    tick(270, 160));
}

export const lone = (text, note) => picture(text, v("g", { transform: "translate(-160 0)" }, ...folder(text, note, [])));

export function copyPicture(item) {
  return picture(`${item.name} has the same contents as ${item.keep}`,
    sheet(40, item.name, "the copy"),
    sheet(330, item.keep, "the one that stays"),
    v("path", { d: "M234 150h14M312 150h14", class: "link-dots" }),
    tick(280, 150));
}

function fit(root) {
  for (const text of root.querySelectorAll("text")) {
    const full = [...text.textContent];
    const width = Number(text.dataset.width);
    const tail = full.slice(-6);
    for (let count = full.length - 7; count > 0 && text.getComputedTextLength() > width; count -= 1) {
      text.textContent = [...full.slice(0, count), "…", ...tail].join("");
    }
  }
}

function rise(root) {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  root.querySelectorAll(".rise").forEach((part, row) => {
    part.animate(
      [{ transform: "translateY(14px)", opacity: 0 }, { transform: "none", opacity: 1 }],
      { duration: 220, delay: row * 40, easing: "cubic-bezier(0.23, 1, 0.32, 1)", fill: "backwards" },
    );
  });
}

const words = h("div", {});
const body = h("div", { class: "art" });
export const acts = h("div", { class: "acts" });
const work = h("div", { class: "work" }, body, acts);
const foot = h("p", { class: "foot" });

export function stage({ say, what, why, art, buttons = [], note = "", focus = false }) {
  shownAt = performance.now();
  words.setAttribute("aria-live", focus ? "off" : "polite");
  words.replaceChildren(
    h("h2", { class: "say", tabindex: "-1" }, say),
    ...(what ? [h("p", { class: "what" }, ...[what].flat())] : []),
    ...(why ? [h("p", { class: "why" }, ...[why].flat())] : []),
  );
  body.className = art instanceof SVGElement ? "art" : "wide";
  foot.textContent = note;
  foot.hidden = !note;
  body.replaceChildren(...(art ? [art] : []));
  acts.hidden = !buttons.length;
  acts.className = body.className === "wide" && art?.matches(".hold") ? "acts held" : "acts";
  acts.replaceChildren(...buttons);
  if (!panel.contains(words)) panel.replaceChildren(words, work, foot);
  panel.scrollTop = 0;
  fit(body);
  rise(body);
  if (focus) {
    words.querySelector("h2").focus({ preventScroll: true });
    if (small.matches) main.scrollIntoView();
  }
}

/** Changes the words of the screen that is showing, without redrawing it. */
export function restage(say, what) {
  words.setAttribute("aria-live", "polite");
  words.querySelector(".say").textContent = say;
  const line = words.querySelector(".what");
  if (line && what) line.textContent = what;
}

export function message(title, lines, buttons = []) {
  app.classList.add("plain");
  tabs.hidden = true;
  panel.removeAttribute("role");
  list.replaceChildren();
  sum.replaceChildren();
  panel.replaceChildren(h("h1", { class: "say", tabindex: "-1" }, title), ...lines.map((line) => h("p", { class: "why" }, clean(line))));
  if (buttons.length) panel.append(h("div", { class: "acts" }, ...buttons));
  panel.append(h("div", { class: "art" }, lone("Downkeep", "")));
  if (state.version) panel.append(h("p", { class: "foot" }, `Downkeep ${state.version}`));
  panel.querySelector("h1").focus({ preventScroll: true });
}

let hidden = 0;

export function toast(text) {
  status.textContent = text;
  clearTimeout(hidden);
  hidden = setTimeout(() => { status.textContent = ""; }, 4000);
}

let over = false;

export function closed() {
  if (over) return;
  over = true;
  again.hidden = true;
  const last = state.acting
    ? "Files were being moved. Open it again: it checks where each one is, and nothing is deleted."
    : "Open it again to see your files. Nothing else will change.";
  hold(false);
  message("Downkeep has closed", [last]);
}

export function expired() {
  again.hidden = true;
  hold(false);
  message("This link has expired", ["Open Downkeep again to get a fresh window. Each link works once, so nobody else can use it."]);
}

/** Handles the two ends of a session. Anything else is for the screen to show. */
export function ended(error) {
  if (!error.status) closed();
  else if (error.status === 403) expired();
  return !error.status || error.status === 403;
}

export const bar = () => h("div", { class: "progress", "aria-hidden": "true" });

/** Nothing else can be pressed while files are being moved. */
export function hold(on) {
  state.acting = on;
  for (const part of [side, list]) part.inert = on;
  panel.setAttribute("aria-busy", String(on));
}

export async function act(say, path, body, retry, back) {
  hold(true);
  stage({ say, why: "Each file is checked again right before it moves. You can close this window, the work still finishes.", art: bar(), focus: true });
  try {
    const result = await ask(path, body);
    hold(false);
    state.moved ||= result.done.length > 0;
    return result;
  } catch (error) {
    if (ended(error)) return null;
    hold(false);
    trouble(error, retry, back);
    return null;
  }
}

export function trouble(error, retry, back, backText = "Back") {
  stage({
    say: "That didn't work",
    why: sentence(clean(error.message)),
    art: h("div", {}),
    buttons: [
      retry ? button("main-act", "Try again", retry) : null,
      back ? button("next", backText, back) : null,
    ].filter(Boolean),
    focus: true,
  });
}

export function loading(say, why) {
  stage({ say, why, art: bar() });
  state.visit += 1;
  return state.visit;
}

export const visit = () => { state.visit += 1; return state.visit; };

export function facts(title, rows, note) {
  if (!rows.length) return null;
  const items = h("ul", {});
  const more = h("p", {});
  let drawn = 0;
  function draw() {
    items.append(...rows.slice(drawn, drawn + CHUNK).map(([name, value]) => h("li", {}, bdi(name), h("span", {}, value ? clean(value) : ""))));
    drawn = Math.min(rows.length, drawn + CHUNK);
    more.replaceChildren(...(drawn < rows.length ? moreButton(drawn, rows.length, () => { draw(); more.querySelector("button")?.focus(); }) : []));
    more.hidden = drawn >= rows.length;
  }
  draw();
  return h("section", {}, h("h3", {}, title), note ? h("p", {}, note) : null, items, more);
}

const moreButton = (drawn, count, draw, extra = "") => [
  `Showing ${drawn.toLocaleString()} of ${count.toLocaleString()}. ${extra}`,
  button("text", `Show ${Math.min(CHUNK, count - drawn).toLocaleString()} more`, draw),
];

/** Rows with ticks. `off` holds the names that are not ticked, also for rows that are not drawn yet. */
export function ticks(rows, off, { name, note, changed, title }) {
  const items = h("ul", { class: "ticks" });
  const more = h("p", { class: "more-rows" });
  const boxes = [];
  let drawn = 0;

  function row(item) {
    const text = h("span", { class: "note" });
    const box = h("input", { type: "checkbox" });
    const update = () => {
      box.checked = !off.has(name(item));
      text.textContent = clean(note(item, box.checked));
    };
    box.addEventListener("change", () => {
      box.checked ? off.delete(name(item)) : off.add(name(item));
      update();
      changed();
    });
    update();
    boxes.push(update);
    return h("li", {}, h("label", {}, box, icon(kindOf(name(item))), bdi(name(item)), text));
  }

  function draw() {
    const next = rows.slice(drawn, drawn + CHUNK).map(row);
    items.append(...next);
    drawn += next.length;
    more.hidden = drawn >= rows.length;
    more.replaceChildren(...(more.hidden ? [] : moreButton(drawn, rows.length, () => { draw(); next.at(-1)?.nextSibling?.querySelector("input").focus(); }, "Ticks cover all of them. ")));
  }

  function all(on) {
    for (const item of rows) on ? off.delete(name(item)) : off.add(name(item));
    boxes.forEach((update) => update());
    changed();
  }

  draw();
  return h("div", { class: "hold" },
    h("div", { class: "tools" }, title ?? h("span", {}), h("span", {},
      button("text", "Select all", () => all(true)), button("text", "Select none", () => all(false)))),
    items, more);
}

export function switchButton(on, name, change) {
  return h("button", {
    class: "switch", type: "button", role: "switch", "aria-checked": String(on), "aria-label": clean(name),
    onclick: (event) => change(event.currentTarget.getAttribute("aria-checked") !== "true", event.currentTarget),
  }, h("i", { "aria-hidden": "true" }));
}

export function entry(art, text, value, onclick) {
  return h("button", { class: "item", type: "button", title: clean(text), "aria-current": "false", onclick }, art, bdi(text), h("span", {}, value));
}

export const mark = (current) => list.querySelectorAll(".item").forEach((item) => item.setAttribute("aria-current", String(item === current)));
