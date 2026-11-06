"""DeepSeek tool calling, HA entity allowlist and on-demand visual inspection."""
import base64
import json
import logging
import asyncio
import os
import aiohttp

LOG = logging.getLogger(__name__)

TOOLS = [
    {"type":"function","function":{"name":"get_entity_state","description":"Read Home Assistant entity status","parameters":{"type":"object","properties":{"entity_id":{"type":"string"}},"required":["entity_id"]}}},
    {"type":"function","function":{"name":"call_ha_service","description":"Execute a permitted Home Assistant service on permitted entities only","parameters":{"type":"object","properties":{"domain":{"type":"string"},"service":{"type":"string"},"entity_id":{"type":"string"}},"required":["domain","service","entity_id"]}}},
    {"type":"function","function":{"name":"see_camera","description":"Inspect configured camera snapshot now","parameters":{"type":"object","properties":{"question":{"type":"string"}},"required":["question"]}}}
]
SAFE_SERVICES = {"light":{"turn_on","turn_off","toggle"}, "switch":{"turn_on","turn_off","toggle"}, "cover":{"open_cover","close_cover","stop_cover"}}

class Brain:
    def __init__(self, options, session, capture_camera):
        self.options = options
        self.http = session
        self.capture_camera = capture_camera
        self.hass_url = "http://supervisor/core"
        self.hass_token = os.environ.get("SUPERVISOR_TOKEN", "")
        prompt = options.get("system_prompt") or "Ты Джарвис, русскоязычный голосовой ассистент."
        self.system_message = {
            "role": "system",
            "content": prompt + " Ответы для телефона: максимум два коротких предложения, кроме случаев уточнения.",
        }
        self.history = []
        self.lock = asyncio.Lock()

    def _allowed(self, entity):
        return entity in self.options.get("allowed_entities", [])

    async def _ha(self, method, path, data=None):
        if not self.hass_token:
            raise RuntimeError("Supervisor token not available")
        async with self.http.request(method, self.hass_url+path, json=data, headers={"Authorization":"Bearer "+self.hass_token}, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _tool(self, name, arguments):
        if name == "see_camera":
            entity = self.options.get("camera_entity", "")
            device = self.options.get("q1_video_device", "")
            try:
                if entity:
                    if not entity.startswith("camera.") or not self.hass_token:
                        return "Неверная сущность камеры или нет доступа к HA"
                    async with self.http.get(self.hass_url+"/api/camera_proxy/"+entity,
                            headers={"Authorization":"Bearer "+self.hass_token},
                            timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        resp.raise_for_status()
                        picture = await resp.read()
                        mime = resp.headers.get("Content-Type","image/jpeg").split(";")[0]
                elif device:
                    picture = await self.capture_camera()
                    mime = "image/jpeg"
                else:
                    return "Камера не настроена"
                if mime not in ("image/jpeg", "image/png") or len(picture)>4*1024*1024:
                    return "Кадр не JPEG/PNG или слишком велик (более 4 МБ)"
                question = str(arguments.get("question","Что на камере?"))[:400]
                image_url = "data:"+mime+";base64,"+base64.b64encode(picture).decode()
                return {"image":image_url,"question":question}
            except Exception as exc:
                return "Ошибка камеры: "+str(exc)[:150]
        entity = arguments.get("entity_id", "")
        if not self._allowed(entity):
            return "Отказ: entity_id отсутствует в разрешённом списке"
        if name == "get_entity_state":
            return await self._ha("GET", "/api/states/"+entity)
        if name == "call_ha_service":
            domain = arguments.get("domain","")
            service = arguments.get("service","")
            if domain not in self.options.get("allow_domains",[]) or service not in SAFE_SERVICES.get(domain,set()) or entity.split(".")[0] != domain:
                return "Отказ: сервис не разрешён"
            return await self._ha("POST", "/api/services/"+domain+"/"+service, {"entity_id":entity})
        return "Unknown tool"

    async def reply(self, phrase):
        async with self.lock:
            return await self._reply(phrase)

    async def _reply(self, phrase):
        key = self.options.get("deepseek_api_key", "")
        if not key:
            return "Ключ DeepSeek не настроен."
        user_message = {"role": "user", "content": phrase}
        messages = [self.system_message, *self.history, user_message]
        try:
            for _ in range(4):
                payload={"model":self.options.get("deepseek_model","deepseek-flash"),
                         "messages":messages,"tools":TOOLS,"tool_choice":"auto",
                         "thinking":{"type":"disabled"},
                         "max_tokens":350,"temperature":0.4}
                async with self.http.post("https://api.deepseek.com/chat/completions",json=payload,
                    headers={"Authorization":"Bearer "+key},
                    timeout=aiohttp.ClientTimeout(total=35)) as response:
                    response.raise_for_status()
                    data=await response.json()
                msg=data["choices"][0]["message"]
                messages.append(msg)
                calls=msg.get("tool_calls") or []
                if not calls:
                    answer=msg.get("content") or "Не удалось сформировать ответ."
                    self.history = [
                        *self.history[-16:], user_message, {"role": "assistant", "content": answer}
                    ]
                    return answer[:1500]
                images = []
                for call in calls:
                    name=call["function"]["name"]
                    args=json.loads(call["function"].get("arguments") or "{}")
                    result=await self._tool(name,args)
                    if isinstance(result,dict) and "image" in result:
                        # Image content MUST be a new user message, not a tool result.
                        messages.append({"role":"tool","tool_call_id":call["id"],"content":"Кадр получен; смотри следующее сообщение пользователя."})
                        images.append({"role":"user","content":[{"type":"text","text":result["question"]},
                            {"type":"image_url","image_url":{"url":result["image"],"detail":"low"}}]})
                    else:
                        messages.append({"role":"tool","tool_call_id":call["id"],"content":json.dumps(result,ensure_ascii=False,default=str)[:3000]})
                messages.extend(images)
            return "Не удалось завершить команду, повторите запрос."
        except Exception:
            LOG.exception("DeepSeek request failed")
            return "Не удалось связаться с интеллектом или выполнить команду."
