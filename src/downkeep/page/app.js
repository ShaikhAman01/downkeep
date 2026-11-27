import { ask, sample, useKey } from "./ask.js";
import { showAuto } from "./auto.js";
import { allClear, proof, showClutter, showRecovered } from "./clutter.js";
import {
  again, app, bar, baseName, bdi, button, closed, ended, expired, folderIcon, go, h, human, list, main, message, panel, plural,
  small, stage, state, sum, tabs, toast, total, visit,
} from "./core.js";
import { lists, loadHistory, undo } from "./runs.js";
import { forgetPlan, showTidy } from "./tidy.js";

const SCREENS = { clutter: () => showClutter(), tidy: showTidy, auto: showAuto };
const TABS = [...tabs.children];
const tagline = document.querySelector("#tagline");
let fresh = null;

go.lists = lists;
go.undo = undo;
go.changed = forgetPlan;
go.focus = () => {
  panel.querySelector(".say").focus({ preventScroll: true });
  if (small.matches) main.scrollIntoView();
};

function pick(name) {
  state.tab = name;
  app.dataset.tab = name;
  for (const tab of TABS) {
    const on = tab.id === `tab-${name}`;
    tab.setAttribute("aria-selected", String(on));
    tab.tabIndex = on ? 0 : -1;
  }
  panel.setAttribute("role", "tabpanel");
  panel.setAttribute("aria-labelledby", `tab-${name}`);
}

go.tab = async (name, focus = true) => {
  const at = visit();
  pick(name);
  if (fresh) {
    list.replaceChildren();
    stage({ say: "Checking your files again", why: "Something moved, so the list is being brought up to date.", art: bar() });
    await fresh;
    if (state.visit !== at) return;
  }
  if (!state.report) return check();
  await SCREENS[name](focus);
  if (focus && state.tab === name && name === "clutter") go.focus();
};

/** After something moved: checks again behind the result that is on screen. */
go.refresh = () => {
  forgetPlan();
  again.disabled = true;
  if (state.tab === "clutter") list.replaceChildren(h("p", { class: "none" }, "Checking again"));
  const turn = (async () => {
    let report = null;
    try {
      report = await ask("/api/report");
      await loadHistory();
    } catch (error) {
      if (ended(error)) return;
    }
    if (fresh !== turn) return;
    state.report = report;
    state.checked = now();
    checkedAt = Date.now();
    if (report) {
      drawSum();
      if (state.tab === "clutter") showClutter(true);
    }
  })().finally(() => {
    if (fresh === turn) { fresh = null; again.disabled = false; }
  });
  fresh = turn;
};

let checkedAt = 0;
let looking = false;
const STALE_MS = 60_000;
const shape = (report) => JSON.stringify([report.files, report.bytes, report.duplicates, report.extracted]);

/** Back in the window after a while: checks quietly, and only says so if something changed. */
async function freshen() {
  if (sample || looking || fresh || state.acting || !state.report || app.classList.contains("plain")) return;
  if (document.visibilityState !== "visible" || Date.now() - checkedAt < STALE_MS) return;
  looking = true;
  let report;
  try {
    report = await ask("/api/report");
  } catch {
    return;
  } finally {
    looking = false;
    checkedAt = Date.now();
  }
  if (fresh || state.acting || shape(report) === shape(state.report)) return;
  const arrived = report.files > state.report.files;
  state.report = report;
  forgetPlan();
  drawSum();
  sum.append(h("p", { class: "fresh", role: "status" }, arrived ? "New files arrived: " : "This folder changed: ",
    button("text inline", "Check again", check)));
}

const now = () => new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

function drawSum() {
  const report = state.report;
  const { proven } = proof(report);
  const bytes = total(proven, "bytes");
  const parts = h("div", { class: "bar", "aria-hidden": "true" }, h("i"), h("i"));
  parts.children[0].style.flexGrow = bytes;
  parts.children[1].style.flexGrow = Math.max(0, report.bytes - bytes);
  sum.replaceChildren(...(proven.length
    ? [h("h1", { tabindex: "-1" }, `${human(bytes)} can go`), parts, h("p", {}, `Proven, out of ${human(report.bytes)} in ${plural(report.files, "file")}`)]
    : [h("h1", { tabindex: "-1" }, allClear(report) ? "All clear: no proven clutter" : report.files ? "Nothing proven" : "No files to check"), ...(report.files ? [h("p", {}, `${human(report.bytes)} in ${plural(report.files, "file")}`)] : [])]));
  tagline.hidden = state.history.length > 0;
  if (!sample) {
    sum.append(h("div", { class: "where" }, bdi(baseName(report.folder)),
      button("link", "Change folder", () => pickFolder(() => go.tab(state.tab)))));
  }
}

async function check() {
  again.disabled = true;
  visit();
  toast("");
  forgetPlan();
  message("Checking your files", ["Reading names, comparing files of the same size, and looking inside zips.", "Big folders can take a minute. Nothing is being changed."]);
  panel.querySelector(".farewell").append(bar());
  panel.setAttribute("aria-busy", "true");
  let report;
  try {
    state.system ??= await ask("/api/system");
    report = await ask("/api/report");
    if (state.system.tidy) await loadHistory();
  } catch (error) {
    if (!report) return failed(error);
  } finally {
    again.disabled = false;
    panel.setAttribute("aria-busy", "false");
  }
  state.report = report;
  state.checked = now();
  checkedAt = Date.now();
  app.classList.remove("plain");
  tabs.hidden = Boolean(sample) && !state.system.tidy && !state.system.trash;
  drawSum();
  again.hidden = Boolean(sample);
  pick(state.tab);
  const found = state.system.recovered;
  if (found && (found.done.length || found.skipped.length)) {
    state.system.recovered = null;
    showClutter(true);
    showRecovered(found, () => go.tab("clutter"));
    return;
  }
  await SCREENS[state.tab](false);
  sum.querySelector("h1")?.focus({ preventScroll: true });
}
go.check = check;

function failed(error) {
  if (ended(error)) return;
  again.hidden = true;
  const buttons = sample ? [] : [
    button("main-act", "Choose a folder", () => pickFolder(check)),
    button("next", "Try again", check),
  ];
  if (error.status === 409) message("Couldn't check this folder", [error.message, "Nothing was changed."], buttons);
  else message("Something went wrong", ["Downkeep couldn't finish checking. Nothing was changed."], buttons);
}

async function pickFolder(back, path = []) {
  let folders;
  const at = visit();
  try {
    ({ folders } = await ask("/api/folders", { path }));
  } catch (error) {
    if (!ended(error)) toast(error.message);
    return;
  }
  if (state.visit !== at) return;
  let picked = null;
  const here = path.at(-1);
  const checkIt = button("main-act", "", () => chooseFolder(picked ? [...path, picked] : path));
  const openIt = button("next", "", () => pickFolder(back, [...path, picked]));
  const cancel = button("next", "Cancel", back);
  const rows = folders.map((name) => h("button", {
    class: "folder", type: "button", "aria-pressed": "false",
    onclick: () => { picked = name; update(); },
    ondblclick: () => pickFolder(back, [...path, name]),
  }, folderIcon(), bdi(name)));

  function update() {
    rows.forEach((row, index) => row.setAttribute("aria-pressed", String(folders[index] === picked)));
    const target = picked ?? here;
    checkIt.hidden = !target;
    openIt.hidden = !picked;
    checkIt.replaceChildren("Check ", bdi(target ?? ""));
    openIt.replaceChildren("Open ", bdi(picked ?? ""));
  }

  const trail = ["Home", ...path].map((name, index) => index === path.length
    ? h("b", { "aria-current": "location" }, bdi(name))
    : h("button", { type: "button", onclick: () => pickFolder(back, path.slice(0, index)) }, bdi(name)));
  update();
  list.querySelectorAll(".item").forEach((item) => item.setAttribute("aria-current", "false"));
  stage({
    say: "Choose a folder to check",
    why: "Only folder names are read until you press Check. Nothing is changed.",
    art: h("div", { class: "picker hold" },
      h("nav", { class: "trail", "aria-label": "Folders above" }, ...trail),
      rows.length ? h("ul", {}, ...rows.map((row) => h("li", {}, row))) : h("p", { class: "why" }, "No folders inside this one.")),
    buttons: [checkIt, openIt, cancel],
    note: "Hidden folders, links, your whole home folder and other drives aren't available here.",
    focus: true,
  });
}

async function chooseFolder(path) {
  try {
    await ask("/api/choose", { path });
  } catch (error) {
    if (!ended(error)) toast(error.message);
    return;
  }
  fresh = null;
  await check();
}

async function stay() {
  try {
    await ask("/api/stay");
  } catch (error) {
    if (error.status) return expired();
  }
  closed();
}

async function start() {
  if (sample) return check();
  const code = decodeURIComponent(location.hash.slice(1));
  try {
    if (new URLSearchParams(location.search).get("window") === "app") sessionStorage.setItem("window", "app");
  } catch {
    // Without storage the window just stays open when Downkeep stops
  }
  history.replaceState(null, "", "/");
  try {
    if (code) {
      const session = await ask("/api/session", { code });
      sessionStorage.setItem("key", session.key);
      sessionStorage.setItem("version", session.version);
    }
  } catch (error) {
    return error.status ? expired() : closed();
  }
  const key = sessionStorage.getItem("key");
  state.version = sessionStorage.getItem("version") ?? "";
  if (!key) return expired();
  useKey(key);
  addEventListener("pageshow", (event) => { if (event.persisted) location.reload(); });
  stay();
  await check();
}

tabs.addEventListener("click", (event) => {
  const tab = event.target.closest("[role=tab]");
  if (tab) go.tab(tab.id.slice(4), false);
});
tabs.addEventListener("keydown", (event) => {
  const from = TABS.indexOf(document.activeElement);
  const to = { ArrowRight: from + 1, ArrowLeft: from - 1, Home: 0, End: TABS.length - 1 }[event.key];
  if (from < 0 || to === undefined) return;
  event.preventDefault();
  TABS[(to + TABS.length) % TABS.length].focus();
});
again.addEventListener("click", check);
addEventListener("focus", freshen);
document.addEventListener("visibilitychange", freshen);
start();
