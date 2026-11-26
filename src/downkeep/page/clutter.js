import { sample, ask } from "./ask.js";
import {
  act, acts, baseName, bdi, bin, button, capital, clean, copyPicture, ended, entry, facts, go, h, human, icon, kindOf, list,
  historyIcon, lone, main, mark, picture, plural, restage, sheet, small, stage, state, ticks, toast, total, visit, zipPicture,
} from "./core.js";
import { showHistory } from "./runs.js";

/** After something was moved to the Trash and nothing proven is left. */
export const allClear = (report) => !proof(report).proven.length && report.files > 0
  && state.history.some((run) => run.kind === "trash" && !run.undone);

const SHOWN = 500;
const off = new Set();
let current = null;

function days(count) {
  if (count < 1) return "Last changed today";
  if (count < 60) return `Last changed ${plural(count, "day")} ago`;
  if (count < 730) return `Last changed ${Math.round(count / 30.4)} months ago`;
  return `Last changed ${Math.floor(count / 365)} years ago`;
}

// A copy filed in a folder like Documents/Resume.pdf is proof, never clutter itself
const here = (name) => !/[\\/]/.test(name);

export function proof(report) {
  const zips = report.extracted.map((item) => ({ ...item, zip: true }));
  const zipNames = new Set(zips.map((item) => item.name));
  const copies = report.duplicates
    .flatMap((group) => group.names.slice(1).filter(here).map((name) => ({ name, keep: group.names[0], bytes: group.bytes_each })))
    .filter((item) => !zipNames.has(item.name));
  return { zips, copies, proven: [...zips, ...copies] };
}

export const copyWhy = (item) => `Same as ${clean(item.keep)}, which stays. Same contents, checked from start to end.`;

function footnote() {
  if (sample && !state.system?.trash) return "This is made-up data. Nothing on your computer was read.";
  if (state.system?.trash && !state.history.length) return "New here? Nothing moves until you press a yellow button and confirm it.";
  if (!state.system?.trash) return `Checked at ${state.checked}. Nothing was changed. Moving files isn't available on this system yet.`;
  if (state.moved) return `Checked at ${state.checked}. Checking changes nothing, and nothing left your computer.`;
  return `Checked at ${state.checked}. Nothing was moved, changed or deleted, and nothing left your computer.`;
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

async function showFile(name) {
  try {
    await ask("/api/show", { name });
  } catch (error) {
    if (!ended(error)) toast(error.message);
    return;
  }
  toast("Opened your file manager");
}

function showIn(name, style) {
  return sample ? null : button(style, "Show in folder", () => showFile(name), { "aria-label": `Show ${clean(name)} in its folder` });
}

const why = (item, home) => item.zip
  ? `${item.files === 1 ? "its file is" : `all ${item.files.toLocaleString()} files are`} in ${item.folder === "." ? home : item.folder}`
  : `same as ${item.keep}`;

/** `quiet` redraws only the list, so that a result stays on screen. */
export function showClutter(quiet = false) {
  const report = state.report;
  const home = baseName(report.folder);
  const { zips, copies, proven } = proof(report);
  const provenNames = new Set(proven.map((item) => item.name));
  const kept = new Set(copies.map((item) => item.keep));
  const others = report.biggest.filter((item) => !provenNames.has(item.name)).slice(0, 5);
  const shown = [...zips.slice(0, SHOWN), ...copies.slice(0, SHOWN)];
  const buttons = new Map();
  const canTrash = state.system?.trash;
  for (const name of off) if (!provenNames.has(name)) off.delete(name);

  function open(item, move = false) {
    visit();
    current = item.name ?? null;
    mark(buttons.get(item));
    const place = shown.indexOf(item);
    if (item.more) {
      stage({ say: "More about this folder", what: [bdi(report.folder)], art: moreAbout(report), note: [footnote(), state.version && `Downkeep ${state.version}.`].filter(Boolean).join(" ") });
    } else {
      const where = item.folder === "." ? home : item.folder;
      const next = () => {
        const target = shown[(place + 1) % shown.length];
        open(target);
        buttons.get(target).scrollIntoView({ block: "nearest" });
        acts.querySelector(".skip").focus();
      };
      const movable = canTrash && provenNames.has(item.name);
      stage({
        say: item.zip
          ? item.files === 1 ? "The file in this zip is already in your folder" : `All ${item.files.toLocaleString()} files in this zip are already in your folder`
          : item.keep ? "This is an exact copy" : kept.has(item.name) ? "This is the one that stays" : "No proof, so this one stays",
        what: [bdi(item.name), `, ${human(item.bytes)}`],
        why: item.zip
          ? `${item.files === 1 ? "It" : "Each"} was found with the same name, size and checksum.`
          : item.keep ? copyWhy(item) : kept.has(item.name) ? "Its identical copies are in the list." : `${days(item.days_old)}. Old or big is not proof of clutter.`,
        art: item.zip ? zipPicture(item, where) : item.keep ? copyPicture(item, home) : picture(item.name, sheet(185, item.name, "left alone")),
        buttons: [
          movable ? button("main-act", `Move to ${bin()}`, () => confirmOne(item), { "aria-label": `Move ${clean(item.name)} to the ${bin()}` }) : null,
          showIn(item.name, movable ? "next" : "main-act"),
          place >= 0 && shown.length > 1 ? button("next skip", "Next", next) : null,
          place >= 0 ? h("p", {}, `${(place + 1).toLocaleString()} of ${shown.length.toLocaleString()}`) : null,
        ].filter(Boolean),
        note: footnote(),
      });
    }
    if (move && small.matches) main.scrollIntoView();
  }

  function confirmOne(item) {
    visit();
    const where = item.folder === "." ? home : item.folder;
    stage({
      say: `Move this file to the ${bin()}?`,
      what: [bdi(item.name), `, ${human(item.bytes)}, `, bdi(why(item, home))],
      why: `The proof is checked again just before it moves. ${back("it")}`,
      art: item.zip ? zipPicture(item, where) : copyPicture(item, home),
      buttons: [
        button("main-act", `Yes, move to ${bin()}`, () => trash([item], () => confirmOne(item))),
        button("next", "Cancel", () => { open(item); acts.querySelector("button").focus(); }),
      ],
      note: "Nothing is ever deleted for good.",
      focus: true,
    });
  }

  function review() {
    visit();
    mark(null);
    const go_ = button("main-act", "", () => trash(proven.filter((item) => !off.has(item.name)), review));
    const heading = () => {
      const picked = proven.filter((item) => !off.has(item.name));
      go_.disabled = !picked.length;
      go_.textContent = picked.length ? `Move ${picked.length.toLocaleString()} to ${bin()}` : "Nothing ticked";
      return [
        picked.length ? `Move ${plural(picked.length, "file")} to the ${bin()}?` : "Nothing is ticked",
        picked.length ? `${human(total(picked, "bytes"))}, every one proven` : "Tick a file to move it",
      ];
    };
    const rows = ticks(proven, off, {
      name: (item) => item.name,
      note: (item, on) => (on ? why(item, home) : `${why(item, home)}, keeping it`),
      changed: () => restage(...heading()),
    });
    const [say, what] = heading();
    stage({
      say, what,
      why: `The proof is checked again just before each file. Anything that changed is skipped. ${back("them")}`,
      art: rows,
      buttons: [go_, button("next", "Cancel", () => first(true))],
      note: "Nothing is ever deleted for good.",
      focus: true,
    });
  }

  const back = (what) => (state.system.restore ? `You can restore ${what} from your Trash.` : "To get one back, open your Recycle Bin and choose Restore.");

  async function trash(items, retry) {
    const names = items.map((item) => item.name);
    const result = await act(`Moving ${plural(names.length, "file")} to the ${bin()}`, "/api/trash", { names }, retry, () => first(true));
    if (!result) return;
    const done = new Set(result.done);
    const bytes = total(items.filter((item) => done.has(item.name)), "bytes");
    outcome(result, {
      say: result.done.length ? `Moved ${plural(result.done.length, "file")} to the ${bin()}` : "Nothing was moved",
      what: result.done.length ? `${human(bytes)}, which your ${bin()} still holds until you empty it` : null,
      why: result.done.length
        ? state.system.restore ? "Undo puts them back. You can also restore them from your Trash." : "To get one back, open your Recycle Bin and choose Restore."
        : "Every file was left where it is.",
      moved: "Moved",
      undo: result.done.length && result.batch != null && state.system.restore,
    });
  }

  function outcome(result, { say, what, why: text, moved, undo }) {
    const buttons = [button(undo ? "next" : "main-act", "Done", () => go.tab("clutter"))];
    if (undo) buttons.unshift(button("main-act", "Undo", () => go.undo(result.batch, "clutter")));
    stage({
      say, what, why: text,
      art: go.lists(result, moved),
      buttons,
      note: "Nothing is ever deleted for good.",
      focus: true,
    });
    go.refresh();
  }

  function row(item, kind, text, value) {
    const made = entry(icon(kind), text, value, () => open(item, true), kept.has(item.name));
    buttons.set(item, made);
    return h("li", {}, made);
  }

  function group(title, value, items) {
    const extra = items.length - SHOWN;
    return [
      h("h2", {}, title, h("span", {}, value)),
      h("ul", {}, ...items.slice(0, SHOWN).map((item) => row(item, kindOf(item.name), item.name, human(item.bytes)))),
      extra > 0 ? h("p", { class: "none" }, `And ${extra.toLocaleString()} more.${canTrash ? " Move all shows every one." : ""}`) : null,
    ];
  }

  const rest = report.files - proven.length;
  list.setAttribute("aria-label", "Files");
  list.replaceChildren(...[
    canTrash && proven.length > 1 ? button("all", `Move all ${proven.length.toLocaleString()} to ${bin()}`, review) : null,
    ...(zips.length ? group("Already extracted", human(total(zips, "bytes")), zips) : []),
    ...(copies.length ? group("Exact copies", human(total(copies, "bytes")), copies) : []),
    ...(report.files ? group("Left alone", human(Math.max(0, report.bytes - total(proven, "bytes"))), others) : []),
    rest > others.length ? h("p", { class: "none" }, `And ${plural(rest - others.length, "more file")}.`) : null,
    h("ul", {}, row({ more: true }, "dots", "More about this folder", ""),
      state.system?.tidy ? h("li", {}, entry(historyIcon(), "History", "", () => showHistory("clutter"))) : null),
  ].filter(Boolean));

  function first(focus = false) {
    if (proven.length) {
      open(shown.find((item) => item.name === current) ?? shown[0]);
    } else {
      visit();
      mark(null);
      stage(report.files
        ? allClear(report)
          ? { say: "All clear: no proven clutter", why: "Everything left here is either one of a kind or has no proof. Tidy can still sort it into folders.", art: lone(home, plural(report.files, "file")), note: footnote() }
          : { say: "Nothing here is proven clutter", why: "There are no exact copies, and no zip whose every file was found. So everything stays.", art: lone(home, plural(report.files, "file")), note: footnote() }
        : { say: "There are no finished files here", why: "Hidden, empty and unfinished files are not counted.", art: lone(home, "nothing to check"), note: footnote() });
    }
    if (focus) go.focus();
  }
  if (!quiet) first();
}

export function showRecovered(found, next) {
  visit();
  mark(null);
  stage({
    say: "Downkeep was interrupted last time",
    why: "Each file was checked again just now. Nothing was overwritten or deleted.",
    art: go.lists(found, "Finished", "Not finished"),
    buttons: [button("main-act", "Continue", next)],
    note: "History shows every run.",
    focus: true,
  });
}

