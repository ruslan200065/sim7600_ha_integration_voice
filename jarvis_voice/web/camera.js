(() => {
  const view = document.querySelector('[data-view="camera"]');
  const feed = document.querySelector("#camera-feed");
  const placeholder = document.querySelector("#camera-placeholder");
  const toggle = document.querySelector("#camera-toggle");
  const save = document.querySelector("#camera-save");
  const status = document.querySelector("#camera-status");
  const format = document.querySelector("#camera-format");
  const size = document.querySelector("#camera-size");
  const fps = document.querySelector("#camera-fps");
  const settings = document.querySelector("#camera-controls");
  const labels = {
    brightness: "Яркость", contrast: "Контрастность", saturation: "Насыщенность",
    hue: "Оттенок", gamma: "Гамма", gain: "Усиление", sharpness: "Резкость",
    backlight_compensation: "Компенсация подсветки", exposure_auto: "Режим экспозиции",
    exposure_absolute: "Экспозиция", white_balance_temperature_auto: "Автобаланс белого",
    white_balance_temperature: "Баланс белого", power_line_frequency: "Частота сети",
    focus_absolute: "Фокус", focus_auto: "Автофокус", zoom_absolute: "Масштаб",
  };
  let modes = [];
  let socket;
  let frameUrl;
  let generation = 0;
  let saving = false;

  function report(message, error = false) {
    status.textContent = message;
    status.dataset.error = String(error);
  }

  function setOptions(select, values, value) {
    select.replaceChildren(...values.map(([id, caption]) => new Option(caption, id)));
    if ([...select.options].some(option => option.value === String(value))) {
      select.value = String(value);
    }
    select.disabled = !values.length;
  }

  function sizeKey(mode) {
    return mode.width + "×" + mode.height;
  }

  function selectMode(preferred) {
    const formats = [...new Set(modes.map(mode => mode.format))];
    setOptions(format, formats.map(key => [key, key === "MJPG" ? "MJPEG (сжатие камерой)" : key]), preferred?.format);
    const matching = modes.filter(mode => mode.format === format.value);
    setOptions(size, [...new Set(matching.map(sizeKey))].map(key => [key, key]), preferred && sizeKey(preferred));
    const rates = matching.filter(mode => sizeKey(mode) === size.value).map(mode => mode.fps);
    setOptions(fps, [...new Set(rates)].sort((a, b) => a - b).map(rate => [rate, rate]), preferred?.fps);
    save.disabled = !modes.length || saving;
    toggle.disabled = !modes.length || saving;
  }

  function selectedMode() {
    return modes.find(mode => mode.format === format.value && sizeKey(mode) === size.value && String(mode.fps) === fps.value);
  }

  function renderControls(controls) {
    settings.replaceChildren(...controls.map(control => {
      const label = document.createElement("label");
      label.textContent = labels[control.id] || control.id.replaceAll("_", " ");
      const input = document.createElement(control.choices.length ? "select" : "input");
      if (control.choices.length) {
        setOptions(input, control.choices.map(choice => [choice.value, choice.label]), control.value);
      } else if (control.type === "bool") {
        input.type = "checkbox";
        input.checked = Boolean(control.value);
      } else {
        input.type = "range";
        input.min = control.min;
        input.max = control.max;
        input.step = control.step || 1;
        input.value = control.value;
        const value = document.createElement("output");
        value.textContent = input.value;
        input.addEventListener("input", () => value.textContent = input.value);
        label.append(value);
      }
      input.disabled = control.inactive;
      label.append(input);
      input.addEventListener("change", async () => {
        const ticket = generation;
        input.disabled = true;
        try {
          await request("camera", "PATCH", {
            control: control.id,
            value: control.type === "bool" ? Number(input.checked) : Number(input.value),
          });
          if (ticket === generation && visible()) await loadCamera(selectedMode());
        } catch (error) {
          if (ticket === generation) {
            await loadCamera(selectedMode());
            report(error.message, true);
          }
        } finally {
          input.disabled = control.inactive;
        }
      });
      return label;
    }));
  }

  function visible() {
    return !view.hidden && !document.hidden;
  }

  async function loadCamera(preferred) {
    const ticket = ++generation;
    try {
      const data = await (await request("camera")).json();
      if (ticket !== generation || !visible()) return;
      modes = data.modes;
      selectMode(preferred || data.mode);
      renderControls(data.controls);
      if (!modes.length) report("Поддерживаемые режимы видео не найдены.", true);
      else if (!socket) report(data.device);
    } catch (error) {
      if (ticket === generation && visible()) {
        modes = [];
        selectMode();
        settings.replaceChildren();
        report(error.message, true);
      }
    }
  }

  function clearFrame() {
    feed.onload = feed.onerror = null;
    feed.removeAttribute("src");
    feed.hidden = true;
    placeholder.hidden = false;
    placeholder.textContent = "Предпросмотр выключен";
    if (frameUrl) URL.revokeObjectURL(frameUrl);
    frameUrl = undefined;
  }

  function stop() {
    const previous = socket;
    socket = undefined;
    if (previous) previous.close();
    toggle.textContent = "Включить предпросмотр";
    clearFrame();
    return previous;
  }

  function start() {
    if (!visible() || toggle.disabled || socket) return;
    const url = new URL("camera/preview", location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    const viewer = new WebSocket(url);
    viewer.binaryType = "blob";
    socket = viewer;
    toggle.textContent = "Остановить предпросмотр";
    placeholder.textContent = "Подключение к камере…";
    report("Подключение…");
    let measuredAt = performance.now();
    let frames = 0;
    viewer.onmessage = ({data}) => {
      if (socket !== viewer) return;
      if (typeof data === "string") {
        try {
          report(JSON.parse(data).error || "Ошибка камеры.", true);
        } catch {
          report("Ошибка камеры.", true);
        }
        stop();
        return;
      }
      if (frameUrl) URL.revokeObjectURL(frameUrl);
      frameUrl = URL.createObjectURL(new Blob([data], {type: "image/jpeg"}));
      const next = () => {
        if (socket === viewer && viewer.readyState === WebSocket.OPEN) viewer.send("next");
      };
      feed.onload = () => {
        const first = feed.hidden;
        feed.hidden = false;
        placeholder.hidden = true;
        const now = performance.now();
        ++frames;
        if (first) {
          report("");
          loadCamera(selectedMode());
        }
        if (now - measuredAt >= 1000) {
          report((frames * 1000 / (now - measuredAt)).toFixed(1) + " кадр/с");
          frames = 0;
          measuredAt = now;
        }
        next();
      };
      feed.onerror = next;
      feed.src = frameUrl;
    };
    viewer.onclose = () => {
      if (socket !== viewer) return;
      stop();
      report("Связь с камерой прервана. Запустите предпросмотр снова.", true);
    };
  }

  toggle.addEventListener("click", () => {
    if (socket) {
      stop();
      report("");
    } else start();
  });
  format.addEventListener("change", () => selectMode({format: format.value}));
  size.addEventListener("change", () => {
    const mode = modes.find(row => row.format === format.value && sizeKey(row) === size.value);
    selectMode(mode);
  });
  save.addEventListener("click", async () => {
    const mode = selectedMode();
    if (!mode) return;
    saving = true;
    save.disabled = toggle.disabled = true;
    const restart = Boolean(socket);
    try {
      await request("camera", "PATCH", {mode});
      if (restart && socket) {
        const previous = stop();
        await new Promise(resolve => {
          if (previous.readyState === WebSocket.CLOSED) resolve();
          else {
            previous.addEventListener("close", resolve, {once: true});
            setTimeout(resolve, 800);
          }
        });
      }
      report("Режим сохранён.");
    } catch (error) {
      report(error.message, true);
      return;
    } finally {
      saving = false;
      save.disabled = toggle.disabled = !modes.length;
    }
    if (restart) start();
  });
  document.addEventListener("jarvis-tab", () => {
    if (visible()) loadCamera();
    else {
      ++generation;
      stop();
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (!visible()) {
      ++generation;
      stop();
    } else loadCamera();
  });
  window.addEventListener("pagehide", stop);
  if (visible()) loadCamera();
})();
