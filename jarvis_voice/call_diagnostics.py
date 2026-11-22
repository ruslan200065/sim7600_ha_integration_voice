"""Explain observable call stages without claiming what a human heard."""

from datetime import datetime

STAGES = {
    "recognized": "stt",
    "reply": "llm",
    "playback": "playback",
    "connection": "connection",
    "audio_rx": "audio_rx",
    "stt": "stt",
    "llm": "llm",
    "tts": "tts",
}
LABELS = {
    "connection": "Соединение",
    "audio_rx": "Приём аудио от модема",
    "stt": "Распознавание речи",
    "llm": "Ответ модели",
    "tts": "Синтез речи",
    "playback": "Передача аудио в модем",
}


def analyze_timeline(entries, call_id=None):
    if isinstance(entries, dict):
        entries = entries.get("items", [])
    rows = [
        row
        for row in entries
        if isinstance(row, dict)
        and (call_id is None or str(row.get("call_id")) == str(call_id))
    ]
    rows.sort(key=lambda row: (str(row.get("at", "")), row.get("id", 0)))
    stages = {
        key: {
            "stage": key,
            "label": label,
            "status": "unknown",
            "evidence": [],
            "duration_ms": None,
        }
        for key, label in LABELS.items()
    }
    failures = []
    connected_at = first_audio_at = None
    for row in rows:
        stage = STAGES.get(row.get("stage"))
        if not stage:
            continue
        status = str(row.get("status", "unknown"))
        if stage == "connection" and status == "connected" and connected_at is None:
            connected_at = row.get("at")
        if stage == "playback" and status == "sent" and first_audio_at is None:
            first_audio_at = row.get("at")
        item = stages[stage]
        item["status"] = status
        item["duration_ms"] = row.get("duration_ms")
        item["evidence"].append(
            {"id": row.get("id"), "at": row.get("at"), "status": status}
        )
        item["evidence"] = item["evidence"][-4:]
        if status in {"failed", "error", "timeout", "lost", "exhausted"}:
            failures.append(
                {
                    "stage": stage,
                    "label": item["label"],
                    "status": status,
                    "event_id": row.get("id"),
                }
            )
    unresolved = [
        failure
        for failure in failures
        if stages[failure["stage"]]["status"]
        in {"failed", "error", "timeout", "lost", "exhausted"}
    ]
    failure = unresolved[-1] if unresolved else None
    if failure:
        reason = failure["label"] + ": " + failure["status"]
        status = "failure_observed"
    elif stages["playback"]["status"] in {"sent", "completed", "ready"}:
        reason = "Аудио передано модему. Слышимость в трубке этим не подтверждается."
        status = "recovered" if failures else "audio_sent"
        if failures:
            reason = "После зарегистрированного сбоя аудио передано модему. Слышимость в трубке этим не подтверждается."
    elif not rows:
        reason = "В журнале нет данных об этом звонке."
        status = "no_data"
    else:
        reason = "Недостаточно данных для подтверждения полного голосового тракта."
        status = "incomplete"
    first_audio_ms = None
    if isinstance(connected_at, str) and isinstance(first_audio_at, str):
        try:
            elapsed = datetime.fromisoformat(first_audio_at) - datetime.fromisoformat(
                connected_at
            )
        except (ValueError, TypeError):
            pass
        else:
            if elapsed.total_seconds() >= 0:
                first_audio_ms = round(elapsed.total_seconds() * 1000)
    return {
        "first_audio_ms": first_audio_ms,
        "call_id": call_id,
        "status": status,
        "reason": reason,
        "failure": failure,
        "stages": list(stages.values()),
        "past_failures": failures,
        "audibility_confirmed": False,
    }
