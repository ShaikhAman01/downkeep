import { ask } from "./ask.js";
import { baseName, bdi, button, clean, ended, enter, go, h, list, loading, panel, plural, sentence, stage, state, switchButton, trouble, visit } from "./core.js";
import { loadRules } from "./rules.js";
import { listRuns, loadHistory, place, showHistory, undo } from "./runs.js";

const DAYS = [0, 1, 2, 3, 7, 14, 30];
const after = (days) => (days === 0 ? "right away" : `after ${plural(days, "day")}`);
const NEVER = "Trash is never automatic. Anything unclear stays in Downloads.";
const SLIDE = 170;
const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
const pause = (ms) => new Promise((done) => { setTimeout(done, reduced() ? 0 : ms); });

/** `quiet` keeps the screen that is showing until the new one is ready, so nothing flashes. */
export async function showAuto(focus = false, { quiet = false, arriving = false } = {}) {
  const at = quiet ? visit() : loading("Looking at what happens by itself", "This only reads your choices.");
  if (!quiet) list.replaceChildren();
  let status;
  let rules = [];
  try {
    status = await ask("/api/auto");
    if (status.available || status.on) {
      ({ rules } = await loadRules());
      await loadHistory();
    }
  } catch (error) {
    if (!ended(error) && state.visit === at) trouble(error, () => showAuto(true), () => go.tab("clutter"), "Back to Clutter");
    return;
  }
  if (state.visit !== at) return;
  draw(status, rules, focus, arriving);
}

function draw(status, rules, focus, arriving = false) {
  visit();
  if (!status.available && !status.on) {
    stage({
      say: "Automatic tidying isn't available here",
      why: sentence(clean(status.why || "It isn't available on this system yet")),
      art: h("div", {}),
      note: "You can still press Tidy yourself.",
      focus,
    });
    return;
  }
  const runs = state.history.filter((run) => run.kind === "auto");
  listRuns(runs, "Last tidied", "auto", {
    none: "Nothing has been tidied by itself yet.",
    extra: runs.length === state.history.length ? null : h("ul", {}, h("li", {}, button("item plain-item", "All history", () => showHistory("auto")))),
  });

  let shown = rules;
  const problem = h("p", { class: "problem", role: "alert" });
  const elsewhere = status.on && status.folder && status.folder !== state.report.folder;

  const back = (rules = shown) => draw(status, rules, true);

  async function master(on, control) {
    control.setAttribute("aria-checked", String(on));
    if (on) {
      // The knob finishes its slide before the next step comes in
      await pause(SLIDE);
      return preview(shown, back);
    }
    control.disabled = true;
    apply(false);
    problem.textContent = "";
    try {
      status = await ask("/api/auto/set", { on: false });
    } catch (error) {
      control.setAttribute("aria-checked", "true");
      control.disabled = false;
      apply(true);
      if (!ended(error)) problem.textContent = sentence(clean(error.message));
      return;
    }
    control.disabled = false;
    if (elsewhere) return showAuto(true, { quiet: true });
    idle();
    const why = panel.querySelector(".why");
    if (why) why.textContent = intro(status, false);
    panel.querySelector(".what")?.remove();
  }

  async function change(rule, changes, control, undoChange) {
    shown = await save(shown, rule, changes, control, undoChange, problem);
    idle();
  }

  const note = h("p", { class: "soft" });
  const idle = () => {
    note.textContent = state_(status, shown, elsewhere);
  };

  function row(rule) {
    const [item, ...parts] = folderRow(rule, change);
    controls.push(...parts);
    return item;
  }

  idle();
  const usable = shown.filter((rule) => rule.on);
  const controls = [];
  const waiting = h("p", { class: "soft waiting", id: "autos-waiting" }, "These folders are used once you turn automatic tidying on");
  const folders = h("ul", { class: "folders" }, ...usable.map(row));

  /** While it is off the folders wait: faded, and not changeable until it is on. */
  function apply(on) {
    folders.setAttribute("class", on ? "folders" : "folders dim");
    if (on) folders.removeAttribute("aria-describedby");
    else folders.setAttribute("aria-describedby", "autos-waiting");
    // The line keeps its place either way, so the list never jumps
    waiting.textContent = on ? "New files that match go to these folders." : "These folders are used once you turn automatic tidying on";
    waiting.hidden = !usable.length;
    for (const control of controls) {
      control.disabled = !on;
      control.setAttribute("aria-disabled", String(!on));
    }
  }
  apply(status.on && !arriving);
  if (arriving) waiting.textContent = "New files that match go to these folders.";
  const last = runs.find((run) => run.can_undo && !run.undone);
  stage({
    say: "Tidy new files for me",
    what: status.note ? clean(status.note) : null,
    why: intro(status, elsewhere),
    art: h("div", { class: "hold autos" },
      h("ul", {},
        h("li", { class: "master" },
          h("div", {}, h("b", {}, "Automatic tidying"), note),
          switchButton(status.on, "Automatic tidying", master))),
      waiting,
      folders,
      problem),
    buttons: [
      elsewhere ? button("main-act", "Use this folder instead", () => preview(shown, back)) : null,
      last ? button("next", "Undo last run", () => undo(last.batch, "auto")) : null,
      button("next", "History", () => showHistory("auto")),
    ].filter(Boolean),
    note: NEVER,
    focus,
  });
  if (arriving) {
    enter();
    setTimeout(() => {
      apply(status.on);
      folders.classList.add("waking");
      setTimeout(() => folders.classList.remove("waking"), 400);
    }, reduced() ? 0 : 120);
  }
}

/** Saves one folder's change and gives back every folder as stored, or as it was when that fails. */
async function save(rules, rule, changes, control, undoChange, problem) {
  control.disabled = true;
  problem.textContent = "";
  const next = rules.map((other) => (other.id === rule.id ? { ...other, ...changes } : other));
  try {
    ({ rules } = await ask("/api/rules/save", { rules: next }));
    go.changed();
  } catch (error) {
    undoChange();
    if (!ended(error)) problem.textContent = sentence(clean(error.message));
  }
  control.disabled = false;
  control.focus();
  return rules;
}

/** One folder: when its new files move, and whether they move at all. Gives back the row and its two controls. */
function folderRow(rule, change) {
  const id = `days-${rule.id}`;
  const choice = h("select", { id, "aria-label": `When to move new files to ${clean(rule.folder)}` },
    ...[...new Set([...DAYS, rule.days])].sort((a, b) => a - b).map((days) => h("option", { value: days }, after(days))));
  choice.value = String(rule.days);
  let saved = rule.days;
  choice.addEventListener("change", () => change(rule, { days: Number(choice.value) }, choice, () => { choice.value = String(saved); })
    .then(() => { saved = Number(choice.value); }));
  const control = switchButton(rule.auto, `Move new files to ${rule.folder}`, (on) => {
    control.setAttribute("aria-checked", String(on));
    change(rule, { auto: on }, control, () => control.setAttribute("aria-checked", String(!on)));
  });
  const item = h("li", {}, h("div", {}, h("b", {}, bdi(rule.folder)), h("p", { class: "soft" }, "to ", bdi(place(baseName(state.report.folder), rule.folder)), ", ", choice)), control);
  return [item, choice, control];
}

export function intro(status, elsewhere) {
  if (!status.on) return "Off until you turn it on. It only moves files that arrive after that, using the same folders you approved.";
  if (elsewhere) return `On for ${baseName(status.folder)}. It only moves files that arrived there after it was turned on.`;
  return "On. It only moves files that arrived after you turned it on, using the folders you approved.";
}

export function every(used, which = "as set below") {
  const days = [...new Set(used.map((rule) => rule.days))];
  const age = days.length === 1 ? (days[0] ? `older than ${plural(days[0], "day")} ` : "") : `old enough, ${which}, `;
  return `Every hour, new files ${age}move to their folder, even when Downkeep is closed.`;
}

function state_(status, rules, elsewhere) {
  if (!status.on) return "Off. Nothing moves by itself.";
  if (elsewhere) return `On for ${baseName(status.folder)}, not for this folder.`;
  const used = rules.filter((rule) => rule.on && rule.auto);
  if (!used.length) return "On, but no folder below is switched on, so nothing will move.";
  const waiting = status.waiting ? ` ${plural(status.waiting, "file")} waiting now.` : "";
  const problem = status.available ? "" : ` ${sentence(status.why)}`;
  return `On. ${every(used)}${waiting}${problem}`;
}

/**
 * Nothing here now ever moves by itself: turning it on counts every file already in the folder as staying.
 * The folders can still be changed here, since they can't be while it is off.
 */
export function preview(rules, back = () => go.tab("auto")) {
  visit();
  let shown = rules;
  const problem = h("p", { class: "problem", role: "alert" });
  const files = state.report?.files ?? 0;
  const now = h("p", { class: "now" }, h("b", {}, "Nothing would move right now. "),
    files ? `The ${plural(files, "file")} here now stay where they are. Only files that arrive after you turn it on can move.` : "Only files that arrive after you turn it on can move.");
  const turnOn = button("main-act", "Turn on", (event) => { event.currentTarget.disabled = true; set(true, problem, event.currentTarget); });

  const why = (used) => (used.length ? every(used, "as set for each folder") : "No folder is switched on for it yet. Switch at least one on first.");
  function sum() {
    const used = shown.filter((rule) => rule.on && rule.auto);
    const line = panel.querySelector(".why");
    if (line) line.textContent = why(used);
    now.hidden = !used.length;
    turnOn.disabled = !used.length;
  }

  async function change(rule, changes, control, undoChange) {
    shown = await save(shown, rule, changes, control, undoChange, problem);
    sum();
  }

  stage({
    say: "Turn on automatic tidying?",
    why: why(shown.filter((rule) => rule.on && rule.auto)),
    art: h("div", { class: "hold autos" },
      now,
      h("p", { class: "soft" }, "Choose which folders take new files, and when:"),
      h("ul", { class: "folders" }, ...shown.filter((rule) => rule.on).map((rule) => folderRow(rule, change)[0])),
      problem),
    buttons: [turnOn, button("next", "Cancel", () => back(shown))],
    note: NEVER,
    focus: true,
  });
  sum();
  enter();
}

async function set(on, problem, control) {
  problem.textContent = "";
  try {
    await ask("/api/auto/set", on ? { on, confirmed: true } : { on });
  } catch (error) {
    if (ended(error)) return;
    if (control) control.disabled = false;
    problem.textContent = sentence(clean(error.message));
    return;
  }
  await showAuto(true, { quiet: true, arriving: on });
}
