import { ask } from "./ask.js";
import {
  act, baseName, bdi, bin, button, ended, entry, facts, folderIcon, go, h, list, loading, mark, moment, plural, small, stage, state,
  trouble, visit, main,
} from "./core.js";

const TITLES = { tidy: "Tidied", auto: "Tidied by itself", trash: "Moved", undo: "Put back" };

export function lists(result, moved, left = "Left alone", where = () => "") {
  return h("div", { class: "more hold" }, ...[
    facts(moved, result.done.map((name) => [name, where(name)])),
    facts(left, result.skipped.map((item) => [item.name, item.why])),
  ].filter(Boolean));
}

export async function undo(batch, back) {
  const result = await act("Putting files back", "/api/undo", { batch }, () => undo(batch, back), () => go.tab(back));
  if (!result) return;
  stage({
    say: result.done.length ? `Put back ${plural(result.done.length, "file")}` : "Nothing was put back",
    why: result.skipped.length ? "Some files couldn't go back, so they were left where they are." : "Every file is where it was.",
    art: lists(result, "Put back", "Not put back"),
    buttons: [button("main-act", "Done", () => go.tab(back))],
    focus: true,
  });
  go.refresh();
}

export async function loadHistory() {
  ({ batches: state.history } = await ask("/api/history"));
}

/** Fills the list on the left with past runs. */
export function listRuns(runs, title, back, { chosen = null, extra = null, none = "Nothing has been moved yet." } = {}) {
  list.setAttribute("aria-label", title);
  list.replaceChildren(...[
    h("h2", {}, title, h("span", {}, runs.length ? plural(runs.length, "run") : "")),
    runs.length
      ? h("ul", {}, ...runs.map((run) => {
        const item = entry(folderIcon(), moment(run.when), run.undone ? "undone" : run.kind === "undo" ? "put back" : plural(run.count, "file"), () => { mark(item); showRun(run, back, true); });
        if (run.batch === chosen) item.setAttribute("aria-current", "true");
        return h("li", {}, item);
      }))
      : h("p", { class: "none" }, none),
    extra,
  ].filter(Boolean));
}

export function showRun(run, back, move = false) {
  visit();
  const place = baseName(run.folder);
  const to = run.kind === "trash" ? ` to the ${bin()}` : "";
  const extra = run.count - run.names.length;
  const why = run.undone ? "This was undone."
    : run.can_undo ? "Undo puts each file back where it was. It never replaces anything."
      : run.kind === "trash" && !state.system.restore ? "To get one back, open your Recycle Bin and choose Restore."
        : run.kind === "undo" ? "These files are back where they were." : "This can't be undone here any more.";
  stage({
    say: `${TITLES[run.kind] ?? "Moved"} ${plural(run.count, "file")}${to}`,
    what: [`${moment(run.when)}, in `, bdi(place)],
    why,
    art: h("div", { class: "more hold" }, ...[
      facts("Files", run.names.map((name) => [name, ""]), extra > 0 ? `And ${extra.toLocaleString()} more.` : null),
      facts("Left alone", run.left.map((item) => [item.name, item.why])),
    ].filter(Boolean)),
    buttons: [
      run.can_undo && !run.undone ? button("main-act", "Undo", () => undo(run.batch, back)) : null,
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
