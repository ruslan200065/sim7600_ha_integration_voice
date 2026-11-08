/* Profile-scoped operations panel. No execution commands are sent by this view. */
(() => {
  const host = document.querySelector("#operations-content");
  if (!host) return;
  let selected = "", generation = 0, page = 0, searchText = "", catalog = [];
  const taskStates = {pending:"Ожидает",running:"Выполняется",completed:"Завершено",cancelled:"Отменено",needs_attention:"Нужна проверка",failed:"Ошибка"};
  const traceStages = {stt:"Распознавание",llm:"Модель",tool:"Инструмент",playback:"Озвучивание",call:"Звонок",reply:"Ответ",audio:"Аудиоканал",speech:"Речь",notification:"Уведомление",event:"Событие"};
  const statusNames = {started:"Начато",recognized:"Распознано",answered:"Ответ получен",accepted:"Команда принята",verified:"Подтверждено",failed:"Ошибка",completed:"Завершено",cancelled:"Отменено",ready:"Готово",ok:"Успешно",sent:"Отправлено",received:"Получено",error:"Ошибка"};
  const el = (tag, text, className) => {const node=document.createElement(tag); if(text !== undefined) node.textContent=text; if(className)node.className=className; return node;};
  function button(text, fn) {const node=el("button",text);node.type="button";node.addEventListener("click",fn);return node;}
  async function api(method="GET", body) {
    const response=await fetch("operations?profile="+encodeURIComponent(selected)+"&offset="+page,{method,cache:"no-store",headers:{"Content-Type":"application/json","X-Jarvis-Setup":"1"},body:body===undefined?undefined:JSON.stringify(body)});
    if(!response.ok)throw new Error((await response.text()).slice(0,250)||"Не удалось загрузить задачи.");
    return response.json();
  }
  function section(title){const card=el("section",undefined,"card");card.append(el("h3",title));host.append(card);return card;}
  function field(label,input){const node=el("label",label);node.append(input);return node;}
  function input(type,value){const node=el("input");node.type=type;node.value=value||"";return node;}
  function summary(details){
    if(!details||typeof details!=="object")return "";
    return Object.entries(details).map(([key,value])=>{
      if(value===null||value===undefined)return "";
      if(typeof value==="object")return summary(value);
      return String(value);
    }).filter(Boolean).join(" · ").slice(0,900);
  }
  function rows(parent,items,render){
    const matches=items.filter(row=>!searchText||JSON.stringify(row).toLocaleLowerCase("ru").includes(searchText));
    if(!matches.length)parent.append(el("p","Здесь пока пусто или ничего не найдено.","muted"));
    matches.forEach(row=>parent.append(render(row)));
  }
  async function mutate(body,method="POST"){
    try{await api(method,body);await load();}catch(error){showError(error.message);}
  }
  function showError(message){const node=host.querySelector("[data-operations-status]");if(node){node.textContent=message;node.dataset.error="true";}}
  function render(data){
    selected=data.profile;host.replaceChildren();
    const heading=el("div",undefined,"section-heading");heading.append(el("h2","Задачи и уведомления"));heading.append(button("Обновить",load));host.append(heading);
    const status=el("p","","hint");status.dataset.operationsStatus="";host.append(status);
    const profile=el("select");profile.setAttribute("aria-label","Участник семьи");profile.replaceChildren(...data.profiles.map(row=>new Option(row.name,row.id)));profile.value=data.profile;
    profile.addEventListener("change",()=>{selected=profile.value;page=0;load();});host.append(field("Участник семьи",profile));
    const query=input("search",searchText);query.placeholder="Поиск по задачам, сценариям и журналу";query.setAttribute("aria-label","Поиск операций");query.addEventListener("input",()=>{searchText=query.value.toLocaleLowerCase("ru");renderLists(data);});host.append(query);
    const policies=section("Как связываться с этим участником");const form=el("form");const fields=el("div",undefined,"fields");
    const channel=el("select");channel.replaceChildren(new Option("Телефонный звонок","call"),new Option("Telegram","telegram"));channel.value=data.policy.channel;
    const target=input("text",data.policy.telegram_target);target.placeholder="chat_id или @username";
    const start=input("time",data.policy.quiet_start),end=input("time",data.policy.quiet_end),zone=input("text",data.policy.timezone);
    const urgent=input("checkbox");urgent.checked=data.policy.urgent_bypass;
    fields.append(field("Обычный канал",channel),field("Получатель Telegram",target),field("Не беспокоить с",start),field("Не беспокоить до",end),field("Часовой пояс",zone),field("Срочные уведомления обходят часы тишины",urgent));
    const save=button("Сохранить правила связи",async()=>{
      save.disabled=true;
      try{await api("PATCH",{policy:{channel:channel.value,telegram_target:target.value.trim(),quiet_start:start.value,quiet_end:end.value,timezone:zone.value.trim(),urgent_bypass:urgent.checked}});status.textContent="Правила связи сохранены.";status.dataset.error="false";}catch(error){showError(error.message);}finally{save.disabled=false;}
    });save.className="primary";form.append(fields,el("p","Без часов тишины уведомления доставляются сразу. Срочные события обходят тишину только при включённой настройке.","hint"),save);form.addEventListener("submit",event=>event.preventDefault());policies.append(form);
    const lists=el("div");lists.dataset.operationsLists="";host.append(lists);renderLists(data);
  }
  function scenarioEditor(row) {
    const card=el("article"),title=el("h4",row.name),steps=el("div"),draft=JSON.parse(JSON.stringify(row.steps));
    const tools={call_ha_service:"Управлять устройством",get_entity_state:"Прочитать состояние",announce:"Проговорить сообщение",send_camera_photo:"Отправить фото",see_camera:"Посмотреть камеру"};
    const services={turn_on:"Включить",turn_off:"Выключить",toggle:"Переключить",open_cover:"Открыть",close_cover:"Закрыть",stop_cover:"Остановить привод",set_cover_position:"Положение привода",media_play:"Воспроизвести",media_pause:"Пауза",media_stop:"Остановить воспроизведение",volume_set:"Громкость",set_temperature:"Температура",set_value:"Задать значение",select_option:"Выбрать вариант",set_percentage:"Скорость",start:"Запустить",pause:"Приостановить",stop:"Остановить",return_to_base:"Вернуть на базу",cancel:"Отменить"};
    const parameters={volume_level:"Громкость (0–1)",temperature:"Температура",value:"Числовое значение",option:"Вариант",percentage:"Процент",position:"Положение (0–100)",brightness_pct:"Яркость (0–100)"};
    function selectEntity(current,cameraOnly=false){
      const select=el("select");select.append(new Option(cameraOnly?"Камера по умолчанию":"Выберите устройство",""));
      const available=catalog.filter(item=>!cameraOnly||item.id?.startsWith("camera."));
      for(const item of available)select.append(new Option([item.name,item.area,item.id].filter(Boolean).join(" · "),item.id));
      if(current&&!available.some(item=>item.id===current))select.append(new Option(current,current));select.value=current||"";return select;
    }
    function redraw(){
      steps.replaceChildren();draft.forEach((step,index)=>{
        const section=el("section",undefined,"card"),fields=el("div",undefined,"fields"),args=step.arguments||(step.arguments={});
        const tool=el("select");Object.entries(tools).forEach(([value,label])=>tool.append(new Option(label,value)));tool.value=step.tool;
        tool.addEventListener("change",()=>{step.tool=tool.value;step.arguments={};redraw();});fields.append(field("Шаг "+(index+1),tool));
        const camera=["send_camera_photo","see_camera"].includes(step.tool),entity=selectEntity(args[camera?"camera":"entity_id"],camera);
        entity.addEventListener("change",()=>{const key=camera?"camera":"entity_id";if(entity.value)args[key]=entity.value;else delete args[key];delete args.domain;});fields.append(field(camera?"Камера":"Устройство",entity));
        if(step.tool==="call_ha_service"){
          const service=el("select");service.append(new Option("Выберите действие",""));Object.entries(services).forEach(([value,label])=>service.append(new Option(label,value)));
          if(args.service&&!(args.service in services))service.append(new Option(args.service,args.service));service.value=args.service||"";service.addEventListener("change",()=>args.service=service.value);fields.append(field("Действие",service));
          for(const [key,label] of Object.entries(parameters)){
            const control=input(key==="option"?"text":"number",args[key]===undefined?"":String(args[key]));if(key!=="option")control.step="any";
            control.addEventListener("input",()=>{if(control.value==="")delete args[key];else args[key]=key==="option"?control.value:Number(control.value);});fields.append(field(label,control));
          }
        }
        if(step.tool==="announce"||step.tool==="see_camera"){
          const key=step.tool==="announce"?"text":"question",text=el("textarea");text.rows=2;text.maxLength=2000;text.value=args[key]||"";text.addEventListener("input",()=>args[key]=text.value);fields.append(field(key==="text"?"Сообщение":"Что узнать по камере",text));
        }
        const actions=el("div",undefined,"actions"),up=button("↑ Выше",()=>{[draft[index-1],draft[index]]=[draft[index],draft[index-1]];redraw();}),down=button("↓ Ниже",()=>{[draft[index+1],draft[index]]=[draft[index],draft[index+1]];redraw();});up.disabled=index===0;down.disabled=index===draft.length-1;
        actions.append(up,down,button("Убрать шаг",()=>{draft.splice(index,1);redraw();}));section.append(fields,actions);steps.append(section);
      });
    }
    const editor=el("details");editor.append(el("summary","Изменить шаги сценария"));redraw();
    const save=button("Сохранить сценарий",async()=>{save.disabled=true;try{await mutate({action:"save_scenario",name:row.name,steps:draft});}finally{save.disabled=false;}});
    editor.append(steps,button("Добавить шаг",()=>{if(draft.length<20){draft.push({tool:"get_entity_state",arguments:{}});redraw();}}),save,el("p","Сохранение меняет только план. Действия не запускаются. Разрешения проверяются при сохранении и выполнении.","hint"));
    card.append(title,el("p","Шагов: "+row.steps.length,"hint"),editor,button("Удалить сценарий",()=>{if(window.confirm("Удалить сценарий «"+row.name+"»?"))mutate({action:"delete_scenario",name:row.name});}));return card;
  }
  function renderLists(data){
    const hostLists=host.querySelector("[data-operations-lists]");if(!hostLists)return;hostLists.replaceChildren();
    const tasks=el("section",undefined,"card");tasks.append(el("h3","Задачи"));
    rows(tasks,data.tasks||[],row=>{
      const card=el("article");card.append(el("h4",row.name),el("p",taskStates[row.status]||row.status,"hint"));
      if(row.current_step!==null&&row.current_step!==undefined)card.append(el("p","Шаг "+(row.current_step+1)+" из "+row.steps.length,"hint"));
      if(row.warning)card.append(el("p",row.warning,"hint"));
      if(["pending","running"].includes(row.status))card.append(button("Отменить оставшиеся действия",()=>mutate({action:"cancel_task",id:row.id})));
      return card;
    });hostLists.append(tasks);
    const scenarios=el("section",undefined,"card");scenarios.append(el("h3","Сохранённые сценарии"),el("p","Создать, изменить и запустить сценарий можно обычной просьбой Джарвису.","hint"));
    rows(scenarios,data.scenarios||[],scenarioEditor);hostLists.append(scenarios);
    const timeline=el("section",undefined,"card");timeline.append(el("h3","Ход разговора и выполнения"),el("p","Показаны только записи выбранного участника. Пароли и код DTMF в журнал не попадают.","hint"));
    rows(timeline,data.timeline?.items||[],row=>{
      const card=el("article");card.append(el("p",[new Date(row.at).toLocaleString("ru-RU"),traceStages[row.stage]||row.stage,statusNames[row.status]||row.status,row.duration_ms!==null?Math.round(row.duration_ms)+" мс":""].filter(Boolean).join(" · "),"hint"));
      if(row.call_id!==null)card.append(el("small","Разговор: "+row.call_id));card.append(el("p",summary(row.details)));return card;
    });
    const navigation=el("div",undefined,"actions");const back=button("Предыдущие",()=>{page=Math.max(0,page-100);load();});back.disabled=page===0;
    const next=button("Следующие",()=>{page=data.timeline.next_offset;load();});next.disabled=data.timeline?.next_offset===null||data.timeline?.next_offset===undefined;
    navigation.append(back,el("span", "Записей: "+(data.timeline?.total||0)),next);timeline.append(navigation);hostLists.append(timeline);
  }
  async function load(){
    const ticket=++generation;
    try{const [data,entities]=await Promise.all([api(),fetch("entities",{cache:"no-store"}).then(response=>response.ok?response.json():{}).catch(()=>({}))]);if(ticket===generation){catalog=entities.entities||[];render(data);}}catch(error){if(ticket!==generation)return;if(!host.children.length)host.append(el("p","","hint"));let node=host.querySelector("[data-operations-status]");if(!node){node=el("p");node.dataset.operationsStatus="";host.prepend(node);}showError(error.message);}
  }
  function maybeLoad(){if(location.hash==="#operations")load();}
  window.addEventListener("hashchange",maybeLoad);maybeLoad();
})();
