import { ask } from "./ask.js";
import {
  act, baseName, bdi, bin, button, clean, ended, entry, facts, folderIcon, go, h, list, loading, mark, moment, plural, small, stage,
  state, toast, trashIcon, trouble, visit, main,
} from "./core.js";

const TITLES = { tidy: "Tidied", auto: "Tidied by itself", trash: "Moved", undo: "Put back" };
const KINDS = { tidy: "Tidy", auto: "Automatic", trash: "Trash", undo: "Put back" };
const CHUNK = 300;

/** Where a run put a file, in words: "to Downloads › Documents", "in the Trash". */
export function where(run, file) {
  const home = baseName(run.folder);
  if (run.kind === "undo") return `back in ${home}`;
  if (file.back) return `put back in ${home}`;
  if (file.to === "trash") return `in the ${bin()}`;
  return `to ${place(home, file.to)}`;
}

export const place = (home, folder) => (!folder || folder === "." ? home : `${home} › ${folder.replaceAll("\\", " › ").replaceAll("/", " › ")}`);

export function lists(result, moved, left = "Left alone", where = () => "") {
  return h("div", { class: "more hold" }, ...[
    facts(moved, result.done.map((name) => [name, where(name)])),
    facts(left, result.skipped.map((item) => [item.name, item.why])),
  ].filter(Boolean));
}

export async function undo(batch, back, names = null) {
  const body = names ? { batch, names } : { batch };
  const say = names?.length === 1 ? "Putting the file back" : "Putting files back";
  const result = await act(say, "/api/undo", body, () => undo(batch, back, names), () => go.tab(back));
  if (!result) return;
  stage({
    say: result.done.length ? `Put back ${plural(result.done.length, "file")}` : "Nothing was put back",
    why: result.skipped.length ? "Some files couldn't go back, so they were left where they are." : "Every file is where it was.",
    art: lists(result, "Put back", "Not put back"),
    buttons: [
      button("main-act", "Done", () => go.tab(back)),
      names ? button("next", "Back to the run", () => showHistory(back, batch)) : null,
    ].filter(Boolean),
    focus: true,
  });
  go.refresh();
}

export async function loadHistory() {
  ({ batches: state.history } = await ask("/api/history"));
}

function short(seconds) {
  const when = new Date(seconds * 1000);
  const day = (date) => new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const ago = Math.round((day(new Date()) - day(when)) / 86_400_000);
  if (ago === 0) return when.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (ago === 1) return "Yesterday";
  if (ago > 1 && ago < 7) return when.toLocaleDateString([], { weekday: "short" });
  return when.toLocaleDateString([], { day: "numeric", month: "short" });
}

export const runIcon = (kind) => (kind === "trash" ? trashIcon() : folderIcon());

/** Fills the list on the left with past runs. */
export function listRuns(runs, title, back, { chosen = null, extra = null, none = "Nothing has been moved yet." } = {}) {
  list.setAttribute("aria-label", title);
  list.replaceChildren(...[
    h("h2", {}, title, h("span", {}, runs.length ? plural(runs.length, "run") : "")),
    runs.length
      ? h("ul", {}, ...runs.map((run) => {
        const label = `${KINDS[run.kind] ?? "Moved"}, ${plural(run.count, "file")}${run.undone ? ", undone" : ""}`;
        const item = entry(runIcon(run.kind), label, short(run.when), () => { mark(item); showRun(run, back, true); });
        item.setAttribute("aria-label", `${label}, ${moment(run.when)}`);
        if (run.batch === chosen) item.setAttribute("aria-current", "true");
        return h("li", {}, item);
      }))
      : h("p", { class: "none" }, none),
    extra,
  ].filter(Boolean));
}

function fileRows(run, back) {
  const files = run.files ?? run.names.map((name) => ({ name, to: "", back: false, can_put_back: false }));
  const items = h("ul", { class: "run-files" });
  const more = h("p", { class: "more-rows" });
  const count = h("p", { class: "soft", "aria-live": "polite" });
  const find = h("input", { type: "search", id: "find-file", autocomplete: "off", spellcheck: "false" });
  let shown = files;
  let drawn = 0;

  function row(file) {
    const canShow = run.kind !== "trash" || file.back;
    return h("li", {},
      h("div", {}, bdi(file.name), h("p", { class: "soft" }, where(run, file))),
      h("div", { class: "row-acts" },
        canShow ? button("text", "Show in folder", () => showMoved(run.batch, file.name), { "aria-label": `Show ${clean(file.name)} in its folder` }) : null,
        file.can_put_back && !run.undone ? button("text", "Put back", () => undo(run.batch, back, [file.name]), { "aria-label": `Put back ${clean(file.name)}` }) : null));
  }

  function draw(reset = false) {
    if (reset) { items.replaceChildren(); drawn = 0; }
    items.append(...shown.slice(drawn, drawn + CHUNK).map(row));
    drawn = Math.min(shown.length, drawn + CHUNK);
    more.hidden = drawn >= shown.length;
    more.replaceChildren(...(more.hidden ? [] : [
      `Showing ${drawn.toLocaleString()} of ${shown.length.toLocaleString()}. `,
      button("text", "Show more", () => { draw(); more.querySelector("button")?.focus(); }),
    ]));
    count.textContent = find.value ? (shown.length ? `${plural(shown.length, "file")} found` : "No file has that name") : "";
  }

  find.addEventListener("input", () => {
    const wanted = find.value.trim().toLocaleLowerCase();
    shown = wanted ? files.filter((file) => file.name.toLocaleLowerCase().includes(wanted)) : files;
    draw(true);
  });
  draw();
  const extra = run.count - files.length;
  return h("section", {},
    files.length > 8 ? h("div", { class: "find" }, h("label", { for: "find-file" }, "Find a file"), find, count) : null,
    items, more, extra > 0 ? h("p", { class: "soft" }, `And ${extra.toLocaleString()} more.`) : null);
}

async function showMoved(batch, name) {
  try {
    await ask("/api/show", { batch, name });
  } catch (error) {
    if (!ended(error)) toast(error.message);
    return;
  }
  toast("Opened your file manager");
}

export function showRun(run, back, move = false) {
  visit();
  const home = baseName(run.folder);
  const to = run.kind === "trash" ? ` to the ${bin()}` : "";
  const why = run.undone ? "This was undone."
    : run.can_undo ? "Undo puts each file back where it was. It never replaces anything."
      : run.kind === "trash" && !state.system.restore ? "To get one back, open your Recycle Bin and choose Restore."
        : run.kind === "undo" ? "These files are back where they were." : "This can't be undone here any more.";
  stage({
    say: `${TITLES[run.kind] ?? "Moved"} ${plural(run.count, "file")}${to}`,
    what: [`${moment(run.when)}, in `, bdi(home)],
    why,
    art: h("div", { class: "hold run" }, fileRows(run, back),
      h("div", { class: "more one" }, facts("Left alone", run.left.map((item) => [item.name, item.why])))),
    buttons: [
      run.can_undo && !run.undone ? button("main-act", "Undo all", () => undo(run.batch, back)) : null,
      button("next", "Back", () => go.tab(back)),
    ].filter(Boolean),
  });
  if (move && small.matches) main.scrollIntoView();
}

export async function showHistory(back, batch = null) {
  const at = loading("Looking at what was moved", "This only reads the record.");
  try {
    await loadHistory();
  } catch (error) {
    if (!ended(error) && state.visit === at) trouble(error, () => showHistory(back, batch), () => go.tab(back));
    return;
  }
  if (state.visit !== at) return;
  const runs = state.history;
  const chosen = runs.find((run) => run.batch === batch) ?? runs[0];
  listRuns(runs, "History", back, { chosen: chosen?.batch });
  if (chosen) showRun(chosen, back);
  else stage({ say: "Nothing has been moved yet", why: "Every run will be listed here, and you can undo it.", buttons: [button("main-act", "Back", () => go.tab(back))] });
  go.focus();
}
