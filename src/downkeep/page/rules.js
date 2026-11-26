import { ask } from "./ask.js";
import { bdi, button, clean, ended, go, h, list, loading, sentence, stage, state, switchButton, trouble, visit } from "./core.js";

const WAIT = 300;
const split = (text) => text.split(/[,\n]/).map((part) => part.trim()).filter(Boolean);
let fields = 0;

export async function loadRules() {
  return ask("/api/rules");
}

export async function showRules() {
  const at = loading("Reading your folders", "Nothing moves.");
  let found;
  try {
    found = await loadRules();
  } catch (error) {
    if (!ended(error) && state.visit === at) trouble(error, showRules, () => go.tab("tidy"));
    return;
  }
  if (state.visit === at) edit(found);
}

function edit({ rules, kinds, defaults }) {
  visit();
  list.replaceChildren(h("p", { class: "none" }, "Your own folders come first. A file goes to the first folder that fits."));
  const builtIn = new Set(defaults.map((rule) => rule.id));
  const kindName = new Map(kinds.map((kind) => [kind.id, kind.name]));
  let draft = structuredClone(rules);
  let opened = null;
  let checked = false;
  let invalid = false;
  const named = new WeakSet();
  const asking = new WeakMap();
  const fill = new WeakMap();
  const own = () => draft.filter((rule) => !builtIn.has(rule.id));
  const holder = h("div", { class: "hold rules" });
  const problem = h("p", { class: "problem", role: "alert" });
  const save = button("main-act", "Save folders", store);

  function field(rule, key, name, help, changed) {
    const id = `field-${fields += 1}`;
    const input = h("input", { type: "text", id, "aria-describedby": `${id}-help`, autocomplete: "off", spellcheck: "false" });
    input.value = rule[key].join(", ");
    input.addEventListener("input", () => { rule[key] = split(input.value); changed(); });
    input.addEventListener("blur", recheck);
    return h("div", { class: "field" }, h("label", { for: id }, name), input, h("p", { id: `${id}-help` }, help));
  }

  function nameField(rule) {
    const id = `field-${fields += 1}`;
    const error = h("p", { class: "problem", id: `${id}-problem` });
    const input = h("input", { type: "text", id, class: "folder-name", dir: "ltr", maxlength: 60, "aria-describedby": `${id}-problem`, "aria-label": "Folder name", autocomplete: "off", spellcheck: "false" });
    input.value = rule.folder;
    const hint = h("p", { class: "hint", id: `${id}-hint` }, "Suggested");
    hint.hidden = !(named.has(rule) && rule.folder);
    input.setAttribute("aria-describedby", `${id}-problem ${id}-hint`);
    const check = () => {
      const wrong = nameProblem(rule.folder);
      error.textContent = wrong;
      input.setAttribute("aria-invalid", String(Boolean(wrong)));
    };
    input.addEventListener("input", () => {
      // Once the person types a name, it is theirs and is never replaced
      named.delete(rule);
      hint.hidden = true;
      rule.folder = input.value.trim();
      check();
      recheck();
    });
    input.addEventListener("blur", () => { check(); recheck(); });
    if (checked) check();
    const suggested = (name) => {
      rule.folder = name;
      input.value = name;
      hint.hidden = false;
      check();
      recheck();
    };
    return { input, error, hint, suggested };
  }

  const takes = (rule) => [
    ...rule.words.map((word) => `“${word}”`), ...rule.sites, ...rule.kinds.map((kind) => kindName.get(kind) ?? kind),
    ...rule.extensions.map((extension) => `.${extension}`),
  ].join(", ") || "Nothing yet";

  function row(rule) {
    const mine = !builtIn.has(rule.id);
    const { input, error, hint, suggested } = nameField(rule);
    fill.set(rule, suggested);
    const tools = h("div", { class: "tools" });
    const details = h("div", { class: "details" });
    const summary = h("p", { class: "takes" }, bdi(takes(rule)));
    if (mine) {
      const place = own().indexOf(rule);
      const called = rule.folder ? clean(rule.folder) : "new folder";
      tools.append(
        button("text", opened === rule ? "Close" : "Edit", () => { opened = opened === rule ? null : rule; draw(rule, ".edit"); }, { "aria-expanded": String(opened === rule), "aria-label": `${opened === rule ? "Close" : "Edit"} ${called}` }),
        button("text", "Up", () => shift(rule, -1), { "aria-label": `Move ${called} up`, ...(place === 0 ? { disabled: "" } : {}) }),
        button("text", "Down", () => shift(rule, 1), { "aria-label": `Move ${called} down`, ...(place === own().length - 1 ? { disabled: "" } : {}) }),
        button("text", "Delete", () => remove(rule), { "aria-label": `Delete ${called}` }),
      );
      ["edit", "up", "down", "delete"].forEach((name, index) => tools.children[index].classList.add(name));
      const changed = () => {
        summary.replaceChildren(bdi(takes(rule)));
        suggest(rule);
        recheck();
      };
      if (opened === rule) details.append(...editor(rule, changed));
    }
    const item = h("li", { class: rule.on ? "" : "off" },
      h("div", { class: "rule-top" },
        h("div", { class: "rule-name" }, input, hint, error, summary),
        switchButton(rule.on, `Use ${rule.folder || "this folder"}`, (on, control) => {
          rule.on = on;
          control.setAttribute("aria-checked", String(on));
          item.classList.toggle("off", !on);
        })),
      tools, details);
    return item;
  }

  function editor(rule, changed) {
    const boxes = kinds.map((kind) => {
      const box = h("input", { type: "checkbox" });
      box.checked = rule.kinds.includes(kind.id);
      box.addEventListener("change", () => {
        rule.kinds = box.checked ? [...rule.kinds, kind.id] : rule.kinds.filter((id) => id !== kind.id);
        changed();
      });
      return h("label", { class: "kind" }, box, kind.name);
    });
    return [
      h("p", { class: "why" }, "A file goes here if any one of these fits. Separate with commas."),
      field(rule, "words", "Words in the name", "For example invoice, receipt", changed),
      field(rule, "sites", "Websites it came from", "For example github.com. A best guess from your browser's record.", changed),
      field(rule, "extensions", "File endings", "For example pdf, docx", changed),
      h("fieldset", {}, h("legend", {}, "Kinds of files"), h("div", { class: "kinds" }, ...boxes)),
    ];
  }

  function shift(rule, by) {
    const mine = own();
    const from = mine.indexOf(rule);
    mine.splice(from, 1);
    mine.splice(from + by, 0, rule);
    draft = [...mine, ...draft.filter((other) => builtIn.has(other.id))];
    draw(rule, by < 0 ? ".up" : ".down");
  }

  function remove(rule) {
    draft = draft.filter((other) => other !== rule);
    if (opened === rule) opened = null;
    draw();
    say("Removed. Press Save folders to keep this.");
    save.focus();
  }

  function suggest(rule) {
    if (!named.has(rule)) return;
    clearTimeout(asking.get(rule));
    if (!["words", "sites", "kinds", "extensions"].some((key) => rule[key].length)) return;
    asking.set(rule, setTimeout(async () => {
      let found;
      try {
        found = await ask("/api/rules/name", { rule: { ...rule, folder: "" } });
      } catch (error) {
        ended(error);
        return;
      }
      if (named.has(rule) && draft.includes(rule)) fill.get(rule)(found.folder);
    }, WAIT));
  }

  function recheck() {
    if (!invalid) return;
    const wrong = firstProblem();
    say(wrong ? wrong[1] : "");
    invalid = Boolean(wrong);
  }

  function add() {
    const rule = { id: "", folder: "", kinds: [], extensions: [], words: [], sites: [], on: true, auto: false, days: 7 };
    named.add(rule);
    draft = [rule, ...draft];
    opened = rule;
    draw(rule, ".folder-name");
    say("");
  }

  function reset() {
    draft = structuredClone(defaults);
    opened = null;
    draw();
    say("Back to the built-in folders. Press Save folders to keep this, or Cancel.");
    save.focus();
  }

  function say(text) {
    if (!text) invalid = false;
    problem.textContent = text;
  }

  function firstProblem() {
    for (const rule of draft) {
      const wrong = nameProblem(rule.folder);
      if (wrong) return [rule, wrong];
      if (!builtIn.has(rule.id) && !["words", "sites", "kinds", "extensions"].some((key) => rule[key].length)) {
        return [rule, `Say which files go to ${clean(rule.folder)}: a word, a website, a kind or a file ending.`];
      }
    }
    return null;
  }

  async function store() {
    const wrong = firstProblem();
    if (wrong) {
      checked = true;
      invalid = true;
      if (!builtIn.has(wrong[0].id)) opened = wrong[0];
      draw(wrong[0], nameProblem(wrong[0].folder) ? ".folder-name" : ".details input");
      say(wrong[1]);
      return;
    }
    save.disabled = true;
    invalid = false;
    say("");
    try {
      await ask("/api/rules/save", { rules: draft });
    } catch (error) {
      save.disabled = false;
      if (!ended(error)) { say(sentence(clean(error.message))); problem.scrollIntoView({ block: "nearest" }); }
      return;
    }
    go.changed();
    go.tab("tidy");
  }

  function draw(rule = null, target = null) {
    const mine = own();
    const rest = draft.filter((other) => builtIn.has(other.id));
    const rows = new Map(draft.map((item) => [item, row(item)]));
    holder.replaceChildren(...[
      mine.length ? h("h3", {}, "Your folders") : null,
      mine.length ? h("ul", {}, ...mine.map((item) => rows.get(item))) : null,
      h("h3", {}, "Built-in folders"),
      h("ul", {}, ...rest.map((item) => rows.get(item))),
    ].filter(Boolean));
    if (!rule) return;
    const made = rows.get(rule);
    made.scrollIntoView({ block: "nearest" });
    const control = made.querySelector(target);
    (control?.disabled ? made.querySelector(".edit") : control)?.focus();
  }

  draw();
  stage({
    say: "Change folders",
    why: "Rename a folder, switch it off, or add your own. Files already sorted stay where they are.",
    art: holder,
    buttons: [save, button("next", "Add a folder", add), button("next", "Reset", reset, { "aria-label": "Reset to the built-in folders" }), button("next", "Cancel", () => go.tab("tidy")), problem],
    focus: true,
  });
}

function nameProblem(name) {
  if (!name) return "A folder needs a name.";
  if (/[<>:"/\\|?*]/.test(name)) return 'A folder name can\'t contain < > : " / \\ | ? *';
  if (/^[.~]/.test(name) || name.endsWith(".")) return "A folder name can't start with a dot or ~, or end with a dot.";
  return "";
}
