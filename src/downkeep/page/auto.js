import { ask } from "./ask.js";
import { baseName, bdi, button, clean, ended, go, h, list, loading, plural, sentence, stage, state, switchButton, trouble, visit } from "./core.js";
import { loadRules } from "./rules.js";
import { listRuns, loadHistory, showHistory, undo } from "./runs.js";

const DAYS = [0, 1, 2, 3, 7, 14, 30];
const after = (days) => (days === 0 ? "right away" : `after ${plural(days, "day")}`);
const NEVER = "Trash is never automatic. Anything unclear stays in Downloads.";

export async function showAuto(focus = false) {
  const at = loading("Looking at what happens by itself", "This only reads your choices.");
  list.replaceChildren();
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
  draw(status, rules, focus);
}

function draw(status, rules, focus) {
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

  async function master(on, control) {
    if (on && !state.history.some((run) => run.kind === "tidy")) return preview(shown);
    control.disabled = true;
    await set(on, problem, control);
  }

  async function change(rule, changes, control, undoChange) {
    control.disabled = true;
    problem.textContent = "";
    const next = shown.map((other) => (other.id === rule.id ? { ...other, ...changes } : other));
    try {
      ({ rules: shown } = await ask("/api/rules/save", { rules: next }));
      go.changed();
    } catch (error) {
      undoChange();
      if (!ended(error)) problem.textContent = sentence(clean(error.message));
    }
    control.disabled = false;
    control.focus();
    idle();
  }

  const note = h("p", { class: "soft" });
  const idle = () => {
    note.textContent = state_(status, shown, elsewhere);
  };

  function row(rule) {
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
    return h("li", {}, h("div", {}, h("b", {}, bdi(rule.folder)), h("p", { class: "soft" }, "to ", bdi(rule.folder), ", ", choice)), control);
  }

  idle();
  const usable = shown.filter((rule) => rule.on);
  const last = runs.find((run) => run.can_undo && !run.undone);
  stage({
    say: "Tidy new files for me",
    what: status.note ? clean(status.note) : null,
    why: "Off until you turn it on. It only moves files that arrive after that, using the same folders you approved.",
    art: h("div", { class: "hold autos" },
      h("ul", {},
        h("li", { class: "master" },
          h("div", {}, h("b", {}, "Automatic tidying"), note),
          switchButton(status.on, "Automatic tidying", master)),
        ...usable.map(row)),
      problem),
    buttons: [
      elsewhere ? button("main-act", "Use this folder instead", () => set(true, problem)) : null,
      status.on && !elsewhere ? button("next", "Pause all", (event) => { event.currentTarget.disabled = true; set(false, problem, event.currentTarget); }) : null,
      last ? button("next", "Undo last run", () => undo(last.batch, "auto")) : null,
      button("next", "History", () => showHistory("auto")),
    ].filter(Boolean),
    note: NEVER,
    focus,
  });
}

function state_(status, rules, elsewhere) {
  if (!status.on) return "Off. Nothing moves by itself.";
  if (elsewhere) return `On for ${baseName(status.folder)}, not for this folder.`;
  if (!rules.some((rule) => rule.on && rule.auto)) return "On, but no folder below is switched on, so nothing will move.";
  const waiting = status.waiting ? `${plural(status.waiting, "file")} waiting` : "nothing waiting";
  const problem = status.available ? "" : ` ${sentence(status.why)}`;
  return `On, ${waiting}.${status.next ? ` Next check ${clean(status.next)}.` : ""}${problem}`;
}

function preview(rules) {
  visit();
  const used = rules.filter((rule) => rule.on && rule.auto);
  const problem = h("p", { class: "problem", role: "alert" });
  stage({
    say: "Turn on automatic tidying?",
    why: used.length
      ? "You haven't used Tidy yet, so here is where new files would go. Files that are here now stay where they are."
      : "No folder is switched on for it yet. Switch at least one on first.",
    art: h("div", { class: "hold autos" }, h("ul", {}, ...used.map((rule) =>
      h("li", {}, h("div", {}, h("b", {}, bdi(rule.folder)), h("p", { class: "soft" }, "new files, ", after(rule.days)))))), problem),
    buttons: [
      used.length ? button("main-act", "Turn it on", (event) => { event.currentTarget.disabled = true; set(true, problem, event.currentTarget); }) : null,
      button("next", "Cancel", () => go.tab("auto")),
    ].filter(Boolean),
    note: NEVER,
    focus: true,
  });
}

async function set(on, problem, control) {
  problem.textContent = "";
  try {
    await ask("/api/auto/set", { on });
  } catch (error) {
    if (ended(error)) return;
    if (control) control.disabled = false;
    problem.textContent = sentence(clean(error.message));
    return;
  }
  await showAuto(true);
}
