"""Ingress-only controls for learning, boundaries, previews and private family messages."""
import asyncio
from datetime import datetime
from aiohttp import web
from call_diagnostics import analyze_timeline
from rules import validate_condition, validate_action


def _selected(panel, request):
    profiles = panel.personal_profiles()
    profile = request.query.get('profile') or next(iter(profiles))
    if profile not in profiles:
        raise web.HTTPNotFound(text='Профиль не найден.')
    member = next((row for row in panel.service.family.values() if row['key'] == profile), None)
    brain = panel.service.caller_brain(member) if member else panel.service.brain
    return profiles, profile, brain


def _check_simulation(brain, condition, action):
    condition, action = validate_condition(condition), validate_action(action)
    for key in ('entity', 'zone'):
        entity = condition.get(key)
        if entity and (not isinstance(entity, str) or not brain._visible(entity)):
            raise ValueError('Устройство условия недоступно этому профилю.')
    args = action.get('arguments', {})
    if not isinstance(args, dict):
        raise ValueError('Неверные параметры действия.')
    entity = args.get('entity_id')
    if entity and (not isinstance(entity, str) or not brain._visible(entity)):
        raise ValueError('Устройство действия недоступно этому профилю.')
    if action.get('tool') == 'call_ha_service' and args.get('service') not in brain._control_actions(entity):
        raise ValueError('Действие недоступно этому профилю.')
    if action.get('tool') == 'announce' and not brain._control_actions(entity):
        raise ValueError('Объявление недоступно этому профилю.')
    camera = args.get('camera')
    if camera and (not isinstance(camera, str) or not brain.camera_allowed(camera)):
        raise ValueError('Камера недоступна этому профилю.')


async def intelligence(panel, request):
    profiles, profile, brain = _selected(panel, request)
    service = panel.service
    for attribute in ('autonomy', 'test_rules', 'inbox', 'events_journal', 'escalation', 'timeline'):
        if getattr(service, attribute, None) is None:
            raise web.HTTPServiceUnavailable(text='Новые службы ещё не готовы.')
    try:
        if request.method in {'POST', 'PATCH'}:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError('Ожидался объект настроек.')
            action = data.get('action', 'set_autonomy' if request.method == 'PATCH' else None)
            if action == 'set_autonomy':
                target = data.get('target')
                if isinstance(target, str) and '.' in target and not brain._visible(target):
                    raise ValueError('Устройство недоступно этому профилю.')
                result = await asyncio.to_thread(service.autonomy.set_rule, profile, target, data.get('mode'), data.get('ranges'))
            elif action == 'remove_rule':
                result = {'deleted': await asyncio.to_thread(service.autonomy.remove_rule, profile, data.get('target'))}
            elif action == 'delete_correction':
                result = {'deleted': await asyncio.to_thread(service.autonomy.forget, profile, data.get('phrase'))}
            elif action == 'save_simulation':
                recipient = data.get('recipient') or profile
                if recipient not in profiles:
                    raise ValueError('Выберите существующего получателя.')
                if profile != service.owner_key and recipient != profile:
                    raise ValueError('Личная проверка не меняет получателя на другого участника.')
                _check_simulation(brain, data.get('condition'), data.get('operation'))
                result = await asyncio.to_thread(service.test_rules.save, profile, data.get('name'), data.get('condition'), data.get('operation'), recipient)
            elif action == 'preview_simulation':
                rule = next((item for item in await asyncio.to_thread(service.test_rules.list, profile) if item['name'] == data.get('name')), None)
                if rule is None:
                    raise ValueError('Пробное правило не найдено.')
                _check_simulation(brain, rule['condition'], rule['action'])
                states = await brain._ha('GET', '/api/states')
                states = {row['entity_id']: row for row in states if brain._visible(row.get('entity_id'))}
                result = {'preview': await asyncio.to_thread(service.test_rules.preview, profile, data['name'], states)}
            elif action == 'delete_simulation':
                result = {'deleted': await asyncio.to_thread(service.test_rules.delete, profile, data.get('name'))}
            elif action == 'send_message':
                recipient = data.get('recipient')
                if recipient not in profiles:
                    raise ValueError('Выберите существующего получателя.')
                expires = data.get('expires_at')
                expires = datetime.fromisoformat(expires) if isinstance(expires, str) and expires else None
                result = await asyncio.to_thread(service.inbox.send, profile, recipient, data.get('text'), expires)
            elif action == 'cancel_message':
                result = {'cancelled': await asyncio.to_thread(service.inbox.cancel, profile, data.get('id'))}
            elif action == 'set_escalation':
                if profile != service.owner_key:
                    raise web.HTTPForbidden(text='Цепочку получателей настраивает владелец.')
                chain = data.get('chain')
                if not isinstance(chain, list) or any(not isinstance(item, str) or item not in profiles for item in chain):
                    raise ValueError('Выберите существующих участников семьи.')
                result = await asyncio.to_thread(service.escalation.set, data.get('source'), chain, data.get('max_attempts', 3))
            else:
                raise ValueError('Неизвестное действие.')
            return web.json_response(result)
        since = request.query.get('since')
        since = datetime.fromisoformat(since) if since else None
        offset = int(request.query.get('offset', 0))
        rules, corrections, simulations, pending, sent, changes, trace = await asyncio.gather(
            asyncio.to_thread(service.autonomy.rules, profile),
            asyncio.to_thread(service.autonomy.corrections, profile),
            asyncio.to_thread(service.test_rules.list, profile),
            asyncio.to_thread(service.inbox.pending, profile) if profile not in {'room','local'} else asyncio.sleep(0, result=[]),
            asyncio.to_thread(service.inbox.sent, profile) if profile not in {'room','local'} else asyncio.sleep(0, result=[]),
            asyncio.to_thread(service.events_journal.list, profile, brain._visible, since, 50, offset),
            asyncio.to_thread(service.timeline.list, profile, limit=500),
        )
        telephony_stages = {'connection', 'audio_rx', 'recognized', 'reply', 'tts', 'playback', 'stt', 'llm'}
        calls = []
        seen = set()
        for row in trace['items']:
            identifier = str(row.get('call_id'))
            if (row.get('stage') in telephony_stages and row.get('call_id') is not None
                    and identifier not in {'room', 'local'} and not identifier.startswith('event:') and identifier not in seen):
                calls.append({'id': identifier, 'at': row.get('at')})
                seen.add(identifier)
        call_id = request.query.get('call_id') or (calls[0]['id'] if calls else None)
        if request.query.get('call_id'):
            trace = await asyncio.to_thread(service.timeline.list, profile, call_id=call_id, limit=500)
        diagnostics = analyze_timeline(trace, call_id) if call_id is not None else analyze_timeline([], None)
        escalation = await asyncio.to_thread(service.escalation.policies) if profile == service.owner_key else []
        return web.json_response({'profiles':[{'id':key,'name':name} for key,name in profiles.items()], 'profile':profile,
            'is_owner':profile == service.owner_key, 'rules':rules, 'corrections':corrections, 'simulations':simulations,
            'inbox':pending, 'sent':sent, 'home_changes':changes, 'diagnostics':diagnostics, 'diagnostics_call_id':call_id, 'calls':calls, 'escalation':escalation})
    except (ValueError, TypeError, OverflowError, KeyError) as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc


def register_intelligence(app, panel):
    async def handler(request):
        return await intelligence(panel, request)
    app.router.add_get('/intelligence', handler)
    app.router.add_post('/intelligence', handler)
    app.router.add_patch('/intelligence', handler)
