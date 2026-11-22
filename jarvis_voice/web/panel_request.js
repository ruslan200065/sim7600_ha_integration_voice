"use strict";

async function request(path, method = "GET", data, signal) {
  let response;
  try {
    response = await fetch(path, {
      method,
      signal,
      cache: "no-store",
      headers: {"Content-Type": "application/json", "X-Jarvis-Setup": "1"},
      body: data === undefined ? undefined : JSON.stringify(data),
    });
  } catch (error) {
    if (signal?.aborted) throw error;
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
