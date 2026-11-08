"use strict";

const form = document.querySelector("#settings");
const controls = document.querySelector("#controls");
const notice = document.querySelector("#notice");
const restart = document.querySelector("#restart");
const snapshot = document.querySelector("#snapshot");
const routes = [
  {select: "#input-route", field: "q1_mic_device", option: "audio_input", devices: "inputs"},
  {select: "#output-route", field: "q1_speaker_device", option: "audio_output", devices: "outputs"},
];
const secrets = [
  {field: "gate_pin", clear: "#clear-gate-pin"},
  {field: "yandex_api_key", clear: "#clear-speech"},
  {field: "deepseek_api_key", clear: "#clear-deepseek"},
];
let current;
let snapshotUrl;

const tabs = [...document.querySelectorAll("[data-tab]")];
const views = [...document.querySelectorAll("[data-view]")];
const familyList = document.querySelector("#family-list");
const familyValue = form.elements.namedItem("family_members");
const domainChoices = document.querySelector("#domain-choices");
const save = document.querySelector("#save");
const saveState = document.querySelector("#save-state");
const volumeValue = document.querySelector("#volume-value");
const phoneRecipient = document.querySelector("#phone-recipient");
const pickers = new Map();
let catalog = [];
let catalogError = false;
let dirty = false;
const domainLabels = {light:"Свет", switch:"Выключатели", cover:"Приводы", fan:"Вентиляторы",
  scene:"Сцены", media_player:"Медиаплееры", climate:"Климат", humidifier:"Влажность",
  vacuum:"Пылесосы", input_boolean:"Логические помощники", timer:"Таймеры",
  number:"Числовые параметры", input_number:"Числовые помощники", select:"Режимы", input_select:"Помощники выбора"};
let controlDomains = Object.keys(domainLabels);


const activeLLM = form.elements.namedItem("llm_active");
const llmProfiles = document.querySelector("#llm-profiles");
const deepseekCard = document.querySelector("#llm-deepseek");
const profileTemplate = document.querySelector("#llm-profile");
let profilesDirty = false;

function llmCards() {
  return [...llmProfiles.querySelectorAll("[data-llm-id]")];
}
function refreshProviders(selected) {
  activeLLM.replaceChildren(new Option("DeepSeek", "deepseek"),
    ...llmCards().map(card => new Option(card.querySelector("[data-llm-name]").value || "Новый сервер", card.dataset.llmId)));
  activeLLM.value = [...activeLLM.options].some(option => option.value === selected) ? selected : "deepseek";
}
function modelInputs(card) {
  if (card === deepseekCard) {
    return {id: "deepseek", model: form.elements.namedItem("deepseek_model"),
      key: form.elements.namedItem("deepseek_api_key"), clear: document.querySelector("#clear-deepseek")};
  }
  return {id: card.dataset.llmId, model: card.querySelector("[data-llm-model]"),
    key: card.querySelector("[data-llm-key]"), clear: card.querySelector("[data-llm-clear]"),
    url: card.querySelector("[data-llm-url]")};
}
async function loadModels(card, force = false) {
  const fields = modelInputs(card);
  const endpoint = fields.url?.value.trim() || "";
  if (fields.url && !/^https?:\/\//i.test(endpoint)) return;
  if (card === deepseekCard && !fields.key.value.trim() && !current?.keys?.deepseek_api_key) return;
  if (!force && card.dataset.modelsLoaded === "yes") return;
  const ticket = String(Number(card.dataset.modelsRequest || "0") + 1);
  card.dataset.modelsRequest = ticket;
  const output = card.querySelector("[data-model-status]");
  output.textContent = "Загружаем модели…";
  output.dataset.error = "false";
  const data = {id: fields.id};
  if (fields.url) data.base_url = endpoint;
  if (fields.key.value.trim() || fields.clear.checked) data.api_key = fields.key.value.trim();
  try {
    const result = await (await request("llm/models", "POST", data)).json();
    if (card.dataset.modelsRequest !== ticket) return;
    const list = card.querySelector("datalist");
    list.replaceChildren(...result.models.map(model => new Option(model, model)));
    if (!fields.model.value && result.models.length === 1) {
      fields.model.value = result.models[0];
      fields.model.dispatchEvent(new Event("input", {bubbles: true}));
    }
    card.dataset.modelsLoaded = "yes";
    output.textContent = result.models.length ? "Доступно моделей: " + result.models.length : "Список пуст. Введи ID модели вручную.";
  } catch (error) {
    if (card.dataset.modelsRequest !== ticket) return;
    output.textContent = error.message;
    output.dataset.error = "true";
  }
}
function wireModels(card) {
  const fields = modelInputs(card);
  card.querySelector("[data-model-load]").addEventListener("click", () => loadModels(card, true));
  for (const field of [fields.url, fields.key, fields.clear].filter(Boolean)) {
    field.addEventListener("change", () => {
      card.dataset.modelsLoaded = "";
      loadModels(card, true);
    });
  }
}
function loadActiveModels() {
  const card = activeLLM.value === "deepseek" ? deepseekCard
    : llmCards().find(card => card.dataset.llmId === activeLLM.value);
  if (card) loadModels(card);
}
function addLLM(profile) {
  const card = profileTemplate.content.firstElementChild.cloneNode(true);
  card.dataset.llmId = profile.id;
  const fields = modelInputs(card);
  const name = card.querySelector("[data-llm-name]");
  const effort = card.querySelector("[data-llm-effort]");
  const title = card.querySelector("summary");
  title.textContent = profile.name || "Новый сервер";
  name.value = profile.name;
  fields.url.value = profile.base_url;
  fields.model.value = profile.model;
  effort.value = profile.effort;
  fields.key.placeholder = profile.has_key ? "Сохранён · можно заменить" : "Необязательно для локального сервера";
  fields.clear.disabled = !profile.has_key;
  const list = card.querySelector("datalist");
  list.id = "llm-models-" + profile.id;
  fields.model.setAttribute("list", list.id);
  card.addEventListener("input", () => { profilesDirty = true; });
  name.addEventListener("input", () => {
    title.textContent = name.value || "Новый сервер";
    refreshProviders(activeLLM.value);
  });
  card.addEventListener("toggle", () => { if (card.open) loadModels(card); });
  card.querySelector("[data-llm-remove]").addEventListener("click", () => {
    const selected = activeLLM.value;
    card.remove();
    refreshProviders(selected);
    profilesDirty = true;
    changed();
  });
  llmProfiles.append(card);
  wireModels(card);
  return card;
}
function readLLMProfiles() {
  return llmCards().map(card => {
    const fields = modelInputs(card);
    const profile = {id: fields.id, name: card.querySelector("[data-llm-name]").value.trim(),
      base_url: fields.url.value.trim(), model: fields.model.value.trim(),
      effort: card.querySelector("[data-llm-effort]").value};
    if (fields.key.value.trim() || fields.clear.checked) profile.api_key = fields.key.value.trim();
    return profile;
  });
}
function renderLLM(state) {
  llmProfiles.replaceChildren();
  for (const profile of state.llm_profiles || []) addLLM(profile);
  refreshProviders(state.options.llm_active || "deepseek");
  profilesDirty = false;
  if (!document.querySelector('[data-view="voice"]').hidden) loadActiveModels();
}
wireModels(deepseekCard);
activeLLM.addEventListener("change", loadActiveModels);
document.querySelector("#llm-add").addEventListener("click", () => {
  const id = "llm-" + [...crypto.getRandomValues(new Uint8Array(8))]
    .map(value => value.toString(16).padStart(2, "0")).join("");
  const card = addLLM({id, name:"", base_url:"", model:"", effort:"default"});
  card.open = true;
  refreshProviders(activeLLM.value);
  card.querySelector("[data-llm-name]").focus();
  profilesDirty = true;
  changed();
});


const personalProfile = document.querySelector("#personal-profile");
const personalStatus = document.querySelector("#personal-status");
const personalContent = document.querySelector("#personal-content");
let personalRequest = 0;

function personalInput(title, element) {
  const label = document.createElement("label");
  label.textContent = title;
  label.append(element);
  return label;
}
function localDate(value) {
  const date = new Date(value * 1000);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}
function personalRecord(kind, record, profile) {
  const card = document.createElement("article");
  card.className = "card";
  const fields = document.createElement("div");
  fields.className = "fields";
  const text = document.createElement("textarea");
  text.rows = 2;
  text.maxLength = 500;
  text.value = kind === "preferences" ? record.phrase : record.text;
  text.readOnly = Boolean(record.completed);
  fields.append(personalInput(kind === "preferences" ? "Фраза" : "Текст", text));
  if (kind === "notes") {
    const metadata = document.createElement("p");
    metadata.className = "hint";
    const sources = {voice:"По просьбе в разговоре",panel:"Сохранено в панели",import:"Импортировано"};
    const kinds = {fact:"Факт",preference:"Предпочтение",name:"Название"};
    metadata.textContent = [record.owner_name, kinds[record.kind] || "Личная заметка",
      sources[record.source] || "Существующая запись", record.created_at ? new Date(record.created_at).toLocaleString("ru-RU") : ""].filter(Boolean).join(" · ");
    card.append(metadata);
  }
  let entity, due, threshold;
  if (kind === "preferences") {
    entity = document.createElement("input");
    entity.value = record.entity;
    entity.setAttribute("list", "personal-entities");
    fields.append(personalInput("Устройство", entity));
  }
  if (kind === "reminders") {
    const details = document.createElement("p");
    details.className = "hint";
    const condition = record.condition;
    const label = condition?.action ? "Одноразовое действие" : "Личное напоминание";
    const deliveryStates = {pending:"Ожидает", attempting:"Попытка доставки", delivered:"Озвучено", declined:"Получатель отказался", expired:"Срок истёк", undelivered:"Не доставлено"};
    const state = deliveryStates[record.status] || (record.completed ? "Завершено" : "Ожидает");
    const action = condition?.action ? [condition.action.entity_id, condition.action.service].join(" · ") : "";
    const trigger = condition?.zone || condition?.entity || "";
    const runStatus = {accepted:"Команда принята", blocked:"Заблокировано", unconfirmed:"Нет подтверждения",
      started:"Запуск начат", profile_removed:"Профиль удалён"};
    details.textContent = [label, state, trigger, action, runStatus[record.status],
      !condition?.action && record.delivery_attempts ? "Попыток: " + record.delivery_attempts : ""].filter(Boolean).join(" · ");
    card.append(details);
    if (record.due && (!condition || condition.kind === "time")) {
      due = document.createElement("input");
      due.type = "datetime-local";
      due.value = localDate(record.due);
      due.disabled = record.completed;
      fields.append(personalInput("Дата и время", due));
    }
    if (condition?.kind === "state") {
      threshold = document.createElement("input");
      threshold.value = condition.value;
      threshold.disabled = record.completed;
      const operation = {above:"Выше",below:"Ниже",equals:"Равно"}[condition.operator];
      fields.append(personalInput(operation || "Значение условия", threshold));
    }
  }
  card.append(fields);
  const actions = document.createElement("div");
  actions.className = "actions phone-actions";
  const save = document.createElement("button");
  save.type = "button";
  save.textContent = "Сохранить запись";
  save.disabled = Boolean(record.completed);
  const remove = document.createElement("button");
  remove.type = "button";
  remove.textContent = kind === "reminders" && !record.completed ? "Отменить" : "Удалить";
  actions.append(save, remove);
  card.append(actions);
  const path = "personal/" + kind + "/" + encodeURIComponent(record.id) + "?profile=" + encodeURIComponent(profile);
  async function update(method, data) {
    save.disabled = true;
    remove.disabled = true;
    try {
      await request(path, method, data);
      if (personalProfile.value === profile) await loadPersonal();
    } catch (error) {
      personalStatus.textContent = error.message;
      personalStatus.dataset.error = "true";
      save.disabled = Boolean(record.completed);
      remove.disabled = false;
    }
  }
  save.addEventListener("click", () => {
    const data = {text: text.value.trim()};
    if (entity) data.entity = entity.value.trim();
    if (due && due.value && due.value !== localDate(record.due)) data.due = new Date(due.value).toISOString();
    if (threshold && threshold.value.trim() !== String(record.condition.value ?? "")) data.value = threshold.value.trim();
    update("PATCH", data);
  });
  remove.addEventListener("click", () => {
    if (window.confirm("Убрать эту запись?")) update("DELETE");
  });
  return card;
}

async function loadPersonal() {
  const ticket = ++personalRequest;
  const selected = personalProfile.value;
  personalStatus.textContent = "Загружаем записи…";
  personalStatus.dataset.error = "false";
  try {
    const data = await (await request("personal" + (selected ? "?profile=" + encodeURIComponent(selected) : ""))).json();
    if (ticket !== personalRequest) return;
    personalProfile.replaceChildren(...data.profiles.map(profile => new Option(profile.name, profile.id)));
    personalProfile.value = data.profile;
    const list = document.querySelector("#personal-entities");
    list.replaceChildren(...catalog.map(row => new Option([row.name,row.area].filter(Boolean).join(" · "), row.id)));
    personalContent.replaceChildren();
    for (const [kind, title] of [["reminders", "Напоминания и правила"], ["notes", "Заметки"], ["preferences", "Кеш выбора устройств"]]) {
      const section = document.createElement("section");
      section.className = "personal-section";
      const heading = document.createElement("h3");
      heading.textContent = title + " · " + data[kind].length;
      section.append(heading);
      if (kind === "preferences") {
        const explanation = document.createElement("p");
        explanation.className = "muted";
        explanation.textContent = "Служебные соответствия фраз и устройств для повторных обращений. Это не привычки и не личная память. Можно исправить или удалить; важные факты сохраняются в заметках по вашей просьбе.";
        section.append(explanation);
      }
      for (const item of data[kind]) section.append(personalRecord(kind, item, data.profile));
      if (!data[kind].length) {
        const empty = document.createElement("p");
        empty.className = "muted";
        empty.textContent = "Здесь пока пусто.";
        section.append(empty);
      }
      personalContent.append(section);
    }
    personalStatus.textContent = "";
  } catch (error) {
    if (ticket !== personalRequest) return;
    personalStatus.textContent = error.message;
    personalStatus.dataset.error = "true";
  }
}
personalProfile.addEventListener("change", loadPersonal);
document.querySelector("#personal-refresh").addEventListener("click", loadPersonal);

const auditStatus = document.querySelector("#audit-status");
let auditTicket = 0;

function auditLine(text) {
  const item = document.createElement("li");
  item.textContent = text;
  return item;
}

async function loadAudit() {
  const ticket = ++auditTicket;
  auditStatus.textContent = "Загрузка…";
  auditStatus.dataset.error = "false";
  try {
    const data = await (await request("audit?limit=200")).json();
    if (ticket !== auditTicket) return;
    const limit = Number(data.daily_limit) || 0;
    document.querySelector("#audit-today").textContent =
      `Сегодня (${data.today}): ${Number(data.tokens_today).toLocaleString("ru-RU")} токенов` +
      (limit ? ` из ${limit.toLocaleString("ru-RU")}` : ", лимит не задан");
    const usage = document.querySelector("#audit-usage");
    usage.replaceChildren(...data.usage.map(row => auditLine(
      `${row.day} · ${row.profile_name}: запросов ${row.requests}, ` +
      `токенов ${row.prompt_tokens} + ${row.completion_tokens}`)));
    if (!data.usage.length) usage.replaceChildren(auditLine("Запросов к модели ещё не было."));
    const items = document.querySelector("#audit-items");
    items.replaceChildren(...data.log.map(row => auditLine(
      `${new Date(row.at).toLocaleString("ru-RU")} · ${row.profile_name} · ${row.tool} ` +
      `${row.arguments} → ${row.result}`)));
    if (!data.log.length) items.replaceChildren(auditLine("Инструменты ещё не вызывались."));
    auditStatus.textContent = "";
  } catch (error) {
    if (ticket !== auditTicket) return;
    auditStatus.textContent = error.message;
    auditStatus.dataset.error = "true";
  }
}
document.querySelector("#audit-refresh").addEventListener("click", loadAudit);

function setTab(key) {
  if (!tabs.some(tab => tab.dataset.tab === key)) key = "calls";
  for (const tab of tabs) {
    if (tab.dataset.tab === key) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  }
  for (const view of views) view.hidden = view.dataset.view !== key;
  form.hidden = key === "history" || key === "personal" || key === "audit" || key === "knowledge" || key === "operations";
  if (key === "personal") loadPersonal();
  if (key === "knowledge") loadKnowledge();
  if (key === "audit") loadAudit();
  if (key === "calls" || key === "hardware") refreshPhone();
  if (key === "voice" && current) loadActiveModels();
}
for (const tab of tabs) tab.addEventListener("click", () => {
  if (location.hash.slice(1) === tab.dataset.tab) setTab(tab.dataset.tab);
  else location.hash = tab.dataset.tab;
});
window.addEventListener("beforeunload", event => {
  if (!dirty) return;
  event.preventDefault();
  event.returnValue = "";
});
window.addEventListener("hashchange", () => setTab(location.hash.slice(1)));

function syncSaveState() {
  saveState.textContent = dirty ? "Есть несохранённые изменения"
    : current?.pending ? "Сохранено · ждёт применения" : "Настройки применены";
  save.disabled = !dirty;
  restart.disabled = !dirty && !current?.pending;
}
function changed() {
  dirty = true;
  syncSaveState();
}

function updateVolume() {
  volumeValue.textContent = form.elements.namedItem("call_playback_volume").value + "%";
}
form.addEventListener("input", event => {
  if (event.target === phoneRecipient || event.target.closest("#sandbox")) return;
  if (familyList.contains(event.target)) syncFamily();
  updateVolume();
  changed();
});

const normalized = text => String(text || "").toLocaleLowerCase("ru").replaceAll("ё", "е");

function makePicker(host, accessibleName, roomSelection = false) {
  const value = host.querySelector("input, textarea");
  const multiple = host.hasAttribute("data-multiple");
  const filter = host.dataset.entity;
  value.hidden = true;
  const current = document.createElement("div");
  current.className = "selection";
  const search = document.createElement("input");
  search.type = "search";
  search.placeholder = "Найти по имени или комнате…";
  search.setAttribute("aria-label", accessibleName);
  const results = document.createElement("div");
  results.className = "picker-results";
  results.hidden = true;
  host.classList.add("picker");
  host.append(current, search, results);
  let rooms;
  if (roomSelection) {
    rooms = document.createElement("select");
    rooms.setAttribute("aria-label", "Добавить устройства комнаты");
    rooms.addEventListener("input", event => event.stopPropagation());
    rooms.addEventListener("change", event => {
      event.stopPropagation();
      if (rooms.value) {
        const ids = catalog.filter(row => row.area === rooms.value && matches(row)).map(row => row.id);
        write([...new Set([...values(), ...ids])]);
        rooms.value = "";
      }
    });
    host.append(rooms);
  }

  const values = () => value.value.trim().split(/\s+/).filter(Boolean);
  function matches(row) {
    const domain = row.id.split(".")[0];
    if (filter === "control") return controlDomains.includes(domain);
    if (filter === "gate") return ["cover", "lock", "button", "script", "switch", "input_boolean"].includes(domain);
    return !filter || filter === domain;
  }
  function write(next) {
    value.value = next.join("\n");
    search.value = "";
    results.hidden = true;
    value.dispatchEvent(new Event("input", {bubbles: true}));
    renderSelection();
  }
  function renderSelection() {
    current.replaceChildren();
    for (const id of values()) {
      const item = catalog.find(row => row.id === id);
      const chip = document.createElement("span");
      chip.className = "selection-chip";
      const action = {lock:"разблокировать",cover:"открыть",button:"нажать",script:"выполнить",switch:"включить",input_boolean:"включить"};
      chip.textContent = filter === "gate"
        ? [item?.name || id, id, action[id.split(".")[0]]].filter(Boolean).join(" · ")
        : item?.name || id;
      chip.title = id;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "×";
      remove.setAttribute("aria-label", "Убрать " + (item?.name || id));
      remove.addEventListener("click", () => write(values().filter(previous => previous !== id)));
      chip.append(remove);
      current.append(chip);
    }
  }
  function showResults() {
    const words = normalized(search.value).trim().split(/\s+/).filter(Boolean);
    const selected = values();
    const candidates = catalog.filter(row => matches(row) && !selected.includes(row.id)
      && words.every(word => normalized([row.name, row.area, row.id].join(" ")).includes(word)));
    results.replaceChildren();
    for (const item of candidates.slice(0, 12)) {
      const option = document.createElement("button");
      option.type = "button";
      option.textContent = item.name;
      const detail = document.createElement("small");
      detail.textContent = [item.area, item.id].filter(Boolean).join(" · ");
      option.append(detail);
      option.addEventListener("click", () => {
        write(multiple ? [...selected, item.id] : [item.id]);
        search.focus();
        results.hidden = true;
      });
      results.append(option);
    }
    if (!candidates.length) {
      const hint = document.createElement("p");
      hint.textContent = catalogError ? "Каталог недоступен. Обновите страницу." : catalog.length
        ? "Совпадений нет" : "Загружаем каталог…";
      results.append(hint);
    }
    results.hidden = false;
  }
  search.addEventListener("focus", showResults);
  search.addEventListener("input", event => { event.stopPropagation(); showResults(); });
  host.addEventListener("focusout", event => {
    if (!host.contains(event.relatedTarget)) results.hidden = true;
  });
  host.addEventListener("keydown", event => {
    if (event.key === "Escape") { results.hidden = true; search.focus(); }
    if (event.target === search && event.key === "Enter") {
      event.preventDefault();
      results.querySelector("button")?.click();
    }
    if (event.target === search && event.key === "ArrowDown") {
      event.preventDefault();
      results.querySelector("button")?.focus();
    }
  });
  function renderPicker() {
    renderSelection();
    if (rooms) {
      const labels = [...new Set(catalog.filter(matches).map(row => row.area).filter(Boolean))]
        .sort((a, b) => a.localeCompare(b, "ru"));
      rooms.replaceChildren(new Option("Добавить всю комнату…", ""), ...labels.map(label => new Option(label, label)));
    }
  }
  pickers.set(host, renderPicker);
  renderPicker();
}

function syncFamily() {
  familyValue.value = [...familyList.querySelectorAll("[data-profile]")].map(card => {
    const fields = [...card.querySelectorAll("[data-profile-value]")];
    return fields.map(input => input.value.trim().replace(/\s*\n\s*/g, " ").replaceAll("|", "/")).join(" | ");
  }).join("\n");
}

function addFamily(line = "", expand = false) {
  const data = line.split("|").map(value => value.trim());
  const card = document.createElement("details");
  card.className = "card profile";
  card.dataset.profile = "";
  card.open = expand;
  const summary = document.createElement("summary");
  const heading = document.createElement("strong");
  heading.textContent = data[0] || "Новый участник";
  const phone = document.createElement("span");
  phone.textContent = data[1] || "Укажите номер";
  summary.append(heading, phone);
  card.append(summary);
  const fields = document.createElement("div");
  fields.className = "fields";
  const definitions = [
    ["Имя"], ["Номер телефона"], ["Доступ к устройствам", "", true],
    ["Доступные замки", "gate", true], ["Личный календарь", "calendar"], ["Местоположение", "person"],
    ["Права доступа"],
  ];
  for (const [index, [label, filter, multiple]] of definitions.entries()) {
    const field = document.createElement(filter === undefined ? "label" : "div");
    field.className = "entity-field";
    const title = document.createElement("span");
    title.textContent = label;
    field.append(title);
    const input = document.createElement(index === 6 ? "select" : "input");
    input.dataset.profileValue = "";
    if (index === 6) input.replaceChildren(
      new Option("Только выбранные устройства", "selected"),
      new Option("Читать весь дом · управлять выбранными", "read_all"),
      new Option("Доступ к устройствам как у владельца", "owner"),
    );
    input.value = data[index] || (index === 6 ? "selected" : "");
    if (index === 0) input.addEventListener("input", () => { heading.textContent = input.value || "Новый участник"; });
    if (index === 1) {
      input.type = "tel";
      input.placeholder = "+7…";
      input.addEventListener("input", () => { phone.textContent = input.value || "Укажите номер"; });
    }
    if (filter !== undefined) {
      const host = document.createElement("div");
      host.dataset.entity = filter;
      if (multiple) host.dataset.multiple = "";
      host.append(input);
      field.append(host);
      makePicker(host, label + " участника", index === 2);
    } else field.append(input);
    fields.append(field);
  }
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "remove-profile";
  remove.textContent = "Убрать участника";
  remove.addEventListener("click", () => {
    for (const host of card.querySelectorAll(".picker")) pickers.delete(host);
    card.remove();
    syncFamily();
    changed();
    showFamilyEmpty();
  });
  card.append(fields, remove);
  familyList.append(card);
  showFamilyEmpty();
  if (expand) fields.querySelector("input").focus();
}
function showFamilyEmpty() {
  familyList.querySelector(".empty-family")?.remove();
  if (familyList.querySelector("[data-profile]")) return;
  const message = document.createElement("p");
  message.className = "card muted empty-family";
  message.textContent = "Список пока пуст. Добавь человека, которому нужен доступ по телефону.";
  familyList.append(message);
}
function renderFamily() {
  for (const host of familyList.querySelectorAll(".picker")) pickers.delete(host);
  familyList.replaceChildren();
  for (const line of familyValue.value.split(/\r?\n/).filter(Boolean)) addFamily(line);
  showFamilyEmpty();
}
document.querySelector("#family-add").addEventListener("click", () => {
  addFamily("", true);
  syncFamily();
  changed();
});

function renderDomains() {
  const value = form.elements.namedItem("allow_domains");
  const selected = value.value.split(/\s+/).filter(Boolean);
  domainChoices.replaceChildren();
  for (const domain of controlDomains) {
    const label = document.createElement("label");
    label.className = "check";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = domain;
    box.checked = selected.includes(domain);
    box.addEventListener("input", () => {
      value.value = [...domainChoices.querySelectorAll("input:checked")].map(item => item.value).join("\n");
    });
    label.append(box, domainLabels[domain] || domain);
    domainChoices.append(label);
  }
}

for (const host of document.querySelectorAll("[data-entity]")) {
  makePicker(host, host.parentElement.firstChild.textContent.trim() || "Найти устройство");
}
async function loadCatalog(attempt = 0) {
  try {
    const data = await (await request("entities")).json();
    catalog = data.entities.sort((a,b) => a.name.localeCompare(b.name, "ru"));
    controlDomains = data.control_domains;
    catalogError = false;
    for (const update of pickers.values()) update();
    renderDomains();
    const personalEntities = document.querySelector("#personal-entities");
    personalEntities?.replaceChildren(...catalog.map(row => new Option(row.name, row.id)));
  } catch {
    catalogError = true;
    for (const update of pickers.values()) update();
    if (attempt < 5) setTimeout(() => loadCatalog(attempt + 1), 3000 * (attempt + 1));
  }
}
loadCatalog();


async function request(path, method = "GET", data) {
  let response;
  try {
    response = await fetch(path, {
      method,
      cache: "no-store",
      headers: {"Content-Type": "application/json", "X-Jarvis-Setup": "1"},
      body: data === undefined ? undefined : JSON.stringify(data),
    });
  } catch {
    throw new Error("Нет связи с аддоном. Проверьте, что он запущен.");
  }
  if (response.status === 401 || response.status === 403) {
    throw new Error("Сессия Home Assistant истекла. Обновите страницу.");
  }
  if (!response.ok) {
    const detail = response.headers.get("Content-Type")?.startsWith("text/plain")
      ? (await response.text()).slice(0, 250)
      : "";
    throw new Error(detail || "Ошибка HTTP " + response.status);
  }
  return response;
}

function showNotice(message, error = false) {
  notice.textContent = message;
  notice.dataset.error = String(error);
}

function addChoices(selector, values) {
  const list = document.querySelector(selector);
  list.replaceChildren();
  for (const value of values) {
    list.append(new Option(value, value));
  }
}

function deviceLabel(name) {
  if (name.includes("Sonix")) return "Sonix · камера Q1";
  if (name.includes("Microchip")) return "Microchip · звук Q1";
  return name;
}

function setRoute(route, state) {
  const select = document.querySelector(route.select);
  const configured = state.options[route.field];
  const active = state.audio[route.option];
  const devices = new Set(state.devices[route.devices]);
  if (active) devices.add(active);
  select.replaceChildren(
    new Option("Отключено", "__off__"),
    new Option("По умолчанию (PulseAudio)", ""),
    ...Array.from(devices, name => new Option(deviceLabel(name), name)),
    new Option("Указано вручную (ALSA)", "__custom__"),
  );
  select.value = configured === "pulse" ? (active || "") : configured ? "__custom__" : "__off__";
}

function fieldValue(key, element) {
  if (key === "family_members") return element.value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  if (["allowed_entities", "observed_entities", "room_entities", "allow_domains", "owner_locks"].includes(key)) {
    return element.value.split(/\s+/).filter(Boolean);
  }
  if (element.type === "checkbox") return element.checked;
  if (element.type === "number" || element.type === "range") return element.value === "" ? undefined : Number(element.value);
  return element.value.trim();
}

function render(state) {
  current = state;
  for (const [key, value] of Object.entries(state.options)) {
    const input = form.elements.namedItem(key);
    if (!input) continue;
    if (input.type === "checkbox") input.checked = value;
    else input.value = Array.isArray(value) ? value.join("\n") : (value ?? "");
  }
  for (const secret of secrets) {
    const input = form.elements.namedItem(secret.field);
    input.value = "";
    input.placeholder = state.keys[secret.field] ? "Сохранён · можно заменить" : "Не настроен";
    document.querySelector(secret.clear).checked = false;
    document.querySelector(secret.clear).disabled = !state.keys[secret.field];
  }
  renderLLM(state);
  addChoices("#serial-ports", state.devices.serial);
  addChoices("#video-nodes", state.devices.video);
  routes.forEach(route => setRoute(route, state));
  renderFamily();
  renderDomains();
  for (const update of pickers.values()) update();
  updateVolume();
  dirty = false;
  syncSaveState();
  document.querySelector("#version").textContent = "Версия " + state.version;
  showNotice(state.pending
    ? "Настройки сохранены. Перезапустите аддон, чтобы применить изменения."
    : "Аддон запущен. Можно проверить устройства или изменить настройки.");
  controls.disabled = false;
}

for (const route of routes) {
  document.querySelector(route.select).addEventListener("change", event => {
    const input = form.elements.namedItem(route.field);
    const choice = event.target.value;
    if (choice === "__custom__") {
      input.closest("details").open = true;
      input.focus();
    } else {
      input.value = choice === "__off__" ? "" : "pulse";
    }
    changed();
  });
  form.elements.namedItem(route.field).addEventListener("input", event => {
    const select = document.querySelector(route.select);
    const device = event.target.value.trim();
    if (device === "pulse") {
      if (select.value.startsWith("__")) select.value = "";
    } else {
      select.value = device ? "__custom__" : "__off__";
    }
  });
}

async function persistSettings() {
  if (!current) return false;
  const options = {};
  for (const [key, previous] of Object.entries(current.options)) {
    const input = form.elements.namedItem(key);
    if (!input) continue;
    const value = fieldValue(key, input);
    if (value !== undefined && JSON.stringify(value) !== JSON.stringify(previous)) options[key] = value;
  }
  for (const secret of secrets) {
    const value = form.elements.namedItem(secret.field).value.trim();
    if (value || document.querySelector(secret.clear).checked) options[secret.field] = value;
  }
  if (profilesDirty) options.llm_profiles = readLLMProfiles();
  const audio = {};
  for (const route of routes) {
    const choice = document.querySelector(route.select).value;
    if (choice !== "__custom__" && choice !== "__off__" && (choice || null) !== current.audio[route.option]) {
      audio[route.option] = choice || null;
    }
  }
  controls.disabled = true;
  showNotice("Сохраняем настройки…");
  try {
    await request("settings", "PUT", {revision: current.revision, options, audio});
    render(await (await request("state")).json());
    return true;
  } catch (error) {
    showNotice(error.message, true);
    controls.disabled = false;
    return false;
  }
}
form.addEventListener("submit", event => {
  event.preventDefault();
  if (dirty) persistSettings();
});

restart.addEventListener("click", async () => {
  if (!current || (dirty && !await persistSettings())) return;
  if (!current.pending) return;
  const boot = current.boot;
  controls.disabled = true;
  showNotice("Аддон перезапускается…");
  try {
    await request("restart", "POST");
    for (let attempt = 0; attempt < 60; attempt += 1) {
      showNotice("Аддон перезапускается… " + Math.round(attempt * 1.5) + " с");
      await new Promise(resolve => setTimeout(resolve, 1500));
      try {
        const state = await (await request("state")).json();
        if (state.boot !== boot) {
          render(state);
          return;
        }
      } catch {
        // The ingress proxy can temporarily return 502 during a normal restart.
      }
    }
    throw new Error("Аддон не подтвердил перезапуск. Проверьте его состояние в Home Assistant.");
  } catch (error) {
    showNotice(error.message, true);
    controls.disabled = false;
  }
});

const microphoneSample = document.querySelector("#microphone-sample");
let microphoneSampleUrl;

async function runCheck(button) {
    const output = button.nextElementSibling;
    output.textContent = "Проверка…";
    output.dataset.error = "false";
    button.disabled = true;
    try {
      const input = button.dataset.check === "camera"
        ? {device: form.elements.namedItem("q1_video_device").value.trim()}
        : undefined;
      const response = await request("checks/" + button.dataset.check, "POST", input);
      if (button.dataset.check === "camera") {
        if (snapshotUrl) URL.revokeObjectURL(snapshotUrl);
        snapshotUrl = URL.createObjectURL(await response.blob());
        snapshot.src = snapshotUrl;
        snapshot.hidden = false;
        output.textContent = "Снимок получен.";
      } else if (button.dataset.check === "microphone_sample") {
        if (microphoneSampleUrl) URL.revokeObjectURL(microphoneSampleUrl);
        microphoneSampleUrl = URL.createObjectURL(await response.blob());
        microphoneSample.src = microphoneSampleUrl;
        microphoneSample.hidden = false;
        output.textContent = "Запись готова. Нажмите воспроизведение.";
      } else {
        output.textContent = (await response.json()).message;
      }
      return true;
    } catch (error) {
      output.textContent = error.message;
      output.dataset.error = "true";
      return false;
    } finally {
      button.disabled = false;
    }
}

for (const button of document.querySelectorAll("[data-check]")) {
  button.addEventListener("click", () => runCheck(button));
}

// Checks that are quick and silent; the 5-second recording and the speaker tone stay manual.
const CHECK_ALL = ["network", "modem", "gps", "camera", "microphone", "speech", "deepseek"];
const checkAllButton = document.querySelector("#check-all");
const checkAllResult = document.querySelector("#check-all-result");
checkAllButton.addEventListener("click", async () => {
  checkAllButton.disabled = true;
  checkAllResult.dataset.error = "false";
  const failed = [];
  let passed = 0;
  for (const name of CHECK_ALL) {
    const button = document.querySelector(`[data-check="${name}"]`);
    if (!button) continue;
    checkAllResult.textContent = "Проверяю: " + button.textContent + "…";
    if (await runCheck(button)) passed += 1;
    else failed.push(button.textContent);
  }
  checkAllResult.textContent = failed.length
    ? `✓ ${passed}, ✗ ${failed.length}: ${failed.join(", ")}`
    : `✓ Все ${passed} проверок пройдены.`;
  checkAllResult.dataset.error = failed.length ? "true" : "false";
  checkAllButton.disabled = false;
});

const sandboxText = document.querySelector("#sandbox-text");
const sandboxAsk = document.querySelector("#sandbox-ask");
const sandboxSpeak = document.querySelector("#sandbox-speak");
const sandboxStatus = document.querySelector("#sandbox-status");
const sandboxReply = document.querySelector("#sandbox-reply");
const sandboxAudio = document.querySelector("#sandbox-audio");
let sandboxAudioUrl;

function sandboxError(error) {
  sandboxStatus.textContent = error.message;
  sandboxStatus.dataset.error = "true";
}

sandboxText.addEventListener("keydown", event => {
  if (event.key === "Enter") { event.preventDefault(); sandboxAsk.click(); }
});

sandboxAsk.addEventListener("click", async () => {
  const text = sandboxText.value.trim();
  if (!text) { sandboxText.focus(); return; }
  sandboxAsk.disabled = true;
  sandboxSpeak.disabled = true;
  sandboxStatus.dataset.error = "false";
  sandboxStatus.textContent = "Модель думает…";
  sandboxReply.textContent = "";
  try {
    const prompt = form.elements.namedItem("system_prompt").value;
    const response = await request("sandbox", "POST", {text, prompt});
    sandboxReply.textContent = (await response.json()).reply;
    sandboxStatus.textContent = "";
    sandboxSpeak.disabled = false;
  } catch (error) {
    sandboxError(error);
  } finally {
    sandboxAsk.disabled = false;
  }
});

sandboxSpeak.addEventListener("click", async () => {
  const text = sandboxReply.textContent.trim();
  if (!text) return;
  sandboxSpeak.disabled = true;
  sandboxStatus.dataset.error = "false";
  sandboxStatus.textContent = "Озвучиваю…";
  try {
    const response = await request("sandbox/speak", "POST", {text});
    if (sandboxAudioUrl) URL.revokeObjectURL(sandboxAudioUrl);
    sandboxAudioUrl = URL.createObjectURL(await response.blob());
    sandboxAudio.src = sandboxAudioUrl;
    sandboxAudio.hidden = false;
    sandboxStatus.textContent = "";
    sandboxAudio.play().catch(() => {});
  } catch (error) {
    sandboxError(error);
  } finally {
    sandboxSpeak.disabled = false;
  }
});

const phone = document.querySelector("#phone");
const phoneStatus = document.querySelector("#phone-status");
const phoneResult = document.querySelector("#phone-result");
const phoneButtons = Array.from(document.querySelectorAll("[data-phone]"));
const volteButton = document.querySelector("#volte");
const phoneStates = {
  offline: "Модем недоступен",
  idle: "Готов к вызову",
  ringing: "Входящий звонок",
  dialing: "Исходящий звонок",
  answering: "Подключение",
  connected: "Разговор",
};
let phoneBusy = false;
let phoneTicket = 0;
let phoneInFlight = false;

async function refreshPhone() {
  if ((phone.hidden && document.querySelector('[data-view="hardware"]').hidden) || phoneBusy || phoneInFlight || document.hidden) return;
  const ticket = ++phoneTicket;
  phoneInFlight = true;
  try {
    const state = await (await request("phone")).json();
    if (ticket !== phoneTicket) return;
    phoneStatus.textContent = (phoneStates[state.state] || state.state)
      + " · автоответ " + (state.auto_answer ? "включён" : "выключен");
    const previous = phoneRecipient.value;
    const choices = [
      ...(state.owner_available ? [new Option("Владелец", "")] : []),
      ...state.recipients.map(item => typeof item === "string"
        ? new Option(item, item) : new Option(item.name, item.key)),
    ];
    if (!choices.length) choices.push(new Option("Добавьте номер в настройках", ""));
    const signature = choices.map(option => option.textContent).join("\n");
    if (phoneRecipient.dataset.signature !== signature) {
      phoneRecipient.replaceChildren(...choices);
      phoneRecipient.dataset.signature = signature;
      if (choices.some(option => option.value === previous)) phoneRecipient.value = previous;
    }
    for (const button of phoneButtons) button.disabled = !state["can_" + button.dataset.phone];
    volteButton.disabled = state.state !== "idle";
  } catch {
    if (ticket !== phoneTicket) return;
    phoneStatus.textContent = "Состояние модема недоступно.";
    for (const button of phoneButtons) button.disabled = true;
    volteButton.disabled = true;
  } finally {
    phoneInFlight = false;
  }
}

volteButton.addEventListener("click", async () => {
  phoneBusy = true;
  volteButton.disabled = true;
  const result = document.querySelector("#volte-result");
  result.dataset.error = "false";
  try {
    result.textContent = (await (await request("phone/volte", "POST")).json()).message;
  } catch (error) {
    result.textContent = error.message;
    result.dataset.error = "true";
  } finally {
    phoneBusy = false;
    await refreshPhone();
  }
});

const historyType = document.querySelector("#history-type");
const historyStatus = document.querySelector("#history-status");
const historyItems = document.querySelector("#history-items");
const historyNewer = document.querySelector("#history-newer");
const historyPage = document.querySelector("#history-page");
const historyOlder = document.querySelector("#history-older");
const historyStates = {
  ringing: "Звонит", dialing: "Набор", connected: "Разговор",
  missed: "Пропущен", failed: "Без соединения", completed: "Завершён",
  interrupted: "Прерван", received: "Получено",
};
let historyOffset = 0;
let historyPageSize = 50;
let historyHasMore = false;
let historyRequest = 0;

async function loadHistory(offset = 0) {
  const ticket = ++historyRequest;
  const params = new URLSearchParams();
  if (historyType.value) params.set("type", historyType.value);
  params.set("offset", offset);
  historyNewer.disabled = true;
  historyOlder.disabled = true;
  try {
    const result = await (await request("history?" + params)).json();
    if (ticket !== historyRequest) return;
    historyPageSize = result.page_size;
    const pageCount = Math.max(1, Math.ceil(result.total / historyPageSize));
    if (offset >= pageCount * historyPageSize) {
      await loadHistory((pageCount - 1) * historyPageSize);
      return;
    }
    historyOffset = offset;
    historyHasMore = result.more;
    historyPage.textContent = "Страница " + (Math.floor(offset / historyPageSize) + 1) + " из " + pageCount;
    historyItems.replaceChildren();
    for (const item of result.items) {
      const row = document.createElement("li");
      const heading = document.createElement("strong");
      const time = new Date(item.created_at * 1000).toLocaleString();
      const direction = item.direction === "incoming" ? "Входящий звонок" : "Исходящий звонок";
      const state = historyStates[item.state] || item.state || "";
      const duration = item.connected_at && item.ended_at
        ? " · " + Math.max(0, Math.round(item.ended_at - item.connected_at)) + " с"
        : "";
      heading.textContent = time + " · " + (item.kind === "sms" ? "SMS" : direction)
        + " · " + (item.number || "Номер скрыт") + " · " + state + duration;
      row.append(heading);
      if (item.text) {
        const body = document.createElement("p");
        body.textContent = item.text;
        row.append(body);
      }
      historyItems.append(row);
    }
    const storage = Object.entries(result.sms.storage || {})
      .map(([name, count]) => name + " " + count.used + "/" + count.total).join(" · ");
    historyStatus.dataset.error = String(Boolean(result.sms.error));
    historyStatus.textContent = result.sms.error || [
      result.total ? "Записей: " + result.total : "Записей пока нет.",
      storage ? "Память SMS: " + storage : "",
    ].filter(Boolean).join(" ");
  } catch (error) {
    if (ticket !== historyRequest) return;
    historyStatus.textContent = error.message;
    historyStatus.dataset.error = "true";
  } finally {
    if (ticket === historyRequest) {
      historyNewer.disabled = historyOffset === 0;
      historyOlder.disabled = !historyHasMore;
    }
  }
}

historyType.addEventListener("change", () => loadHistory());
historyNewer.addEventListener("click", () => loadHistory(historyOffset - historyPageSize));
historyOlder.addEventListener("click", () => loadHistory(historyOffset + historyPageSize));
document.querySelector("#history-refresh").addEventListener("click", async () => {
  try {
    await request("history/refresh", "POST");
    await new Promise(resolve => setTimeout(resolve, 1200));
  } catch (error) {
    historyStatus.textContent = error.message;
    historyStatus.dataset.error = "true";
    return;
  }
  await loadHistory(historyOffset);
});
loadHistory();

setTab(location.hash.slice(1));
setInterval(refreshPhone, 3000);
for (const button of phoneButtons) {
  button.addEventListener("click", async () => {
    phoneBusy = true;
    for (const control of phoneButtons) control.disabled = true;
    phoneResult.textContent = "Команда отправляется…";
    phoneResult.dataset.error = "false";
    try {
      const member = phoneRecipient.value;
      await request("phone/" + button.dataset.phone, "POST",
        button.dataset.phone === "dial" && member ? {member} : undefined);
      phoneResult.textContent = button.dataset.phone === "dial"
        ? "Модем принял команду вызова. Состояние обновится ниже."
        : "Команда принята.";
    } catch (error) {
      phoneResult.textContent = error.message;
      phoneResult.dataset.error = "true";
    } finally {
      phoneBusy = false;
      await refreshPhone();
    }
  });
}

request("state")
  .then(response => response.json())
  .then(render)
  .catch(error => showNotice("Не удалось загрузить настройки. " + error.message, true));


// Administrator-maintained vocabulary; descriptive fields never grant control access.
let homeVocabulary = {}, homeCatalog = [], knowledgeTicket = 0;
const knowledgeStatus = document.querySelector("#knowledge-status");
const knowledgeEntity = document.querySelector("#knowledge-entity");
function fillKnowledgeEditor() {
  const item = homeVocabulary[knowledgeEntity.value] || {};
  document.querySelector("#knowledge-aliases").value = (item.aliases || []).join("\n");
  document.querySelector("#knowledge-room").value = item.room || "";
  document.querySelector("#knowledge-purpose").value = item.purpose || "";
  document.querySelector("#knowledge-save").disabled = !knowledgeEntity.value;
  document.querySelector("#knowledge-remove").disabled = !homeVocabulary[knowledgeEntity.value];
}
function renderKnowledge() {
  const selected = knowledgeEntity.value;
  const search = normalized(document.querySelector("#knowledge-search").value).trim();
  const rows = homeCatalog.filter(row => !search || normalized([row.id,row.name,row.area,...(homeVocabulary[row.id]?.aliases || [])].join(" ")).includes(search));
  knowledgeEntity.replaceChildren(...rows.map(row => new Option([row.name,row.area,row.id].filter(Boolean).join(" · "),row.id)));
  if (rows.some(row => row.id === selected)) knowledgeEntity.value = selected;
  fillKnowledgeEditor();
  const list = document.querySelector("#knowledge-list");
  list.replaceChildren();
  for (const row of rows.filter(row => homeVocabulary[row.id])) {
    const item = homeVocabulary[row.id];
    const card = document.createElement("article");card.className = "card";
    const name = document.createElement("h3");name.textContent = row.name;
    const details = document.createElement("p");details.textContent = [row.id,item.room,item.purpose,...item.aliases].filter(Boolean).join(" · ");
    const edit = document.createElement("button");edit.type="button";edit.textContent="Изменить";
    edit.addEventListener("click",()=>{knowledgeEntity.value=row.id;fillKnowledgeEditor();knowledgeEntity.focus();});
    card.append(name,details,edit);list.append(card);
  }
}
async function loadKnowledge() {
  const ticket=++knowledgeTicket;
  knowledgeStatus.textContent="Загружаем карту дома…";
  try {
    const [vocabulary,devices]=await Promise.all([request("knowledge").then(r=>r.json()),request("entities").then(r=>r.json())]);
    if(ticket!==knowledgeTicket)return;
    homeVocabulary=vocabulary.entities || {};homeCatalog=devices.entities || [];
    renderKnowledge();knowledgeStatus.textContent="Описания: "+Object.keys(homeVocabulary).length;
  } catch(error){knowledgeStatus.textContent=error.message;}
}
async function updateKnowledge(remove=false) {
  const entity=knowledgeEntity.value;if(!entity)return;
  const save=document.querySelector("#knowledge-save");const clear=document.querySelector("#knowledge-remove");
  save.disabled=clear.disabled=true;
  try {
    await request("knowledge/"+encodeURIComponent(entity),remove?"DELETE":"PATCH",remove?undefined:{
      aliases:document.querySelector("#knowledge-aliases").value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean),
      room:document.querySelector("#knowledge-room").value.trim(),purpose:document.querySelector("#knowledge-purpose").value.trim()});
    await loadKnowledge();knowledgeStatus.textContent=remove?"Описание удалено. Используются данные Home Assistant.":"Описание сохранено и доступно Джарвису.";
  } catch(error){knowledgeStatus.textContent=error.message;}
  finally{fillKnowledgeEditor();}
}
knowledgeEntity.addEventListener("change",fillKnowledgeEditor);
document.querySelector("#knowledge-search").addEventListener("input",renderKnowledge);
document.querySelector("#knowledge-refresh").addEventListener("click",loadKnowledge);
document.querySelector("#knowledge-save").addEventListener("click",()=>updateKnowledge());
document.querySelector("#knowledge-remove").addEventListener("click",()=>updateKnowledge(true));
