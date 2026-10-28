"use strict";
// Los borradores sobreviven a filtros y cambios de vista; nunca se guardan solos.
const tableDrafts = new Map();
const tableMessages = new Map();
const tableFields = ["priority", "status", "assignee", "dead_time", "planned_start", "estimated_days", "end_mode", "planned_end", "actual_start", "actual_end"];
const tableKey = task => String(task.id ?? `${task.category}:${task.row}`);

function tablePatch(original, values) {
  const patch = {};
  for(const field of tableFields) {
    if(String(values[field] ?? "") !== String(original[field] ?? "")) patch[field] = values[field];
  }
  // El servidor calcula la entrega en modo automático, no el navegador.
  if(values.end_mode === "auto") delete patch.planned_end;
  return patch;
}

function tableDraftCount() {
  document.querySelector("#table-drafts").textContent = tableDrafts.size ? `${tableDrafts.size} fila(s) con cambios sin guardar, incluidos los ocultos por los filtros.` : "Ordenadas por prioridad numérica, de mayor a menor.";
}

function renderTaskTable(tasks) {
  const container = document.querySelector("#task-table");
  document.querySelector("#table-count").textContent = state.loaded ? tasks.length : "—";
  tableDraftCount();
  if(!state.loaded) {container.innerHTML = empty("No pudimos cargar las tareas", "Revisa el aviso de conexión y pulsa Actualizar.");return;}
  if(!tasks.length) {container.innerHTML = empty("No hay tareas para mostrar", "Cambia los filtros o registra una nueva tarea.");return;}
  const input = (field, label, value, code, extra="") => `<input class="form-control" name="${field}" aria-label="${escapeHtml(label+' '+code)}" value="${escapeHtml(value)}" ${extra}>`;
  const select = (field, label, value, options, code) => `<select class="form-select" name="${field}" aria-label="${escapeHtml(label+' '+code)}">${options.map(option=>`<option value="${escapeHtml(option[0])}" ${String(option[0])===String(value)?'selected':''}>${escapeHtml(option[1])}</option>`).join("")}</select>`;
  const labels = ["Código", "Tarea / categoría", "Módulo", "Prioridad", "Estado", "Responsable", "Tiempo muerto", "Inicio previsto", "Días hábiles", "Entrega", "Fin previsto", "Inicio real", "Fin real", "Acciones"];
  const scroll = container.scrollLeft;
  container.innerHTML = `<table class="task-table"><caption class="visually-hidden">Tareas con edición de prioridad, estado, responsable, duración y fechas. Guardado por fila.</caption><thead><tr>${labels.map(label=>`<th scope="col">${label}</th>`).join("")}</tr></thead><tbody>${tasks.map(task=>{
    const key=tableKey(task), draft=tableDrafts.get(key), values=draft?.values || task;
    const code=task.code || `Tarea ${task.row}`, index=state.tasks.indexOf(task);
    const options = (configured,value) => [...new Set([...configured,value])].map(v=>[v,v || "Sin registrar"]);
    const message=tableMessages.get(key);
    return `<tr data-table-key="${escapeHtml(key)}" class="${draft?'is-dirty':''} ${values.dead_time?'is-dead-time':''}">
      <td><span class="task-code">${escapeHtml(code)}</span></td>
      <td class="table-name"><button type="button" data-task-index="${index}" title="Abrir edición completa">${escapeHtml(task.name)}</button><small>${escapeHtml(task.category_name || task.category)}</small></td>
      <td>${escapeHtml(task.module)}</td>
      <td>${input('priority','Prioridad',values.priority,code,'type="text" inputmode="decimal" maxlength="30"')}</td>
      <td>${select('status','Estado',values.status,options(state.config.statuses,values.status),code)}</td>
      <td>${select('assignee','Responsable',values.assignee,options(state.config.people,values.assignee),code)}</td>
      <td class="dead-time-cell"><input name="dead_time" type="checkbox" aria-label="Tiempo muerto ${escapeHtml(code)}" ${values.dead_time?'checked':''}></td>
      <td>${input('planned_start','Inicio previsto',values.planned_start,code,'type="date"')}</td>
      <td>${input('estimated_days','Días hábiles',values.estimated_days,code,'type="number" min="1" max="3660" step="1"')}</td>
      <td>${select('end_mode','Modo de entrega',values.end_mode,[['auto','Automática'],['manual','Manual']],code)}</td>
      <td>${input('planned_end','Fin previsto',values.planned_end,code,`type="date" ${values.end_mode==='auto'?'readonly':''}`)}</td>
      <td>${input('actual_start','Inicio real',values.actual_start,code,'type="date"')}</td>
      <td>${input('actual_end','Fin real',values.actual_end,code,'type="date"')}</td>
      <td class="table-actions"><button type="button" class="btn btn-primary" data-table-action="save" aria-label="Guardar ${escapeHtml(code)}" data-locked="${!draft}" ${!draft?'disabled':''}>Guardar</button><button type="button" class="btn btn-light" data-table-action="discard" aria-label="Descartar ${escapeHtml(code)}" data-locked="${!draft}" ${!draft?'disabled':''}>Descartar</button><p class="table-row-message ${message?.error?'error':''}" role="status">${escapeHtml(message?.text || (draft?'Cambios sin guardar':''))}</p></td>
    </tr>`;
  }).join("")}</tbody></table>`;
  container.scrollLeft=scroll;
  if(state.busy) container.querySelectorAll("input,select,button").forEach(el=>el.disabled=true);
}

document.querySelector("#task-table").addEventListener("input",event=>{
  const row=event.target.closest("[data-table-key]");
  if(!row || !event.target.name || state.busy) return;
  const key=row.dataset.tableKey, task=state.tasks.find(task=>tableKey(task)===key);
  if(!task) return;
  const original=tableDrafts.get(key)?.original || {...task};
  const values=Object.fromEntries([...row.querySelectorAll("input,select")].map(el=>[el.name,el.type==='checkbox'?el.checked:el.value]));
  const dirty=Object.keys(tablePatch(original,values)).length>0;
  if(dirty) tableDrafts.set(key,{original,values}); else tableDrafts.delete(key);
  tableMessages.delete(key);
  row.classList.toggle("is-dirty",dirty);
  row.querySelector('[name="planned_end"]').readOnly=values.end_mode==='auto';
  row.querySelectorAll("[data-table-action]").forEach(button=>{button.disabled=!dirty;button.dataset.locked=String(!dirty);});
  const message=row.querySelector(".table-row-message");
  message.classList.remove("error");message.textContent=dirty?"Cambios sin guardar":"";
  tableDraftCount();
});

document.querySelector("#task-table").addEventListener("click",async event=>{
  const button=event.target.closest("[data-table-action]");
  if(!button || state.busy) return;
  const row=button.closest("[data-table-key]"), key=row.dataset.tableKey, draft=tableDrafts.get(key);
  if(button.dataset.tableAction==='discard') {tableDrafts.delete(key);tableMessages.delete(key);renderTaskTable(filteredTasks());return;}
  if(!draft) return;
  const changes=tablePatch(draft.original,draft.values);
  for(const input of row.querySelectorAll("input")) {
    if(input.name in changes && !input.reportValidity()) return;
  }
  setBusy(true);
  row.querySelector(".table-row-message").textContent="Guardando…";
  let saved=false;
  try {
    const task=draft.original;
    const result=await api(`/api/tasks/${encodeURIComponent(task.category)}/${task.row}`,{method:"PUT",headers:{"Content-Type":"application/json","X-CSRF-Token":state.csrf},body:JSON.stringify({...changes,revision:task.revision})});
    const index=state.tasks.findIndex(item=>tableKey(item)===key);
    if(index>=0) state.tasks[index]={...state.tasks[index],...result.task};
    tableDrafts.delete(key);
    tableMessages.set(key,{text:"Guardado",error:false});
    saved=true;
    notice(`Tarea ${result.task.code} actualizada.`,"success");
  } catch(error) {
    tableMessages.set(key,{text:error.message,error:true});
  } finally {
    setBusy(false);
    if(saved) {updateFilterOptions();render();} else renderTaskTable(filteredTasks());
  }
});
document.querySelector("#table-refresh").addEventListener("click",()=>refresh());
window.addEventListener("beforeunload",event=>{if(tableDrafts.size){event.preventDefault();event.returnValue="";}});
