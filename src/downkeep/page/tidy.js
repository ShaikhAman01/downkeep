import { ask } from "./ask.js";
import {
  act, bdi, button, ended, entry, facts, folderIcon, go, h, human, list, loading, main, mark, plural, restage, small, stage, state,
  baseName, clean, ticks, toast, total, trouble, visit, warnIcon,
} from "./core.js";
import { loadRules, nameProblem, showRules } from "./rules.js";
import { lists, listRuns, loadHistory, place, showHistory } from "./runs.js";

const HERE = "Leave here";
const PROVEN = "Proven clutter";
const off = new Set();
let plan = null;
let chosen = null;
let focusTidy = false;
let renames = 0;

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
    // Proven clutter belongs in the Trash, not in a folder: it starts unticked
    for (const move of found.moves) if (move.proven) off.add(move.name);
    if (state.visit !== at) return;
  }
  draw(focus);
}

const again = () => { plan = null; showTidy(true); };

/** The server's same(): names some system would treat as one. Upper then lower case stands in for Python's casefold. */
export const sameName = (name) => name.normalize("NFC").toUpperCase().toLowerCase().normalize("NFC");

/** The suggested rules not yet covered by a rule for the same folder. An id already in use is left for the server to pick. */
export function newRules(suggested, rules) {
  const taken = new Set(rules.map((rule) => sameName(rule.folder)));
  const ids = new Set(rules.map((rule) => rule.id));
  const fresh = [];
  for (const { rule } of suggested) {
    if (taken.has(sameName(rule.folder))) continue;
    taken.add(sameName(rule.folder));
    const free = rule.id && !ids.has(rule.id);
    if (free) ids.add(rule.id);
    fresh.push(free ? rule : { ...rule, id: "" });
  }
  return fresh;
}

/** New folders worth having here. Nothing is made on disk until Tidy. */
function suggestions(here) {
  if (!here.suggested?.length) return null;
  const block = h("section", { class: "suggest", "aria-labelledby": "suggest-title" });
  const adds = [];
  const names = new Map();
  const shows = new Map();
  const opens = new Map();
  let known = null;
  const nameOf = (item) => (names.get(item) ?? item.folder).trim();
  const named = (item) => ({ ...item, folder: nameOf(item), rule: { ...item.rule, folder: nameOf(item) } });

  /** A typed name is checked like any folder name, and may not be one that is already taken. */
  function problemOf(item) {
    const name = nameOf(item);
    const wrong = nameProblem(name);
    if (wrong) return wrong;
    const key = sameName(name);
    if (names.has(item) && known?.some((rule) => sameName(rule.folder) === key)) return `You already have a folder named ${clean(name)}.`;
    const clash = (other) => other !== item && (names.has(item) || names.has(other)) && sameName(nameOf(other)) === key;
    if (here.suggested.some(clash)) return "Another suggested folder has this name.";
    return "";
  }

  /** Shows every problem, and puts the cursor in the first wrong name. False if there is one. */
  function allRight(items) {
    const wrong = items.filter((item) => shows.get(item)());
    if (wrong.length) opens.get(wrong[0])();
    return !wrong.length;
  }

  async function addAll() {
    for (const control of adds) control.disabled = true;
    let added;
    try {
      const { rules } = await loadRules();
      known = rules;
      if (!allRight(here.suggested)) {
        for (const control of adds) control.disabled = false;
        return;
      }
      const fresh = newRules(here.suggested.map(named), rules);
      added = fresh.length;
      if (added) await ask("/api/rules/save", { rules: [...fresh, ...rules] });
    } catch (error) {
      for (const control of adds) control.disabled = false;
      if (!ended(error)) toast(clean(error.message));
      return;
    }
    toast(added ? `Added ${plural(added, "folder")}. To undo, delete ${added === 1 ? "it" : "them"} in Change folders.` : "These folders are already there.");
    go.changed();
    focusTidy = true;
    await showTidy(true);
  }

  async function add(item, control) {
    control.disabled = true;
    try {
      const { rules } = await loadRules();
      known = rules;
      if (!allRight([item])) {
        control.disabled = false;
        return;
      }
      await ask("/api/rules/save", { rules: [named(item).rule, ...rules] });
    } catch (error) {
      control.disabled = false;
      if (!ended(error)) toast(clean(error.message));
      return;
    }
    toast(`Added ${clean(nameOf(item))}. To undo, delete it in Change folders.`);
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

  const count = here.suggested.length;
  const all = count > 1 ? button("add-all", `Add all ${count} folders`, addAll) : null;
  if (all) adds.push(all);
  block.append(
    h("div", { class: "tools" }, h("h3", { id: "suggest-title" }, "Suggested folders"),
      h("span", { class: "suggest-acts" }, all,
        button("text", "Not now", (event) => later(event.currentTarget), { "aria-label": "Not now, hide suggested folders" }))),
    h("ul", {}, ...here.suggested.map((item) => {
      const one = button("text add", "Add", (event) => add(item, event.currentTarget), { "aria-label": `Add the folder ${clean(item.folder)}` });
      adds.push(one);
      const id = `suggest-name-${renames += 1}`;
      const error = h("p", { class: "problem", id: `${id}-problem` });
      const title = h("b", {}, bdi(item.folder));
      let input = null;
      const show = () => {
        const wrong = problemOf(item);
        error.textContent = wrong;
        input?.setAttribute("aria-invalid", String(Boolean(wrong)));
        return wrong;
      };
      const open = () => {
        if (input) return input.focus();
        input = h("input", {
          type: "text", id, class: "folder-name", dir: "ltr", maxlength: 60, autocomplete: "off", spellcheck: "false",
          "aria-label": `Name for the suggested folder ${clean(item.folder)}`, "aria-describedby": `${id}-problem`,
        });
        input.value = nameOf(item);
        names.set(item, input.value);
        input.addEventListener("input", () => {
          names.set(item, input.value);
          // A change here can end a clash shown on another row
          for (const each of shows.values()) each();
        });
        input.addEventListener("keydown", (event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            add(item, one);
          }
        });
        title.replaceWith(input);
        rename.remove();
        input.focus();
        input.select?.();
        if (!known) loadRules().then((found) => { known = found.rules; show(); }, () => {});
      };
      const rename = button("text rename", "Rename", open, { "aria-label": `Rename ${clean(item.folder)} before adding it` });
      shows.set(item, show);
      opens.set(item, open);
      return h("li", {}, folderIcon(), h("div", {}, h("span", { class: "named-row" }, title, rename), h("p", { class: "soft" }, clean(item.why)), error), one);
    })),
  );
  return block;
}

function notices(here) {
  const proven = here.moves.filter((move) => move.proven).length;
  const odd = here.moves.filter((move) => move.warning);
  return [
    odd.length ? h("p", { class: "banner warn" }, warnIcon(),
      h("span", {}, `${odd.length === 1 ? "One file looks harmless but is really a program" : `${plural(odd.length, "file")} look harmless but are really programs`}: `,
        ...odd.slice(0, 3).flatMap((move, index) => [index ? ", " : "", bdi(move.name)]), odd.length > 3 ? " and more" : "", ".")) : null,
    proven ? h("p", { class: "banner" },
      `${proven === 1 ? "One file here is" : `${plural(proven, "file")} here are`} proven clutter, so ${proven === 1 ? "it is" : "they are"} left out. `,
      button("text", "Open Clutter", () => go.tab("clutter"))) : null,
  ].filter(Boolean);
}

const withSuggestions = (here, art) => {
  const parts = [...notices(here), suggestions(here)].filter(Boolean);
  return parts.length ? h("div", { class: "hold" }, ...parts, art) : art;
};

function draw(focus) {
  const here = plan;
  const home = baseName(state.report.folder);
  const groups = new Map(here.folders.map((folder) => [folder.name, { ...folder, moves: [] }]));
  const proven = here.moves.filter((move) => move.proven);
  for (const move of here.moves) if (!move.proven) groups.get(move.folder)?.moves.push(move);
  for (const [name, group] of groups) if (!group.moves.length) groups.delete(name);
  const picked = () => here.moves.filter((move) => !off.has(move.name));
  const entries = new Map();
  if (chosen !== HERE && chosen !== PROVEN && !groups.has(chosen)) chosen = groups.size ? [...groups.keys()][0] : proven.length ? PROVEN : HERE;
  if (chosen === HERE && !here.stays.length && groups.size) chosen = [...groups.keys()][0];

  list.setAttribute("aria-label", "Folders");
  list.replaceChildren(
    h("h2", {}, "Folders", h("span", {}, plural(here.moves.length + here.stays.length, "file"))),
    h("ul", {}, ...[...groups.keys(), ...(proven.length ? [PROVEN] : []), ...(here.stays.length ? [HERE] : [])].map((name) => {
      const made = entry(folderIcon(), name, "", () => { open(name); if (small.matches) main.scrollIntoView(); });
      entries.set(name, made);
      return h("li", {}, made);
    })));

  const tidy = button("main-act", "", apply);
  const buttons = [tidy, button("next", "Change folders", () => showRules()), button("next", "History", () => showHistory("tidy"))];

  function counts() {
    for (const [name, made] of entries) {
      const moves = name === PROVEN ? proven : groups.get(name)?.moves;
      const on = moves?.filter((move) => !off.has(move.name)).length;
      made.lastChild.textContent = !moves ? here.stays.length.toLocaleString()
        : on === moves.length ? on.toLocaleString() : `${on.toLocaleString()} of ${moves.length.toLocaleString()}`;
    }
    const now = picked();
    const into = new Set(now.map((move) => move.folder)).size;
    tidy.disabled = !now.length;
    tidy.textContent = now.length ? `Tidy ${plural(now.length, "file")}` : "Nothing ticked";
    if (now.length) return `Tidy ${plural(now.length, "file")} into ${plural(into, "folder")}`;
    return groups.size ? "Nothing is ticked, so nothing will move" : "Nothing left to tidy";
  }

  function open(name, first = false) {
    visit();
    chosen = name;
    mark(entries.get(name));
    const say = counts();
    const group = groups.get(name);
    const warn = (move) => move.warning ?? "";
    stage({
      say,
      why: "Nothing moves until you press Tidy. Untick anything to leave it. Undo puts everything back.",
      art: withSuggestions(here, name === PROVEN
        ? ticks(proven, off, {
          name: (move) => move.name,
          note: (move, on) => (on ? `to ${place(home, move.folder)}` : "proven clutter, left here"),
          changed: () => restage(counts()),
          warn,
          title: h("h3", {}, "Proven clutter", h("span", {}, ", see the "), button("text inline", "Clutter tab", () => go.tab("clutter"))),
        })
        : group
        ? ticks(group.moves, off, {
          name: (move) => move.name,
          note: (move, on) => (on ? move.why : "left here, you unticked it"),
          changed: () => restage(counts()),
          warn,
          title: h("h3", {}, "To ", bdi(place(home, name)), h("span", {}, `${group.exists ? "" : ", a new folder"}, ${human(total(group.moves, "bytes"))}`)),
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
      art: lists(result, "Moved", "Left alone", (name) => (folderOf.has(name) ? `to ${place(home, folderOf.get(name))}` : "")),
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
  if (focusTidy) {
    focusTidy = false;
    if (here.moves.length && !tidy.disabled) tidy.focus();
  }
}
