const app = document.querySelector("#app");
const main = document.querySelector("#main");
const list = document.querySelector("#list");
const sum = document.querySelector("#sum");
const again = document.querySelector("#again");
const status = document.querySelector("#status");
const sample = document.documentElement.dataset.sample;
const SHOWN = 500;
const KINDS = {
  zip: "zip tar gz tar.gz tgz 7z rar xz bz2 zst",
  document: "pdf doc docx odt txt md rtf epub csv xls xlsx ods",
  image: "png jpg jpeg gif webp svg heic bmp tiff avif",
  media: "mp4 mkv mov webm avi mp3 wav flac ogg m4a",
  slides: "ppt pptx odp key",
};
const KIND = new Map(Object.entries(KINDS).flatMap(([kind, names]) => names.split(" ").map((name) => [name, kind])));

let key = null;
let hidden = 0;

const plural = (count, one, many = `${one}s`) => `${count.toLocaleString()} ${count === 1 ? one : many}`;
const capital = (text) => text[0].toUpperCase() + text.slice(1);
const clean = (text) => text.replace(/[\p{C}\p{Zl}\p{Zp}]/gu, "�");
const kindOf = (text) => KIND.get(text.split(".").at(-1).toLowerCase()) ?? "other";
const total = (items, field) => items.reduce((sum, item) => sum + item[field], 0);

function human(size) {
  if (size < 1000) return `${size} B`;
  let value = size;
  for (const unit of ["KB", "MB", "GB", "TB"]) {
    value /= 1000;
    if (value < 999.5 || unit === "TB") return `${value >= 99.95 ? Math.round(value) : value.toFixed(1).replace(".0", "")} ${unit}`;
  }
}

function days(count) {
  if (count < 1) return "Last changed today";
  if (count < 60) return `Last changed ${plural(count, "day")} ago`;
  if (count < 730) return `Last changed ${Math.round(count / 30.4)} months ago`;
  return `Last changed ${Math.floor(count / 365)} years ago`;
}

function fill(element, attributes, children) {
  for (const [name, value] of Object.entries(attributes)) {
    if (name === "onclick") element.addEventListener("click", value);
    else if (value != null) element.setAttribute(name, value);
  }
  element.append(...children.filter((child) => child != null));
  return element;
}

const h = (tag, attributes = {}, ...children) => fill(document.createElement(tag), attributes, children);
const v = (tag, attributes = {}, ...children) =>
  fill(document.createElementNS("http://www.w3.org/2000/svg", tag), attributes, children);

const bdi = (text) => h("bdi", { dir: "ltr" }, clean(text));

function icon(kind) {
  if (kind === "dots") {
    return v("svg", { width: 20, height: 22, viewBox: "0 0 20 22", class: "k-dots", "aria-hidden": "true" },
      ...[4, 10, 16].map((cx) => v("circle", { cx, cy: 11, r: 1.8 })));
  }
  return v("svg", { width: 20, height: 22, viewBox: "0 0 20 22", "aria-hidden": "true" },
    v("path", { d: "M3 1h9l6 6v12a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V2a1 1 0 0 1 1-1z", class: `k-${kind}` }),
    v("path", { d: "M12 1l6 6h-5a1 1 0 0 1-1-1z", class: "fold" }));
}

const label = (text, x, y, width, style) => v("text", { x, y, class: style, "data-width": width }, clean(text));

const tick = (x, y) => [
  v("circle", { cx: x, cy: y, r: 27, class: "front" }),
  v("path", { d: `M${x - 11} ${y + 1}l8 8 14-16`, class: "tick" }),
];

function picture(text, ...parts) {
  return v("svg", { viewBox: "0 0 560 320", role: "img", "aria-label": clean(text) }, ...parts.flat());
}

function sheet(x, text, note) {
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

function zipPicture(item, where) {
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

const lone = (text, note) => picture(text, v("g", { transform: "translate(-160 0)" }, ...folder(text, note, [])));

function copyPicture(item) {
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

const words = h("div", { "aria-live": "polite" });
const body = h("div", { class: "art" });
const acts = h("div", { class: "acts" });
const foot = h("p", { class: "foot" });

function stage({ say, what, why, art, buttons = [] }) {
  words.replaceChildren(
    h("h2", { class: "say" }, say),
    ...(what ? [h("p", { class: "what" }, ...what)] : []),
    ...(why ? [h("p", { class: "why" }, why)] : []),
  );
  body.className = art?.classList.contains("more") ? "wide" : "art";
  body.replaceChildren(...(art ? [art] : []));
  acts.hidden = !buttons.length;
  acts.replaceChildren(...buttons);
  if (!main.contains(words)) main.replaceChildren(words, body, acts, foot);
  main.scrollTop = 0;
  fit(body);
  rise(body);
}

function show(text) {
  if (sample) return null;
  return h("button", { class: "main-act", type: "button", "aria-label": `Show ${clean(text)} in its folder`, onclick: () => showFile(text) }, "Show in folder");
}

function facts(title, rows, note) {
  if (!rows.length) return null;
  const extra = rows.length - SHOWN;
  return h("section", {}, h("h3", {}, title), note ? h("p", {}, note) : null,
    h("ul", {}, ...rows.slice(0, SHOWN).map(([name, value]) => h("li", {}, bdi(name), h("span", {}, value)))),
    extra > 0 ? h("p", {}, `And ${extra.toLocaleString()} more.`) : null);
}

function moreAbout(report) {
  const { known, unknown } = report.sites;
  return h("div", { class: "more" }, ...[
    facts("Last changed", report.ages.filter((row) => row.files).map((row) => [capital(row.age), `${plural(row.files, "file")}, ${human(row.bytes)}`]), "Old is not proof of clutter."),
    facts("Probably came from", known.map((row) => [row.site, plural(row.files, "file")]), `A best guess from your browser's record. Only the site name is kept.${unknown.files ? ` ${plural(unknown.files, "file")} had none.` : ""}`),
    facts("Biggest", report.biggest.map((row) => [row.name, human(row.bytes)])),
    facts("Types", report.types.map((row) => [row.type, `${plural(row.files, "file")}, ${human(row.bytes)}`])),
    facts("Not counted", Object.entries(report.skipped).map(([reason, count]) => [capital(reason), count.toLocaleString()])),
  ].filter(Boolean));
}

function draw(report) {
  const home = report.folder.split(/[\\/]/).filter(Boolean).at(-1) ?? report.folder;
  const zips = report.extracted.map((item) => ({ ...item, zip: true }));
  const zipNames = new Set(zips.map((item) => item.name));
  const copies = report.duplicates
    .flatMap((group) => group.names.slice(1).map((name) => ({ name, keep: group.names[0], bytes: group.bytes_each })))
    .filter((item) => !zipNames.has(item.name));
  const proven = [...zips, ...copies];
  const provenNames = new Set(proven.map((item) => item.name));
  const kept = new Set(copies.map((item) => item.keep));
  const others = report.biggest.filter((item) => !provenNames.has(item.name)).slice(0, 5);
  const bytes = total(proven, "bytes");
  const shown = [...zips.slice(0, SHOWN), ...copies.slice(0, SHOWN)];
  const buttons = new Map();

  function open(item, move = false) {
    buttons.forEach((button, other) => button.setAttribute("aria-current", String(other === item)));
    const place = shown.indexOf(item);
    if (item.more) stage({ say: "More about this folder", what: [bdi(report.folder)], art: moreAbout(report) });
    else {
      const where = item.folder === "." ? home : item.folder;
      const next = () => {
        const target = shown[(place + 1) % shown.length];
        open(target);
        buttons.get(target).scrollIntoView({ block: "nearest" });
        acts.querySelector(".next").focus();
      };
      stage({
        say: item.zip
          ? item.files === 1 ? "The file in this zip is already in your folder" : `All ${item.files.toLocaleString()} files in this zip are already in your folder`
          : item.keep ? "This is an exact copy" : kept.has(item.name) ? "This is the one that stays" : "No proof, so this one stays",
        what: [bdi(item.name), `, ${human(item.bytes)}`],
        why: item.zip
          ? `${item.files === 1 ? "It" : "Each"} was found with the same name, size and checksum.`
          : item.keep ? "Same contents, checked from start to end." : kept.has(item.name) ? "Its identical copies are listed above." : `${days(item.days_old)}. Old or big is not proof of clutter.`,
        art: item.zip ? zipPicture(item, where) : item.keep ? copyPicture(item) : picture(item.name, sheet(185, item.name, "left alone")),
        buttons: [
          show(item.name),
          place >= 0 && shown.length > 1 ? h("button", { class: "next", type: "button", onclick: next }, "Next") : null,
          place >= 0 ? h("p", {}, `${(place + 1).toLocaleString()} of ${shown.length.toLocaleString()}`) : null,
        ].filter(Boolean),
      });
    }
    if (move && matchMedia("(max-width: 48rem)").matches) main.scrollIntoView();
  }

  function entry(item, kind, text, value) {
    const button = h("button", { class: "item", type: "button", title: clean(text), "aria-current": "false", onclick: () => open(item, true) },
      icon(kind), bdi(text), h("span", {}, value));
    buttons.set(item, button);
    return h("li", {}, button);
  }

  function group(title, value, items) {
    const extra = items.length - SHOWN;
    return [
      h("h2", {}, title, h("span", {}, value)),
      h("ul", {}, ...items.slice(0, SHOWN).map((item) => entry(item, kindOf(item.name), item.name, human(item.bytes)))),
      extra > 0 ? h("p", { class: "none" }, `And ${extra.toLocaleString()} more.`) : null,
    ];
  }

  const more = { more: true };
  const restFiles = report.files - proven.length;
  list.replaceChildren(...[
    ...(zips.length ? group("Already extracted", human(total(zips, "bytes")), zips) : []),
    ...(copies.length ? group("Exact copies", human(total(copies, "bytes")), copies) : []),
    ...(report.files ? group("Left alone", human(Math.max(0, report.bytes - bytes)), others) : []),
    restFiles > others.length ? h("p", { class: "none" }, `And ${plural(restFiles - others.length, "more file")}.`) : null,
    h("ul", {}, entry(more, "dots", "More about this folder", "")),
  ].filter(Boolean));

  const parts = h("div", { class: "bar", "aria-hidden": "true" }, h("i"), h("i"));
  parts.children[0].style.flexGrow = bytes;
  parts.children[1].style.flexGrow = Math.max(0, report.bytes - bytes);
  sum.replaceChildren(...(proven.length
    ? [h("h1", { tabindex: "-1" }, `${human(bytes)} can go`), parts, h("p", {}, `Proven, out of ${human(report.bytes)} in ${plural(report.files, "file")}`)]
    : [h("h1", { tabindex: "-1" }, report.files ? "Nothing proven" : "No files to check"), ...(report.files ? [h("p", {}, `${human(report.bytes)} in ${plural(report.files, "file")}`)] : [])]));

  const time = new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  foot.textContent = sample
    ? "This is made-up data. Nothing on your computer was read."
    : `Checked at ${time}. Nothing was moved, changed or deleted, and nothing left your computer.`;

  app.classList.remove("plain");
  if (proven.length) open(shown[0]);
  else {
    buttons.forEach((button) => button.setAttribute("aria-current", "false"));
    stage(report.files
      ? { say: "Nothing here is proven clutter", why: "There are no exact copies, and no zip whose every file was found. So everything stays.", art: lone(home, plural(report.files, "file")) }
      : { say: "There are no finished files here", why: "Hidden, empty and unfinished files are not counted.", art: lone(home, "nothing to check") });
  }
}

function message(title, ...lines) {
  app.classList.add("plain");
  list.replaceChildren();
  sum.replaceChildren();
  main.replaceChildren(h("h1", { class: "say", tabindex: "-1" }, title), ...lines.map((line) => h("p", { class: "why" }, clean(line))));
  main.append(h("div", { class: "art" }, lone("Downkeep", "")));
  main.querySelector("h1").focus({ preventScroll: true });
}

function toast(text) {
  status.textContent = text;
  clearTimeout(hidden);
  hidden = setTimeout(() => { status.textContent = ""; }, 4000);
}

async function ask(path, body = {}) {
  const headers = { "Content-Type": "application/json" };
  if (key) headers["X-Downkeep-Key"] = key;
  const answer = await fetch(path, { method: "POST", headers, body: JSON.stringify(body) });
  const value = await answer.json();
  if (!answer.ok) throw Object.assign(new Error(value.error), { status: answer.status });
  return value;
}

function closed() {
  again.hidden = true;
  message("Downkeep has closed", "Open it again to see your files. Nothing was changed.");
}

async function showFile(text) {
  try {
    await ask("/api/show", { name: text });
    toast("Opened your file manager");
  } catch (error) {
    if (error.status) toast(error.message);
    else closed();
  }
}

async function check() {
  again.disabled = true;
  status.textContent = "";
  message("Checking your files", "Reading names, comparing files of the same size, and looking inside zips.", "Big folders can take a minute. Nothing is being changed.");
  main.querySelector(".art").before(h("div", { class: "progress", "aria-hidden": "true" }));
  main.setAttribute("aria-busy", "true");
  let report;
  try {
    report = sample ? await (await fetch(sample)).json() : await ask("/api/report");
  } catch (error) {
    if (error.status === 403) return expired();
    if (error.status === 409) message("Couldn't check this folder", error.message);
    else if (error.status) message("Something went wrong", "Downkeep couldn't finish checking. Nothing was changed.");
    else return closed();
    again.hidden = false;
    return;
  } finally {
    again.disabled = false;
    main.setAttribute("aria-busy", "false");
  }
  draw(report);
  again.hidden = Boolean(sample);
  sum.querySelector("h1").focus({ preventScroll: true });
}

function expired() {
  again.hidden = true;
  message("This link has expired", "Open Downkeep again to get a fresh window. Each link works once, so nobody else can use it.");
}

async function stay() {
  try {
    const answer = await fetch("/api/stay", { method: "POST", headers: { "Content-Type": "application/json", "X-Downkeep-Key": key }, body: "{}" });
    if (!answer.ok) return expired();
    const reader = answer.body.getReader();
    while (!(await reader.read()).done);
  } catch {}
  closed();
}

async function start() {
  if (sample) return check();
  const code = decodeURIComponent(location.hash.slice(1));
  history.replaceState(null, "", "/");
  key = sessionStorage.getItem("key");
  try {
    if (code) key = (await ask("/api/session", { code })).key;
    if (!key) return expired();
    sessionStorage.setItem("key", key);
  } catch (error) {
    return error.status ? expired() : closed();
  }
  addEventListener("pageshow", (event) => { if (event.persisted) location.reload(); });
  stay();
  await check();
}

again.addEventListener("click", check);
start();
