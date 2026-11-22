"use strict";

(() => {
  const card = document.querySelector("#companion-setup");
  const status = card.querySelector("#companion-status");
  const discover = card.querySelector("#companion-discover");
  const reveal = card.querySelector("#companion-reveal");
  const values = card.querySelector("#companion-values");
  const fields = ["host", "port", "token"];
  let requestId = 0;

  function report(message, error = false) {
    status.textContent = message;
    status.dataset.error = String(error);
  }

  async function refresh() {
    const id = ++requestId;
    try {
      const info = await (await request("companion")).json();
      if (id !== requestId) return;
      if (!info.installed_version) {
        report(info.directory_exists
          ? "Папка интеграции есть, но манифест не читается. Проверь инструкцию приложения."
          : info.auto_install
            ? "Файлы интеграции не установлены. Проверь журнал запуска приложения."
            : "Включи auto_install_integration в настройках приложения и перезапусти его.", true);
        return;
      }
      const installed = "Файлы интеграции: " + info.installed_version + ".";
      if (info.bundled_version && info.bundled_version !== info.installed_version) {
        report(installed + " В комплекте версия " + info.bundled_version +
          (info.managed ? ". Перезапусти приложение." : ". Обнови вручную файлы интеграции."), true);
      } else {
        report(installed + " Для загрузки новых файлов перезапусти Core.");
      }
    } catch (error) {
      if (id === requestId) report(error.message, true);
    }
  }

  function hideCredentials() {
    values.hidden = true;
    for (const field of fields) card.querySelector("#companion-" + field).value = "";
  }

  discover.addEventListener("click", async () => {
    ++requestId;
    discover.disabled = true;
    try {
      await request("companion/discover", "POST");
      report("Запрос отправлен. Проверь «Устройства и службы».");
    } catch (error) {
      report(error.message, true);
    } finally {
      discover.disabled = false;
    }
  });

  const manual = card.querySelector("#companion-manual");
  manual.addEventListener("toggle", () => {
    if (!manual.open) hideCredentials();
  });
  reveal.addEventListener("click", async () => {
    reveal.disabled = true;
    const id = requestId;
    try {
      const data = await (await request("companion/credentials", "POST")).json();
      if (!manual.open || id !== requestId) return;
      for (const field of fields) card.querySelector("#companion-" + field).value = data[field];
      values.hidden = false;
    } catch (error) {
      report(error.message, true);
    } finally {
      reveal.disabled = false;
    }
  });

  document.addEventListener("jarvis-tab", event => {
    if (event.detail === "voice") void refresh();
    else {
      ++requestId;
      hideCredentials();
    }
  });
  window.addEventListener("pagehide", hideCredentials);
  if (location.hash === "#voice") void refresh();
})();
