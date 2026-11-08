"""LLM-facing profile-scoped tasks, saved workflows and notification preferences."""
import asyncio
from workflows import WorkflowManager, validate_steps

FEATURE_TOOLS = {'list_tasks','recent_events','resume_task','cancel_task','save_scenario','list_scenarios','run_scenario','delete_scenario','get_notification_policy','set_notification_policy','list_memories'}

class AssistantFeatures:
    def __init__(self, brain, path):
        self.brain = brain
        self.workflows = WorkflowManager(path, brain.profile)

    async def runner(self, name, args):
        if name == 'call_ha_service':
            async with self.brain.action_lock:
                return await self.brain._tool(name,args)
        result = await self.brain._tool(name,args)
        if isinstance(result,str) and result in ('Telegram подтвердил отправку фото', 'Home Assistant принял объявление для динамика'):
            return {'accepted':True,'message':result}
        return result

    async def action(self, name, args, runner):
        record = await self.workflows.execute(name,[{'tool':name,'arguments':args}],runner)
        if record['status'] == 'cancelled':
            return {'cancelled':True,'task_id':record['id'],'warning':record.get('warning','Уже отправленные команды не отменяются.')}
        if record['results']:
            result = record['results'][-1]['result']
            return {**result,'task_id':record['id']} if isinstance(result,dict) else result
        return {'error':record.get('error','Задача не завершена'),'task_id':record['id']}

    async def tool(self,name,args):
        b=self.brain
        if name=='recent_events':
            if not b.timeline:return {'events':[]}
            page=await asyncio.to_thread(b.timeline.list,b.profile,None,100,0)
            return {'events':[row for row in page['items'] if row['stage']=='event'][:20]}
        if name=='list_tasks':return {'tasks':self.workflows.list_tasks()}
        if name=='cancel_task':return self.workflows.cancel(args.get('id',''))
        if name=='resume_task':
            task=self.workflows.get(args.get('id',''))
            if not task or task.get('status')!='needs_attention':return {'error':'Можно продолжить только остановленную задачу.'}
            steps=task['steps'][task.get('current_step') or 0:]
            if args.get('confirmed') is True:
                steps[0]['arguments']['confirmed']=True
            result=await self.workflows.execute(task['name'],steps,self.runner)
            self.workflows.tasks[task['id']]['status']='resumed'
            return result
        if name=='list_scenarios':return {'scenarios':await asyncio.to_thread(self.workflows.list_scenarios)}
        if name=='delete_scenario':return {'deleted':await asyncio.to_thread(self.workflows.delete,args.get('name',''))}
        if name=='save_scenario':
            steps=validate_steps(args.get('steps'))
            for step in steps:
                entity=step.get('arguments',{}).get('entity_id')
                if entity and (not isinstance(entity,str) or not b._visible(entity)):return {'error':'Устройство недоступно этому профилю.'}
                if step['tool']=='call_ha_service' and step.get('arguments',{}).get('service') not in b._control_actions(entity):
                    return {'error':'Действие недоступно этому профилю.'}
                camera=step.get('arguments',{}).get('camera')
                if camera and (not isinstance(camera,str) or not b.camera_allowed(camera)):
                    return {'error':'Камера недоступна этому профилю.'}
            await asyncio.to_thread(self.workflows.save,args.get('name',''),steps)
            return {'saved':True,'name':args['name']}
        if name=='run_scenario':
            steps=await asyncio.to_thread(self.workflows.scenario,args.get('name',''))
            return await self.workflows.execute(args['name'],steps,self.runner)
        if name=='list_memories':
            notes=await asyncio.to_thread(b.personal.notes,b.profile) if b.personal else []
            metadata=await asyncio.to_thread(b.knowledge.memories,b.profile) if b.knowledge else []
            return {'notes':notes,'metadata':[row for row in metadata if row['text'] in notes]}
        if name=='get_notification_policy':
            return await asyncio.to_thread(b.policies.get,b.profile) if b.policies else {'error':'Настройки уведомлений недоступны.'}
        if name=='set_notification_policy':
            if not b.policies or b.profile in {'room','local'}:return {'error':'Настройте личный телефонный профиль.'}
            policy={key:value for key,value in args.items() if key in {'channel','quiet_start','quiet_end','timezone','urgent_bypass','telegram_target'}}
            return await asyncio.to_thread(b.policies.set,b.profile,policy)
        return {'error':'Unknown feature tool'}
