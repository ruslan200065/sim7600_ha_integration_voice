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
  {field: "yandex_api_key", hint: "#speech-key-hint", clear: "#clear-speech"},
  {field: "deepseek_api_key", hint: "#deepseek-key-hint", clear: "#clear-deepseek"},
];
let current;
let snapshotUrl;

async function request(path, method = "GET", data) {
  const response = await fetch(path, {
    method,
    cache: "no-store",
    headers: {"Content-Type": "application/json", "X-Jarvis-Setup": "1"},
    body: data === undefined ? undefined : JSON.stringify(data),
  });
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
  if (["allowed_entities", "observed_entities", "allow_domains"].includes(key)) {
    return element.value.split(/\s+/).filter(Boolean);
  }
  if (element.type === "checkbox") return element.checked;
  if (element.type === "number") return element.value === "" ? undefined : Number(element.value);
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
  addChoices("#serial-ports", state.devices.serial);
  addChoices("#video-nodes", state.devices.video);
  routes.forEach(route => setRoute(route, state));
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

form.addEventListener("submit", async event => {
  event.preventDefault();
  if (!current) return;
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
  } catch (error) {
    showNotice(error.message, true);
    controls.disabled = false;
  }
});

restart.addEventListener("click", async () => {
  if (!current) return;
  const boot = current.boot;
  controls.disabled = true;
  showNotice("Аддон перезапускается…");
  try {
    await request("restart", "POST");
    for (let attempt = 0; attempt < 24; attempt += 1) {
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

for (const button of document.querySelectorAll("[data-check]")) {
  button.addEventListener("click", async () => {
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
    } catch (error) {
      output.textContent = error.message;
      output.dataset.error = "true";
    } finally {
      button.disabled = false;
    }
  });
}

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

async function refreshPhone() {
  if (!phone.open || phoneBusy || document.hidden) return;
  try {
    const state = await (await request("phone")).json();
    phoneStatus.textContent = (phoneStates[state.state] || state.state)
      + " · автоответ " + (state.auto_answer ? "включён" : "выключен");
    for (const button of phoneButtons) button.disabled = !state["can_" + button.dataset.phone];
    volteButton.disabled = state.state !== "idle";
  } catch {
    phoneStatus.textContent = "Состояние модема недоступно.";
    for (const button of phoneButtons) button.disabled = true;
    volteButton.disabled = true;
  }
}

volteButton.addEventListener("click", async () => {
  phoneBusy = true;
  volteButton.disabled = true;
  phoneResult.dataset.error = "false";
  try {
    phoneResult.textContent = (await (await request("phone/volte", "POST")).json()).message;
  } catch (error) {
    phoneResult.textContent = error.message;
    phoneResult.dataset.error = "true";
  } finally {
    phoneBusy = false;
    await refreshPhone();
  }
});

const historyType = document.querySelector("#history-type");
const historyStatus = document.querySelector("#history-status");
const historyItems = document.querySelector("#history-items");
const historyOlder = document.querySelector("#history-older");
const historyStates = {
  ringing: "Звонит", dialing: "Набор", connected: "Разговор",
  missed: "Пропущен", failed: "Без соединения", completed: "Завершён",
  interrupted: "Прерван", received: "Получено",
};
let historyOffset = 0;
let historyRequest = 0;

async function loadHistory(append = false) {
  const ticket = ++historyRequest;
  const params = new URLSearchParams();
  if (historyType.value) params.set("type", historyType.value);
  if (append) params.set("offset", historyOffset);
  historyOlder.disabled = true;
  try {
    const result = await (await request("history?" + params)).json();
    if (ticket !== historyRequest) return;
    if (!append) {
      historyItems.replaceChildren();
      historyOffset = 0;
    }
    for (const item of result.items) {
      const row = document.createElement("li");
      const heading = document.createElement("strong");
      const time = new Date(item.created_at * 1000).toLocaleString();
      const direction = item.direction === "incoming" ? "Входящий звонок" : "Исходящий звонок";
      const state = historyStates[item.state] || ("Частей " + item.state);
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
    historyOffset += result.items.length;
    const storage = Object.entries(result.sms.storage || {})
      .map(([name, count]) => name + " " + count.used + "/" + count.total).join(" · ");
    historyStatus.dataset.error = String(Boolean(result.sms.error));
    historyStatus.textContent = result.sms.error || [
      historyOffset ? "" : "Записей пока нет.",
      storage ? "Память SMS: " + storage : "",
    ].filter(Boolean).join(" ");
    historyOlder.hidden = !result.more;
  } catch (error) {
    if (ticket !== historyRequest) return;
    historyStatus.textContent = error.message;
    historyStatus.dataset.error = "true";
  } finally {
    if (ticket === historyRequest) historyOlder.disabled = false;
  }
}

historyType.addEventListener("change", () => loadHistory());
historyOlder.addEventListener("click", () => loadHistory(true));
document.querySelector("#history-refresh").addEventListener("click", async () => {
  try {
    await request("history/refresh", "POST");
    await new Promise(resolve => setTimeout(resolve, 1200));
  } catch (error) {
    historyStatus.textContent = error.message;
    historyStatus.dataset.error = "true";
    return;
  }
  await loadHistory();
});
loadHistory();

phone.addEventListener("toggle", refreshPhone);
setInterval(refreshPhone, 3000);
for (const button of phoneButtons) {
  button.addEventListener("click", async () => {
    phoneBusy = true;
    for (const control of phoneButtons) control.disabled = true;
    phoneResult.textContent = "Команда отправляется…";
    phoneResult.dataset.error = "false";
    try {
      await request("phone/" + button.dataset.phone, "POST");
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
