export const sample = document.documentElement.dataset.sample;
const LIMIT = 1_000_000;
let key = null;

export const useKey = (value) => { key = value; };

async function server(path, body) {
  const text = JSON.stringify(body);
  if (text.length * 3 > LIMIT && new Blob([text]).size > LIMIT) {
    return { status: 413, value: { error: "That is too many at once. Untick some and try again" } };
  }
  const headers = { "Content-Type": "application/json" };
  if (key) headers["X-Downkeep-Key"] = key;
  const answer = await fetch(path, { method: "POST", headers, body: text });
  if (path === "/api/stay" && answer.ok) {
    const reader = answer.body.getReader();
    while (!(await reader.read()).done);
    return { status: 200, value: {} };
  }
  return { status: answer.status, value: await answer.json() };
}

async function reading(path) {
  if (path === "/api/report") return { status: 200, value: await (await fetch(sample)).json() };
  if (path === "/api/system") {
    return { status: 200, value: { tidy: false, trash: false, restore: false, auto: { available: false, why: "" }, recovered: null } };
  }
  return { status: 409, value: { error: "This is made-up data, so nothing can be moved" } };
}

// Every question to the server goes through here. A demo page sets data-sample and its own answers.
const send = sample ? globalThis.downkeepServer ?? reading : server;

export async function ask(path, body = {}) {
  const { status, value } = await send(path, body).catch(() => ({ status: 0, value: {} }));
  if (status !== 200) throw Object.assign(new Error(value.error), { status });
  return value;
}
