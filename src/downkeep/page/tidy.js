import { ask } from "./ask.js";
import {
  act, bdi, button, ended, entry, facts, folderIcon, go, h, human, list, loading, main, mark, plural, restage, small, stage, state,
  clean, ticks, toast, total, trouble, visit,
} from "./core.js";
import { loadRules, showRules } from "./rules.js";
import { lists, listRuns, loadHistory, showHistory } from "./runs.js";

const HERE = "Leave here";
const off = new Set();
let plan = null;
let chosen = null;

export const forgetPlan = () => { plan = null; };

export async function showTidy(focus = false) {
  if (!state.system.tidy) {
    visit();
    list.replaceChildren();
    stage({ say: "Tidying isn't available on this system yet", why: "You can still see what is clutter, and why. Nothing is moved.", art: h("div", {}), focus });
    return;
  }
  if (!plan) {
    list.replaceChildren(h("p", { class: "none" }, "Looking at your files"));
    const at = loading("Looking at your files", "Working out which folder each file belongs in. Nothing moves.");
    let found;
    try {
      found = await ask("/api/tidy/plan");
    } catch (error) {
      if (!ended(error) && state.visit === at) {
        list.replaceChildren();
        trouble(error, () => showTidy(true), () => go.tab("clutter"), "Back to Clutter");
      }
      return;
    }
    plan = found;
    if (state.visit !== at) return;
  }
  draw(focus);
}

const again = () => { plan = null; showTidy(true); };

/** New folders worth having here. Nothing is made on disk until Tidy. */
function suggestions(here) {
  if (!here.suggested?.length) return null;
  const block = h("section", { class: "suggest", "aria-labelledby": "suggest-title" });

  async function add(item, control) {
    control.disabled = true;
    try {
      const { rules } = await loadRules();
      await ask("/api/rules/save", { rules: [item.rule, ...rules] });
    } catch (error) {
      control.disabled = false;
      if (!ended(error)) toast(clean(error.message));
      return;
    }
    toast(`Added ${clean(item.folder)}. To undo, delete it in Change folders.`);
    go.changed();
    showTidy(true);
  }

  async function later(control) {
    control.disabled = true;
    try {
      await ask("/api/tidy/later");
    } catch (error) {
      control.disabled = false;
      if (!ended(error)) toast(clean(error.message));
      return;
    }
    here.suggested = [];
    block.remove();
    go.focus();
  }

  block.append(
    h("div", { class: "tools" }, h("h3", { id: "suggest-title" }, "Suggested folders"),
      button("text", "Not now", (event) => later(event.currentTarget), { "aria-label": "Not now, hide suggested folders" })),
    h("ul", {}, ...here.suggested.map((item) => h("li", {},
      folderIcon(),
      h("div", {}, h("b", {}, bdi(item.folder)), h("p", { class: "soft" }, clean(item.why))),
      button("text add", "Add", (event) => add(item, event.currentTarget), { "aria-label": `Add the folder ${clean(item.folder)}` })))),
  );
  return block;
}

const withSuggestions = (here, art) => {
  const block = suggestions(here);
  return block ? h("div", { class: "hold" }, block, art) : art;
};

function draw(focus) {
  const here = plan;
  const groups = new Map(here.folders.map((folder) => [folder.name, { ...folder, moves: [] }]));
  for (const move of here.moves) groups.get(move.folder).moves.push(move);
  const picked = () => here.moves.filter((move) => !off.has(move.name));
  const entries = new Map();
  if (chosen !== HERE && !groups.has(chosen)) chosen = groups.size ? [...groups.keys()][0] : HERE;
  if (chosen === HERE && !here.stays.length && groups.size) chosen = [...groups.keys()][0];

  list.setAttribute("aria-label", "Folders");
  list.replaceChildren(
    h("h2", {}, "Folders", h("span", {}, plural(here.moves.length + here.stays.length, "file"))),
    h("ul", {}, ...[...groups.keys(), ...(here.stays.length ? [HERE] : [])].map((name) => {
      const made = entry(folderIcon(), name, "", () => { open(name); if (small.matches) main.scrollIntoView(); });
      entries.set(name, made);
      return h("li", {}, made);
    })));

  const tidy = button("main-act", "", apply);
  const buttons = [tidy, button("next", "Change folders", () => showRules()), button("next", "History", () => showHistory("tidy"))];

  function counts() {
    for (const [name, made] of entries) {
      const moves = groups.get(name)?.moves;
      const on = moves?.filter((move) => !off.has(move.name)).length;
      made.lastChild.textContent = !moves ? here.stays.length.toLocaleString()
        : on === moves.length ? on.toLocaleString() : `${on.toLocaleString()} of ${moves.length.toLocaleString()}`;
    }
    const now = picked();
    const into = new Set(now.map((move) => move.folder)).size;
    tidy.disabled = !now.length;
    tidy.textContent = now.length ? `Tidy ${plural(now.length, "file")}` : "Nothing ticked";
    return now.length ? `Tidy ${plural(now.length, "file")} into ${plural(into, "folder")}` : "Nothing is ticked, so nothing will move";
  }

  function open(name, first = false) {
    visit();
    chosen = name;
    mark(entries.get(name));
    const say = counts();
    const group = groups.get(name);
    stage({
      say,
      why: "Nothing moves until you press Tidy. Untick anything to leave it. Undo puts everything back.",
      art: withSuggestions(here, group
        ? ticks(group.moves, off, {
          name: (move) => move.name,
          note: (move, on) => (on ? move.why : "left here, you unticked it"),
          changed: () => restage(counts()),
          title: h("h3", {}, "To ", bdi(name), h("span", {}, `${group.exists ? "" : ", a new folder"}, ${human(total(group.moves, "bytes"))}`)),
        })
        : h("div", { class: "more hold one" }, facts("These stay where they are", here.stays.map((stay) => [stay.name, stay.why])))),
      buttons,
      note: "Files still downloading, hidden files and existing folders are never touched.",
      focus: first && focus,
    });
  }

  async function apply() {
    const moves = picked();
    const names = moves.map((move) => move.name);
    const result = await act(`Tidying ${plural(names.length, "file")}`, "/api/tidy/apply", { plan: here.plan, names }, again, again);
    if (!result) return;
    plan = null;
    const folderOf = new Map(moves.map((move) => [move.name, move.folder]));
    const into = new Set(result.done.map((name) => folderOf.get(name))).size;
    const undo = result.done.length > 0 && result.batch != null;
    stage({
      say: result.done.length ? `Tidied ${plural(result.done.length, "file")} into ${plural(into, "folder")}` : "Nothing was moved",
      why: result.skipped.length ? "Some files were left where they are. The reason is next to each one." : "Undo puts them back.",
      art: lists(result, "Moved", "Left alone", (name) => (folderOf.has(name) ? `to ${folderOf.get(name)}` : "")),
      buttons: [
        undo ? button("main-act", "Undo", () => go.undo(result.batch, "tidy")) : null,
        button(undo ? "next" : "main-act", "Done", () => go.tab("tidy")),
      ].filter(Boolean),
      note: "Nothing is ever deleted for good.",
      focus: true,
    });
    go.refresh();
    list.replaceChildren();
    try {
      await loadHistory();
      listRuns(state.history, "History", "tidy", { chosen: result.batch });
    } catch (error) {
      ended(error);
    }
  }

  if (!here.moves.length && !here.stays.length) {
    visit();
    chosen = null;
    list.replaceChildren();
    stage({
      say: "Nothing to tidy",
      why: "There are no finished files here. Hidden, empty and unfinished files are left alone.",
      art: h("div", {}),
      buttons: buttons.slice(1),
      focus,
    });
  } else if (!here.moves.length) {
    visit();
    counts();
    mark(entries.get(HERE));
    stage({
      say: "Nothing to tidy",
      why: "No file here belongs in one of your folders. Change folders to add your own.",
      art: withSuggestions(here, h("div", { class: "more hold one" }, facts("These stay where they are", here.stays.map((stay) => [stay.name, stay.why])))),
      buttons: buttons.slice(1),
      focus,
    });
  } else {
    open(chosen, true);
  }
}
