"use strict";

(() => {
  const phone = document.querySelector("#phone");
  const hardware = document.querySelector('[data-view="hardware"]');
  const history = document.querySelector('[data-view="history"]');
  const phoneRecipient = document.querySelector("#phone-recipient");
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
  let phoneRequest;
  let phoneTimer;

  function phoneVisible() {
    return !document.hidden && (!phone.hidden || !hardware.hidden);
  }

  function updatePolling() {
    clearTimeout(phoneTimer);
    if (phoneVisible() && !phoneBusy) {
      void refreshPhone();
    } else {
      phoneRequest?.abort();
      phoneRequest = undefined;
    }
  }

  async function refreshPhone() {
    if (!phoneVisible() || phoneBusy || phoneRequest) return;
    clearTimeout(phoneTimer);
    const controller = new AbortController();
    phoneRequest = controller;
    try {
      const state = await (await request("phone", "GET", undefined, controller.signal)).json();
      if (controller.signal.aborted) return;
      phoneStatus.textContent = (phoneStates[state.state] || state.state)
        + " · автоответ " + (state.auto_answer ? "включён" : "выключен");
      const previous = phoneRecipient.value;
      const choices = [
        ...(state.owner_available ? [new Option("Владелец", "")] : []),
        ...state.recipients.map(item => typeof item === "string"
          ? new Option(item, item) : new Option(item.name, item.key)),
      ];
      if (!choices.length) choices.push(new Option("Добавьте номер в настройках", ""));
      const signature = JSON.stringify(choices.map(option => [option.value, option.textContent]));
      if (phoneRecipient.dataset.signature !== signature) {
        phoneRecipient.replaceChildren(...choices);
        phoneRecipient.dataset.signature = signature;
        if (choices.some(option => option.value === previous)) phoneRecipient.value = previous;
      }
      for (const button of phoneButtons) button.disabled = !state["can_" + button.dataset.phone];
      volteButton.disabled = state.state !== "idle";
    } catch {
      if (controller.signal.aborted) return;
      phoneStatus.textContent = "Состояние модема недоступно.";
      for (const button of phoneButtons) button.disabled = true;
      volteButton.disabled = true;
    } finally {
      if (phoneRequest === controller) {
        phoneRequest = undefined;
        if (phoneVisible() && !phoneBusy) phoneTimer = setTimeout(refreshPhone, 3000);
      }
    }
  }

  volteButton.addEventListener("click", async () => {
    phoneBusy = true;
    updatePolling();
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
  for (const button of document.querySelectorAll("[data-clear-history]")) {
    button.addEventListener("click", async () => {
      if (!window.confirm(button.textContent + "?")) return;
      button.disabled = true;
      try {
        const response = await request("history/" + button.dataset.clearHistory, "DELETE");
        const result = await response.json();
        await loadHistory(0);
        historyStatus.dataset.error = "false";
        historyStatus.textContent = "Удалено записей или файлов: " + result.cleared;
      } catch (error) {
        historyStatus.dataset.error = "true";
        historyStatus.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    });
  }
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
  for (const button of phoneButtons) {
    button.addEventListener("click", async () => {
      phoneBusy = true;
    updatePolling();
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

  document.addEventListener("jarvis-tab", () => {
    updatePolling();
    if (!history.hidden && !document.hidden) void loadHistory(historyOffset);
  });
  document.addEventListener("visibilitychange", () => {
    updatePolling();
    if (!history.hidden && !document.hidden) void loadHistory(historyOffset);
  });
  window.addEventListener("pagehide", () => {
    clearTimeout(phoneTimer);
    phoneRequest?.abort();
    phoneRequest = undefined;
  });
  updatePolling();
  if (!history.hidden && !document.hidden) void loadHistory(historyOffset);
})();
