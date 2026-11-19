/* Табло за управление на инсталационния агент.
 * Работи без външни зависимости: зарежда /api/snapshot (когато е стартирано
 * `python -m pasevsu_installer gui`) или JSON файл, избран ръчно.
 */
"use strict";

const PHASES = ["selection", "backup", "apply", "tests", "publish", "done"];
const PHASE_LABELS = {
  new: "нов",
  selection: "избор",
  backup: "бекъп",
  apply: "прилагане",
  tests: "тестове",
  publish: "публикуване",
  done: "завършено",
};

let snapshot = null;

const byId = (id) => document.getElementById(id);

function text(value, fallback) {
  if (value === null || value === undefined || value === "") {
    return fallback === undefined ? "—" : fallback;
  }
  return String(value);
}

function esc(value) {
  return text(value, "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function badge(status) {
  const raw = text(status, "?");
  const good = ["applied", "passed", "published", "unchanged", "done", "active", "ok"];
  const bad = ["failed", "broken", "error", "publish-failed"];
  const warn = ["planned", "skipped", "disabled", "unverified", "dry-run"];
  let cls = "";
  if (good.includes(raw)) cls = " ok";
  else if (bad.includes(raw)) cls = " bad";
  else if (warn.includes(raw)) cls = " warn";
  return `<span class="badge${cls}">${esc(raw)}</span>`;
}

function table(headers, rows, emptyText) {
  if (!rows.length) return `<p class="empty">${esc(emptyText)}</p>`;
  const head = headers.map((h) => `<th>${esc(h)}</th>`).join("");
  const body = rows.map((row) => `<tr>${row.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function notice(message) {
  const el = byId("notice");
  el.textContent = message || "";
  el.hidden = !message;
}

function loadScript(url) {
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = url;
    script.onload = () => resolve(url);
    script.onerror = () => reject(new Error("неуспешно зареждане на " + url));
    document.body.appendChild(script);
  });
}

async function load() {
  const source = new URLSearchParams(location.search).get("snapshot");
  const candidates = [];
  if (source) candidates.push(source);
  candidates.push("snapshot.json", "api/snapshot");
  for (const candidate of candidates) {
    try {
      const response = await fetch(candidate, { cache: "no-store" });
      if (!response.ok) continue;
      apply(await response.json(), candidate);
      return;
    } catch (error) {
      /* пробваме следващия източник */
    }
  }
  try {
    // Резервен вариант: статиченJSON до таблото (file:// или GitHub Pages).
    const url = await loadScript("snapshot.js");
    if (window.PASEVSU_SNAPSHOT) {
      apply(window.PASEVSU_SNAPSHOT, url);
      return;
    }
  } catch (error) {
    /* няма статичен snapshot */
  }
  if (byId("source-pill")) byId("source-pill").textContent = "източник: няма данни";
  notice(
    "Няма данни. Стартирайте „python -m pasevsu_installer gui“ или заредете JSON файл с бутона „Зареди файл“."
  );
}

function apply(data, source) {
  if (!data || typeof data !== "object") {
    notice("Файлът не съдържа валиден snapshot.");
    return;
  }
  snapshot = data;
  notice("");
  byId("source-pill").textContent = "източник: " + source;
  render();
}

function render() {
  const data = snapshot || {};
  const installer = data.installer || {};
  const run = data.run || {};
  byId("subtitle").textContent = `${text(installer.name, "pasevsu-installer-agent")} · версия ${text(
    installer.version,
    "—"
  )} · фаза ${text(PHASE_LABELS[run.phase] || run.phase, "нова")}`;
  byId("generated").textContent = data.generated_at ? "генерирано: " + data.generated_at : "";
  renderStats(data);
  renderPipeline(data);
  renderArtefacts(data);
  renderAgents(data);
  renderTasks(data);
  renderSuites(data);
  renderPublish(data);
  renderJournal(data);
  byId("report").textContent = data.report_markdown || "Няма отчет от предишен пуск.";
}

function renderStats(data) {
  const run = data.run || {};
  const agents = data.agents || [];
  const suites = data.suites || [];
  const tasks = data.tasks || [];
  const publish = data.publish || {};
  const passed = suites.filter((s) => s.status === "passed").length;
  const changed = tasks.filter((t) => t.status === "applied").length;
  const cards = [
    { label: "Версия", value: text((data.installer || {}).version), note: text((data.installer || {}).version_file, "") },
    { label: "Пускове", value: text(run.runs, "0"), note: "обновено " + text(run.updated_at) },
    { label: "Агенти", value: String(agents.filter((a) => a.status === "active").length), note: `${agents.length} декларации` },
    { label: "Задачи с промени", value: String(changed), note: `${tasks.length} записани` },
    { label: "Тестове", value: `${passed}/${suites.length}`, note: suites.some((s) => s.status === "failed") ? "има провал" : "без провал" },
    { label: "Публикуване", value: text(publish.status, "—"), note: text(publish.tag, "") },
  ];
  byId("stats").innerHTML = cards
    .map(
      (card) =>
        `<article class="card"><div class="label">${esc(card.label)}</div>` +
        `<div class="value">${esc(card.value)}</div>` +
        `<div class="note">${esc(card.note)}</div></article>`
    )
    .join("");
}

function renderPipeline(data) {
  const run = data.run || {};
  const current = text(run.phase, "new");
  const index = PHASES.indexOf(current);
  const items = PHASES.map((phase, position) => {
    let cls = "";
    if (current === "done" || (index >= 0 && position < index)) cls = "is-done";
    else if (phase === current) cls = "is-current";
    if (current === "failed") cls = "is-failed";
    return `<li class="${cls}"><span class="dot"></span><span>${esc(
      PHASE_LABELS[phase] || phase
    )}</span><code>${esc(phase)}</code></li>`;
  });
  byId("pipeline").innerHTML = items.join("");
}

function renderArtefacts(data) {
  const items = data.artefacts || [];
  if (!items.length) {
    byId("artefacts").innerHTML = `<li class="missing">няма артефакти</li>`;
    return;
  }
  byId("artefacts").innerHTML = items
    .map(
      (item) =>
        `<li><code>${esc(item.name)}</code><span class="${
          item.exists ? "" : "missing"
        }">${item.exists ? (item.size || 0) + " B" : "липсва"}</span></li>`
    )
    .join("");
}

function renderAgents(data) {
  const agents = data.agents || [];
  if (!agents.length) {
    byId("agents").innerHTML = `<p class="empty">${esc(
      data.registry_error || "Няма заредени декларации."
    )}</p>`;
    return;
  }
  const blocks = agents.map((agent) => {
    const tasks = agent.tasks || [];
    const rows = tasks.map((task) => {
      const state = task.state || {};
      return [
        `<code>${esc(task.id)}</code>`,
        esc(task.title),
        task.declared ? badge("active") : badge("disabled"),
        esc(text(task.risk, "low")),
        state.status ? badge(state.status) : `<span class="badge">не е пускана</span>`,
        esc(String(state.runs || 0)),
      ];
    });
    return (
      `<h2 style="margin-top:1rem">${esc(agent.title)} ${badge(agent.status)}</h2>` +
      `<p class="hint">${esc(text(agent.purpose, "—"))} · <code>${esc(
        text(agent.source, "—")
      )}</code> · обхват: ${esc((agent.scope || []).join(", ") || "—")}</p>` +
      table(["Задача", "Заглавие", "Декларирана", "Риск", "Състояние", "Пускове"], rows, "Агентът няма задачи.")
    );
  });
  byId("agents").innerHTML = blocks.join("");
}

function renderTasks(data) {
  const tasks = data.tasks || [];
  const rows = tasks.map((task) => [
    `<code>${esc(task.id)}</code>`,
    esc(text(task.agent)),
    badge(task.status),
    esc(String(task.runs || 0)),
    `<code>${esc(text(task.fingerprint, "").slice(0, 12))}</code>`,
  ]);
  byId("tasks").innerHTML = table(
    ["Задача", "Агент", "Статус", "Пускове", "Fingerprint"],
    rows,
    "Все още няма изпълнени задачи."
  );
}

function renderSuites(data) {
  const suites = data.suites || [];
  const rows = suites.map((suite) => [
    `<code>${esc(suite.id)}</code>`,
    esc(text(suite.description, "—")),
    suite.required ? badge("active") : badge("disabled"),
    badge(suite.status || "unverified"),
    esc(suite.duration_ms ? (suite.duration_ms / 1000).toFixed(1) + " s" : "—"),
    esc(text(suite.skip_reason, "")),
  ]);
  byId("suites").innerHTML = table(
    ["Пакет", "Описание", "Задължителен", "Статус", "Време", "Причина за пропуск"],
    rows,
    "Няма конфигурирани тестови пакети."
  );
}

function renderPublish(data) {
  const publish = data.publish || {};
  if (!publish.status) {
    byId("publish").innerHTML = `<p class="empty">Все още няма публикуване.</p>`;
    return;
  }
  const rows = [
    ["Статус", badge(publish.status)],
    ["Клон", `<code>${esc(text(publish.branch))}</code>`],
    ["Commit", `<code>${esc(text(publish.commit))}</code>`],
    ["Таг", `<code>${esc(text(publish.tag))}</code>`],
    ["Push", esc((publish.pushed || []).join(", ") || "не")],
    ["Файлове", esc(String((publish.paths || []).length))],
    ["Причина", esc(text(publish.reason, ""))],
  ];
  byId("publish").innerHTML = table(["Поле", "Стойност"], rows, "няма данни");
}

function renderJournal(data) {
  const events = data.journal || [];
  const rows = events
    .slice()
    .reverse()
    .map((event) => [
      `<code>${esc(text(event.at))}</code>`,
      badge(event.event),
      `<code>${esc(
        Object.keys(event)
          .filter((key) => !["at", "event", "pid"].includes(key))
          .map((key) => `${key}=${JSON.stringify(event[key])}`)
          .join(" ")
          .slice(0, 160)
      )}</code>`,
    ]);
  byId("journal").innerHTML = table(["Време", "Събитие", "Данни"], rows, "Журналът е празен.");
}

function setView(name) {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("is-active", tab.dataset.view === name);
  });
  document.querySelectorAll(".view").forEach((view) => {
    view.classList.toggle("is-active", view.id === "view-" + name);
  });
}

function init() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => setView(tab.dataset.view));
  });
  byId("refresh").addEventListener("click", load);
  byId("file").addEventListener("change", async (event) => {
    const file = event.target.files && event.target.files[0];
    if (!file) return;
    try {
      apply(JSON.parse(await file.text()), file.name);
    } catch (error) {
      notice("Файлът не е валиден JSON: " + error.message);
    }
  });
  load();
}

document.addEventListener("DOMContentLoaded", init);
