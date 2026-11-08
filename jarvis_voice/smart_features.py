"""Context-selected personal learning, policy, simulations and family communication."""
import asyncio
from datetime import datetime, timedelta
from autonomy import phrase_key
from rules import validate_action, validate_condition

SMART_TOOLS = {'learn_device_correction','forget_device_correction','get_autonomy_rules','set_autonomy_rule','remove_autonomy_rule','save_test_rule','list_test_rules','delete_test_rule','preview_test_rule','home_changes','message_recipients','send_family_message','sent_family_messages','cancel_family_message'}


def _normalize_plan(condition, action):
    if not isinstance(condition, dict) or not isinstance(action, dict):
        raise ValueError('Укажи условие и действие.')
    condition, action = dict(condition), dict(action)
    # Accept the older tool description while storing one canonical format.
    if condition.get('kind') == 'time' and 'due' in condition and 'when' not in condition:
        condition['when'] = condition.pop('due')
    if 'tool' not in action:
        action = {'tool':'call_ha_service','arguments':action}
    return validate_condition(condition), validate_action(action)


def _plan_allowed(b, condition, action):
    for field in ('entity','zone'):
        if condition.get(field) and not b._visible(condition[field]):
            return False
    args = action.get('arguments', {})
    tool = action['tool']
    entity = args.get('entity_id')
    if tool == 'call_ha_service':
        return isinstance(entity,str) and b._visible(entity) and args.get('service') in b._control_actions(entity)
    if tool == 'announce':
        return isinstance(entity,str) and entity.startswith('media_player.') and b._visible(entity) and bool(b._control_actions(entity))
    camera = args.get('camera')
    if camera and (not isinstance(camera,str) or not b.camera_allowed(camera)):
        return False
    if tool == 'notify' and any(key in args for key in ('recipient','member','target','number','profile')):
        return False
    return True


async def _preview(b, name):
    rule = next((row for row in await asyncio.to_thread(b.test_rules.list,b.profile) if row['name']==name),None)
    if not rule or rule['recipient'] != b.profile or not _plan_allowed(b,rule['condition'],rule['action']):
        return {'error':'Пробное правило или его источники недоступны этому профилю.'}
    rows = await b._ha('GET','/api/states')
    states = {row['entity_id']:row for row in rows if b._visible(row.get('entity_id'))}
    result = await asyncio.to_thread(b.test_rules.preview,b.profile,name,states)
    if rule['action']['tool']=='call_ha_service':
        args = rule['action']['arguments']
        policy = await asyncio.to_thread(b.autonomy.evaluate,b.profile,args['entity_id'],args['service'],args)
        result['autonomy'] = policy
        result['would_execute'] = result['would_trigger'] and policy['decision'] not in {'deny','confirm'}
        result['needs_confirmation'] = policy['decision']=='confirm'
    else:
        result['would_execute'] = result['would_trigger']
    return result


async def dispatch(b, name, args):
    if name=='learn_device_correction':
        phrase,entity = args.get('phrase'),args.get('entity_id')
        if not isinstance(phrase,str) or not b._visible(entity):
            return {'error':'Выбери доступное устройство и понятную фразу.'}
        phrase = phrase_key(phrase)
        ask = b._needs_confirmation(('correction',phrase,entity),args,'Запомнить этот выбор устройства для вас?',required=True)
        if ask:
            return ask
        return await asyncio.to_thread(b.autonomy.learn,b.profile,phrase,entity,True)
    if name=='forget_device_correction':
        return {'deleted':await asyncio.to_thread(b.autonomy.forget,b.profile,args.get('phrase',''))}
    if name=='get_autonomy_rules':
        rules,corrections = await asyncio.gather(asyncio.to_thread(b.autonomy.rules,b.profile),asyncio.to_thread(b.autonomy.corrections,b.profile))
        return {'rules':[row for row in rules if '.' not in row['target'] or b._visible(row['target'])],
                'corrections':[row for row in corrections if b._visible(row['entity'])]}
    if name=='set_autonomy_rule':
        target = args.get('target','')
        if not isinstance(target,str) or ('.' in target and not b._visible(target)):
            return {'error':'Устройство недоступно.'}
        ranges = {}
        if args.get('parameter'):
            bounds = {key:args[source] for key,source in (('min','minimum'),('max','maximum')) if source in args}
            ranges = {args['parameter']:bounds}
        return await asyncio.to_thread(b.autonomy.set_rule,b.profile,target,args.get('mode'),ranges)
    if name=='remove_autonomy_rule':
        return {'deleted':await asyncio.to_thread(b.autonomy.remove_rule,b.profile,args.get('target',''))}
    if name in {'save_test_rule','preview_test_rule'}:
        if name=='save_test_rule':
            if args.get('recipient',b.profile)!=b.profile:
                return {'error':'Пробное личное правило принадлежит только этому профилю.'}
            condition,action = _normalize_plan(args.get('condition',{}),args.get('action',{}))
            if not _plan_allowed(b,condition,action):
                return {'error':'Условие или действие недоступно этому профилю.'}
            await asyncio.to_thread(b.test_rules.save,b.profile,args.get('name'),condition,action,b.profile)
        return await _preview(b,args.get('name'))
    if name=='list_test_rules':
        rows = await asyncio.to_thread(b.test_rules.list,b.profile)
        return {'rules':[row for row in rows if row['recipient']==b.profile and _plan_allowed(b,row['condition'],row['action'])]}
    if name=='delete_test_rule':
        return {'deleted':await asyncio.to_thread(b.test_rules.delete,b.profile,args.get('name',''))}
    if name=='home_changes':
        since = None
        if args.get('since'):
            since = datetime.fromisoformat(args['since'])
            if since.tzinfo is None:
                since = since.replace(tzinfo=b.timezone)
        return await asyncio.to_thread(b.events.list,b.profile,b._visible,since,args.get('limit',50),args.get('offset',0))
    if name=='message_recipients':
        return {'recipients':[{'id':key,'name':value} for key,value in b.contacts.items()]}
    if name=='send_family_message':
        recipient = args.get('recipient','')
        key = next((key for key,value in b.contacts.items() if key==recipient or value.casefold()==str(recipient).strip().casefold()),None)
        if key is None or b.profile not in b.contacts:
            return {'error':'Выбери участника семьи из message_recipients.'}
        expiry = datetime.fromisoformat(args['expires_at']) if args.get('expires_at') else datetime.now(b.timezone)+timedelta(days=7)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=b.timezone)
        return await asyncio.to_thread(b.inbox.send,b.profile,key,args.get('text'),expiry)
    if name=='sent_family_messages':
        return {'messages':await asyncio.to_thread(b.inbox.sent,b.profile)}
    if name=='cancel_family_message':
        return {'cancelled':await asyncio.to_thread(b.inbox.cancel,b.profile,args.get('id',''))}
    return {'error':'Unknown smart tool'}
