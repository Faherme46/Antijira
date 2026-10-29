"use strict";
const $ = (selector) => document.querySelector(selector);
const state = { config: null, tasks: [], csrf: "", loaded: false, busy: false, view: "board", editing: null, baseline: null };
const palette = ["#9aa889", "#d5a063", "#a297bf", "#6a9c88", "#729caa", "#bd8d94"];
const DAY = 86400000;
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
const localToday = () => new Intl.DateTimeFormat("en-CA", { timeZone: "America/Bogota", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
const dayNumber = value => value ? Date.parse(`${value}T00:00:00Z`) / DAY : NaN;
const isoDay = day => new Date(day * DAY).toISOString().slice(0, 10);
const formatDate = (value, long = false) => value ? new Date(`${value}T00:00:00Z`).toLocaleDateString("es-CO", { timeZone: "UTC", day: "numeric", month: long ? "long" : "short", ...(long ? { year: "numeric" } : {}) }) : "Sin registrar";
const initials = value => String(value).trim().split(/\s+/).slice(0, 2).map(v => v[0] || "").join("").toUpperCase();
const isDone = task => state.config.completed_statuses.includes(task.status);
const isOverdue = task => task.planned_end && task.planned_end < localToday() && !isDone(task) && !task.actual_end;
const isUpcoming = task => !isDone(task) && !task.actual_start && dayNumber(task.planned_start) >= dayNumber(localToday()) && dayNumber(task.planned_start) <= dayNumber(localToday()) + state.config.upcoming_days;
const statusColor = status => palette[Math.max(0, state.config.statuses.indexOf(status)) % palette.length];

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { cache: "no-store", ...options });
  }
  catch {
    throw new Error(options.method ? "Se perdió la conexión. El cambio podría haberse guardado: actualiza el tablero antes de reintentar." : "No se pudo conectar con el servidor Python. Comprueba que siga ejecutándose.");
  }
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error("Respuesta no válida del servidor. Actualiza el tablero antes de reintentar.");
  }
  if (response.status === 401 && window.handleUnauthorized) window.handleUnauthorized();
  if (!response.ok) throw new Error(data.error || "No se pudo completar la operación.");
  return data;
}

function notice(message, kind = "warning") {
  $("#notice").textContent = message;
  $("#notice").className = `alert alert-${kind}`;
  $("#notice").hidden = !message;
  setTimeout(() => {
    if ($("#notice").textContent === message) $("#notice").hidden = true;
  }, 8000);
}

function setBusy(busy) {
  state.busy = busy;
  $("#loading").hidden = !busy;
  $("#refresh").disabled = busy;
  $("#table-refresh").disabled = busy;
  document.querySelectorAll("#task-table input, #task-table select, #task-table button").forEach(el => el.disabled = busy || el.dataset.locked === "true");
  $("#save-task").disabled = busy || !state.config;
  $("#save-task").textContent = busy ? "Procesando…" : state.editing ? "Guardar cambios ↗" : "Registrar tarea ↗";
}

function populate(selector, values, placeholder = "", preserve = false) {
  const el = $(selector), before = el.value;
  el.replaceChildren();
  if (placeholder) el.add(new Option(placeholder, ""));
  values.forEach(value => el.add(new Option(value, value)));
  if (preserve && [...el.options].some(option => option.value === before)) el.value = before;
}

async function loadConfig() {
  const data = await api("/api/config");
  state.config = data.parameters;
  state.csrf = data.csrf_token;
  [["assignee", "people"], ["complexity", "complexities"], ["status", "statuses"]].forEach(([field, key]) => populate(`#task-${field}`, state.config[key], "Selecciona una opción", true));
  categoryOptions("#task-category", false);
  if (!$("#task-status").value) $("#task-status").value = state.config.statuses[0];
  updateFilterOptions();
}

function updateFilterOptions() {
  const union = (key, field) => [...new Set([...state.config[key], ...state.tasks.map(t => t[field]).filter(Boolean)])];
  populateMulti("#filter-person", union("people", "assignee"), "Todos los responsables");
  populateMulti("#filter-status", union("statuses", "status"), "Todos los estados");
  const priorities = [...new Set(state.tasks.map(t => String(t.priority)).filter(Boolean))].sort((a, b) => Number(String(b).replace(",", ".")) - Number(String(a).replace(",", ".")));
  populateMulti("#filter-priority", priorities, "Todas las prioridades");
  categoryOptions("#filter-category", true);
}

function multiValues(selector) {
  return new Set([...document.querySelectorAll(`${selector} input:checked`)].map(input => input.value));
}

function populateMulti(selector, values, emptyLabel) {
  const control = $(selector), selected = multiValues(selector), options = control.querySelector(".multi-filter-options");
  const existing = control.querySelectorAll("input[type=checkbox]").length;
  const selectNew = !control.dataset.initialized || existing === 0 || selected.size === existing;
  options.innerHTML = values.map(
    value => `<label><input type="checkbox" value="${escapeHtml(value)}" ${selectNew || selected.has(String(value)) ? 'checked' : ''}>
    <span>${escapeHtml(value)}</span></label>`).join("");
  options.insertAdjacentHTML("afterbegin", '<div class="multi-filter-actions"><button type="button" data-multi-action="all">Seleccionar todos</button><button type="button" data-multi-action="none">Borrar todos</button></div>');
  control.dataset.initialized = "true";
  updateMultiLabel(selector, emptyLabel);
}

function updateMultiLabel(selector, emptyLabel) {
  const selected = [...multiValues(selector)], total = document.querySelectorAll(`${selector} input[type=checkbox]`).length, summary = $(`${selector} summary`);
  summary.textContent = selected.length === total && total ? emptyLabel : !selected.length ? "Ninguno seleccionado" : selected.length === 1 ? selected[0] : `${selected.length} seleccionados`;
}

const multiLabels = {"filter-person":"Todos los responsables", "filter-status":"Todos los estados", "filter-priority":"Todas las prioridades"};
function setMultiSelection(control, checked) {
  control.querySelectorAll("input[type=checkbox]").forEach(input => input.checked = checked);
  updateMultiLabel("#" + control.id, multiLabels[control.id]);
}

async function refresh(keepNotice = false) {
  if (state.busy) return;
  setBusy(true);
  if (!keepNotice) notice("");
  try {
    await loadConfig();
    const data = await api("/api/tasks");
    state.tasks = data.tasks;
    state.loaded = true;
    updateFilterOptions();
    if (data.warnings.length) notice(data.warnings.join(" "));
  } catch (error) {
    state.loaded = false;
    state.tasks = [];
    notice(error.message, "danger");
  } finally {
    setBusy(false);
    render();
  }
}

function filteredTasks() {
  const number = value => Number(String(value).replace(",", ".")) || -Infinity;
  const category = $("#filter-category").value, people = multiValues("#filter-person"), statuses = multiValues("#filter-status"), priorities = multiValues("#filter-priority");
  return state.tasks.filter(task => (!category || task.category === category) && people.has(String(task.assignee)) && statuses.has(String(task.status)) && priorities.has(String(task.priority))).sort((a, b) => number(b.priority) - number(a.priority));
}

function switchView() {
  const requested = location.hash.slice(1);
  state.view = ["board", "timeline", "create", "table", "admin"].includes(requested) && (requested !== "admin" || window.authState?.user?.role === "admin") ? requested : "board";
  const headings = { board: ["Tablero de tareas", "Tu trabajo, en perspectiva", "Prioridades claras. Un equipo conectado. Todo en un mismo tablero."], timeline: ["Cronograma", "Un plan que toma forma", "Conecta las fechas con el trabajo y encuentra el ritmo de tu equipo."], create: ["Registrar tarea", "Dale espacio a lo que sigue", "Convierte una idea en un siguiente paso claro para tu equipo."] };
  headings.table = ["Tabla de tareas", "Los detalles, en una sola vista", "Ajusta prioridades, estados y fechas directamente en cada fila."];
  headings.admin = ["Usuarios y auditoría", "Control del espacio de trabajo", "Crea usuarios y consulta quién realizó cada acción."];
  const [label, title, subtitle] = headings[state.view];
  $("#breadcrumb").textContent = label;
  $("#page-title").innerHTML = `${title}<span>.</span>`;
  $("#page-subtitle").textContent = subtitle;
  if (state.view === "create" && state.editing) {
    $("#page-title").textContent = "Cada detalle cuenta.";
    $("#breadcrumb").textContent = "Editar tarea";
    $("#page-subtitle").textContent = "Ajusta la tarea, su prioridad y sus fechas de trabajo.";
  }
  document.querySelectorAll("[data-view]").forEach(link => {
    link.classList.toggle("active", link.dataset.view === state.view);
    if (link.dataset.view === state.view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  ["board", "timeline", "create", "table", "admin"].forEach(view => $(`#${view}-view`).hidden = state.view !== view);
  ["#overview", "#filters", ".new-task"].forEach(selector => $(selector).hidden = ["create", "admin"].includes(state.view));
  if (state.view === "admin" && window.loadAdmin) window.loadAdmin();
  render();
}

function taskFlag(task) {
  const flags = [];
  if (task.dead_time) flags.push('<span class="task-flag dead-time">Tiempo muerto</span>');
  if (isOverdue(task)) flags.push('<span class="task-flag overdue">Vencida</span>');
  else if (isUpcoming(task)) flags.push('<span class="task-flag">Próxima a iniciar</span>');
  return flags.join("");
}

function card(task, index) {
  const code = task.code || `${task.category.toUpperCase()} · fila ${task.row}`;
  return `<button class="task-card compact-card ${isOverdue(task) ? 'is-overdue' : ''} 
    ${task.dead_time ? 'is-dead-time' : ''}" draggable="true" data-task-index="${index}" type="button" title="${escapeHtml(code + ' · ' + task.name + ' · Prioridad ' + task.priority + ' · ' + task.assignee +
    (task.dead_time ? ' · Tiempo muerto' : ''))}"><span class="task-priority task-priority-${parseInt(task.priority)}">${task.priority}</span><span class="task-code">${escapeHtml(code)}</span><span class="compact-title">${escapeHtml(task.name)}</span>
    ${task.dead_time ? '<span class="dead-time-dot" aria-label="Tiempo muerto" title="Tiempo muerto">●</span>' : ''}</button>`;
}

function empty(message, description, action = false) {
  return `<div class="empty-state"><h3>${escapeHtml(message)}</h3><p>${escapeHtml(description)}</p>${action ? '<a href="#create" class="btn btn-primary">＋ Registrar primera tarea</a>' : ""}</div>`;
}

function render() {
  renderTaskTable(state.config ? filteredTasks() : []);
  if (!state.config) {
    $("#board").innerHTML = empty("La configuración necesita atención", "Revisa el mensaje de conexión y vuelve a actualizar.");
    return;
  }
  const tasks = filteredTasks();
  $("#stat-total").textContent = state.loaded ? tasks.length : "—";
  $("#stat-open").textContent = state.loaded ? tasks.filter(t => !isDone(t)).length : "—";
  $("#stat-done").textContent = state.loaded ? tasks.filter(isDone).length : "—";
  $("#stat-overdue").textContent = state.loaded ? tasks.filter(isOverdue).length : "—";
  $("#result-count").textContent = state.loaded ? tasks.length : "—";
  if (!state.loaded) {
    const html = empty("No pudimos cargar las tareas", "Revisa el aviso de conexión y pulsa Actualizar para volver a intentarlo.");
    $("#board").innerHTML = html;
    $("#timeline").innerHTML = html;
    return;
  }
  if (!tasks.length) {
    $("#board").innerHTML = state.tasks.length ? empty("Ninguna tarea coincide", "Prueba con otros filtros o pulsa Limpiar.") : empty("Aquí comienza el próximo avance", "Registra tu primera tarea para empezar a organizar el trabajo del equipo.", true);
  } else {
    const statuses = [...new Set([...state.config.statuses, ...tasks.map(t => t.status || "Sin estado")])];
    const selectedStatuses = multiValues("#filter-status");
    $("#board").innerHTML = statuses.filter(status => !selectedStatuses.size || selectedStatuses.has(status)).map(status => {
      let group = tasks.filter(task => (task.status || "Sin estado") === status);
      if (state.config.completed_statuses.includes(status)) {
        group = group.slice(0, 10);
      }
      return `<div class="kanban-column" data-drop-status="${escapeHtml(status)}" style="--status-color:${statusColor(status)}"><div class="column-heading"><span class="column-title"><span class="column-dot"></span>${escapeHtml(status)} <span class="count-pill">${group.length}</span></span><a href="#create" class="column-add" aria-label="Registrar tarea">＋</a></div>${group.length ? group.map(task => card(task, state.tasks.indexOf(task))).join("") : '<p class="column-empty">Sin tareas en este estado</p>'}</div>`;
    }).join("");
  }
  renderTimeline(tasks);
}

function period() {
  const start = dayNumber($("#timeline-start").value), end = dayNumber($("#timeline-end").value);
  const fallback = dayNumber(localToday());
  return Number.isFinite(start) && Number.isFinite(end) && end >= start ? [start, end] : [fallback, fallback];
}

function dateRange(task, actual = false) {
  const start = dayNumber(actual ? task.actual_start : task.planned_start);
  const end = dayNumber(actual ? (task.actual_end || localToday()) : task.planned_end);
  return Number.isFinite(start) && Number.isFinite(end) && end >= start ? [start, end] : null;
}

function renderTimeline(tasks) {
  const [start, end] = period(), days = end - start + 1;
  $("#period-label").textContent = start === end ? formatDate(isoDay(start), true) : `${formatDate(isoDay(start), true)} — ${formatDate(isoDay(end), true)}`;
  const overlap = range => range && range[0] <= end && range[1] >= start;
  const valid = tasks.filter(t => dateRange(t) || dateRange(t, true));
  const visible = valid.filter(t => overlap(dateRange(t)) || overlap(dateRange(t, true)));
  $("#timeline-note").textContent = `${visible.length} tareas en este periodo, agrupadas por persona. Se usa una segunda línea únicamente cuando hay superposición.`;
  if (!visible.length) {
    $("#timeline").innerHTML = empty("Un poco de espacio en el calendario", "No hay tareas con fechas en este periodo. Cambia la fecha o los filtros.");
    return;
  }
  const today = dayNumber(localToday());
  const holidaySet = new Set(state.config.holidays || []);
  const dayKind = day => {
    const value = isoDay(day), weekDay = new Date(day * DAY).getUTCDay();
    return holidaySet.has(value) ? "is-holiday" : (weekDay === 0 || weekDay === 6) ? "is-weekend" : "is-workday";
  };
  const headers = Array.from({ length: days }, (_, i) => {
    const day = start + i, value = isoDay(day), kind = dayKind(day);
    return `<span class="${kind} ${day === today ? 'is-today' : ''}" title="${kind === 'is-holiday' ? 'Festivo de Colombia' : kind === 'is-weekend' ? 'Fin de semana' : 'Día laboral'} · ${escapeHtml(formatDate(value, true))}">${new Date(day * DAY).getUTCDate()}</span>`;
  }).join("");
  const calendar = `<div class="gantt-calendar" aria-hidden="true">${Array.from({ length: days }, (_, i) => `<span class="${dayKind(start + i)}"></span>`).join("")}</div>`;
  const bar = interval => {
    const { task, actual, range, lane } = interval;
    const left = (Math.max(range[0], start) - start) / days * 100, width = (Math.min(range[1], end) - Math.max(range[0], start) + 1) / days * 100;
    const label = `${actual ? 'Real' : 'Programado'}: ${formatDate(isoDay(range[0]))} — ${formatDate(isoDay(range[1]))}`;
    return `<button type="button" class="gantt-bar ${actual ? 'actual' : ''}" style="--left:${left}%;--width:${width}%;--lane:${lane};--status-color:${statusColor(task.status)}" title="${escapeHtml(task.code + ' · ' + task.name + ' · ' + label)}" aria-label="${escapeHtml(task.name + ' · ' + label)}" data-task-index="${state.tasks.indexOf(task)}"><span class="task-priority task-priority-sm task-priority-${parseInt(task.priority)}">${escapeHtml(task.priority)}</span>${escapeHtml(task.code)} · ${escapeHtml(task.name)}</button>`;
  };
  const line = today >= start && today <= end ? `<span class="today-line" style="--left:${(today - start + .5) / days * 100}%"></span>` : "";
  const people = [...new Set(visible.map(task => task.assignee || "Sin responsable"))];
  const rows = people.map(person => {
    const personTasks = visible.filter(task => (task.assignee || "Sin responsable") === person);
    const intervals = [];
    personTasks.forEach(task => [[false, dateRange(task)], [true, dateRange(task, true)]].forEach(([actual, range]) => { if (overlap(range)) intervals.push({ task, actual, range }); }));
    intervals.sort((a, b) => a.range[0] - b.range[0] || a.range[1] - b.range[1]);
    const laneEnds = [-Infinity, -Infinity];
    intervals.forEach(interval => {
      let lane = laneEnds[0] < interval.range[0] ? 0 : laneEnds[1] < interval.range[0] ? 1 : (laneEnds[0] <= laneEnds[1] ? 0 : 1);
      interval.lane = lane; laneEnds[lane] = Math.max(laneEnds[lane], interval.range[1]);
    });
    const lanes = intervals.some(interval => interval.lane === 1) ? 2 : 1;
    return `<div class="gantt-row" style="--lanes:${lanes}"><div class="gantt-label"><strong>${escapeHtml(person)}</strong><small>${personTasks.length} tarea(s) · ${lanes} línea(s)</small></div><div class="gantt-track">${calendar}${line}${intervals.map(bar).join("")}</div></div>`;
  }).join("");
  $("#timeline").innerHTML = `<div class="gantt" style="--days:${days};min-width:${Math.max(750, days * 30 + 240)}px"><div class="gantt-header"><div class="gantt-label">RESPONSABLE</div><div class="gantt-days">${headers}</div></div>${rows}</div>`;
}

function showDetail(index) {
  const task = state.tasks[index];
  if (!task) return;
  if (state.busy) return;
  state.editing = task;
  const form = $("#task-form");
  form.reset();
  for (const element of form.elements) {
    if (!element.name) continue;
    const value = String(task[element.name] ?? "");
    if (element.tagName === "SELECT" && value && ![...element.options].some(o => o.value === value)) element.add(new Option(value + " (histórico)", value));
    element.value = value;
  }
  $("#task-dead-time").checked = Boolean(task.dead_time);

  $("#task-manual").checked = task.end_mode === "manual";
  $("#task-end").readOnly = !$("#task-manual").checked;
  $("#actual-dates").hidden = false;
  $("#form-title").textContent = "Editar tarea";
  $("#form-code").textContent = task.code || "Se asignará un código al guardar";

  $("#form-error").hidden = true;
  $("#schedule-hint").textContent = task.end_mode === "manual" ? "Entrega histórica o manual. Desmarca la opción para calcularla con días hábiles." : "Entrega automática. Incluye el día de inicio y excluye festivos.";
  setFormRequired(false);
  state.baseline = formValues();
  setBusy(false);
  location.hash = "create";
  switchView();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function syncScope() {
  const selectedPeople = multiValues("#filter-person").size, totalPeople = document.querySelectorAll("#filter-person input[type=checkbox]").length;
  const individual = selectedPeople > 0 && selectedPeople < totalPeople;
  $("#general-view").classList.toggle("selected", !individual);
  $("#person-view").classList.toggle("selected", individual);
  $("#general-view").setAttribute("aria-pressed", String(!individual));
  $("#person-view").setAttribute("aria-pressed", String(individual));
  render();
}

$("#task-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (state.busy || !state.config || !event.target.reportValidity()) return;
  const editing = state.editing;
  let payload = formValues();
  if (editing) {
    payload = Object.fromEntries(Object.entries(payload).filter(([key, value]) => value !== state.baseline[key]));
    payload.revision = editing.revision;
  } else {
    delete payload.actual_start;
    delete payload.actual_end;
  }
  $("#form-error").hidden = true;
  setBusy(true);
  let saved;
  try {
    const data = await api(editing ? `/api/tasks/${encodeURIComponent(editing.category)}/${editing.row}` : "/api/tasks", { method: editing ? "PUT" : "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf }, body: JSON.stringify(payload) });
    saved = data.task;
    resetTaskForm();
    notice(`Tarea ${saved.code} ${editing ? 'actualizada' : 'registrada'} correctamente.`, "success");
    $("#filter-category").value = "";
    document.querySelectorAll(".multi-filter").forEach(control => setMultiSelection(control, true));
    location.hash = "board";
  } catch (error) {
    $("#form-error").textContent = error.message;
    $("#form-error").hidden = false;
  }
  finally {
    setBusy(false);
  }
  if (saved) {
    await refresh(true);
    if (!state.loaded) notice(`La tarea ${saved.code} se guardó, pero no se pudo actualizar el tablero. Pulsa Actualizar;
no vuelvas a registrarla.`, "warning");
    syncScope();
  }
});
$("#filter-category").addEventListener("change", render);
$("#refresh").addEventListener("click", () => refresh());
document.querySelector(".filter-controls").addEventListener("change", event => {
  if (!event.target.matches(".multi-filter input")) return;
  const control = event.target.closest(".multi-filter");
  const labels = { "filter-person": "Todos los responsables", "filter-status": "Todos los estados", "filter-priority": "Todas las prioridades" };
  updateMultiLabel("#" + control.id, labels[control.id]);
  syncScope();
});
document.querySelector(".filter-controls").addEventListener("click", event => {
  const button = event.target.closest("[data-multi-action]");
  if (!button) return;
  const control = button.closest(".multi-filter");
  setMultiSelection(control, button.dataset.multiAction === "all");
  syncScope();
});
$("#general-view").addEventListener("click", () => {
  setMultiSelection($("#filter-person"), true);
  syncScope();
});
$("#person-view").addEventListener("click", () => {
  const inputs = [...document.querySelectorAll("#filter-person input[type=checkbox]")];
  if (!multiValues("#filter-person").size || multiValues("#filter-person").size === inputs.length) inputs.forEach((input,index) => input.checked = index === 0);
  updateMultiLabel("#filter-person", "Todos los responsables");
  syncScope();
  $("#filter-person").open = true;
});
$("#clear-filters").addEventListener("click", () => {
  $("#filter-category").value = "";
  document.querySelectorAll(".multi-filter").forEach(control => setMultiSelection(control, true));
  syncScope();
});
function setPeriodPreset(preset, anchor = localToday()) {
  const day = dayNumber(anchor), date = new Date(day * DAY), year = date.getUTCFullYear(), month = date.getUTCMonth();
  let start = day, end = day;
  if (preset === "week") { start = day - ((date.getUTCDay() + 6) % 7); end = start + 6; }
  if (preset === "month") { start = Date.UTC(year, month, 1) / DAY; end = Date.UTC(year, month + 1, 0) / DAY; }
  if (preset === "quarter") { const first = Math.floor(month / 3) * 3; start = Date.UTC(year, first, 1) / DAY; end = Date.UTC(year, first + 3, 0) / DAY; }
  $("#timeline-start").value = isoDay(start);
  $("#timeline-end").value = isoDay(end);
  render();
}
setPeriodPreset("month");
$("#timeline-start").addEventListener("change", () => {
  if ($("#timeline-end").value && $("#timeline-end").value < $("#timeline-start").value) $("#timeline-end").value = $("#timeline-start").value;
  $("#timeline-preset").value = "custom";
  render();
});
$("#timeline-end").addEventListener("change", () => {
  if ($("#timeline-start").value && $("#timeline-end").value < $("#timeline-start").value) $("#timeline-start").value = $("#timeline-end").value;
  $("#timeline-preset").value = "custom";
  render();
});
$("#timeline-preset").addEventListener("change", () => setPeriodPreset($("#timeline-preset").value, $("#timeline-start").value || localToday()));
function movePeriod(direction) {
  const [start, end] = period(), preset = $("#timeline-preset").value;
  if (preset === "month" || preset === "quarter") {
    const date = new Date(start * DAY), months = preset === "quarter" ? 3 : 1;
    setPeriodPreset(preset, isoDay(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + direction * months, 1) / DAY));
    return;
  }
  const width = end - start + 1, offset = direction * width;
  $("#timeline-start").value = isoDay(start + offset);
  $("#timeline-end").value = isoDay(end + offset);
  render();
}
$("#previous-period").addEventListener("click", () => movePeriod(-1));
$("#next-period").addEventListener("click", () => movePeriod(1));
$("#today-period").addEventListener("click", () => {
  setPeriodPreset($("#timeline-preset").value, localToday());
});
document.addEventListener("click", event => {
  const target = event.target.closest("[data-task-index]");
  if (target) showDetail(Number(target.dataset.taskIndex));
  if (event.target.closest('a[href="#create"]')) {
    resetTaskForm();
    switchView();
  }
});
$("#close-detail").addEventListener("click", () => $("#task-detail").close());
$("#task-detail").addEventListener("click", event => {
  if (event.target === $("#task-detail")) {
    const r = event.target.getBoundingClientRect();
    if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) event.target.close();
  }
});

function categoryOptions(selector, all) {
  const el = $(selector), current = el.value;
  el.replaceChildren();
  if (all) el.add(new Option("Todas las categorías", ""));
  state.config.categories.forEach(category => el.add(new Option(`${category.name} · ${category.code_prefix}`, category.id)));
  if ([...el.options].some(o => o.value === current)) el.value = current;
}

function formValues() {
  const values = Object.fromEntries(new FormData($("#task-form")));
  values.dead_time = $("#task-dead-time").checked;
  values.end_mode = $("#task-manual").checked ? "manual" : "auto";
  return values;
}

function setFormRequired(required) {
  ["#task-name", "#task-assignee", "#task-status", "#task-priority", "#task-complexity", "#task-days", "#task-start"].forEach(s => $(s).required = required);
}

function resetTaskForm() {
  state.editing = null;
  state.baseline = null;
  scheduleGeneration++;
  $("#task-form").reset();
  $("#actual-dates").hidden = true;
  $("#task-end").readOnly = true;
  $("#form-title").textContent = "Una nueva tarea, un nuevo avance";
  $("#form-code").textContent = "Se asignará al guardar";

  $("#schedule-hint").textContent = "Se incluye el día de inicio como primer día de trabajo.";
  $("#form-error").hidden = true;
  if (state.config) {
    [["assignee", "people"], ["complexity", "complexities"], ["status", "statuses"]].forEach(([field, key]) => populate(`#task-${field}`, state.config[key], "Selecciona una opción"));
    $("#task-status").value = state.config.statuses[0];
    categoryOptions("#task-category", false);
    if ($("#filter-category").value) $("#task-category").value = $("#filter-category").value;
  }
  setFormRequired(true);
  setBusy(state.busy);
}

let scheduleGeneration = 0;
let scheduleTimer;
async function calculateSchedule() {
  const generation = ++scheduleGeneration;
  const manual = $("#task-manual").checked;
  $("#task-end").readOnly = !manual;
  if (manual) {
    $("#schedule-hint").textContent = "Entrega ajustada manualmente; no se recalcula hasta desmarcar esta opción.";
    return;
  }
  const values = formValues();
  if (!values.planned_start || !values.estimated_days) {
    $("#task-end").value = "";
    $("#schedule-hint").textContent = "Selecciona el inicio y la duración para calcular la entrega.";
    return;
  }
  $("#schedule-hint").textContent = "Calculando días hábiles de Colombia…";
  try {
    const result = await api("/api/schedule", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf }, body: JSON.stringify({ planned_start: values.planned_start, estimated_days: values.estimated_days }) });
    if (generation !== scheduleGeneration) return;
    $("#task-end").value = result.planned_end;
    $("#schedule-hint").textContent = `Entrega: ${formatDate(result.planned_end, true)}. Inicio incluido;
sin domingos ni festivos.`;
  } catch (error) {
    if (generation === scheduleGeneration) {
      $("#task-end").value = "";
      $("#schedule-hint").textContent = error.message;
    }
  }
}
["#task-start", "#task-days", "#task-manual"].forEach(selector => $(selector).addEventListener("input", () => {
  clearTimeout(scheduleTimer);
  scheduleGeneration++;
  scheduleTimer = setTimeout(calculateSchedule, 180);
}));
$("#task-assignee").addEventListener("change", () => {
  if (!state.editing) {
    calculateSchedule();
  }
});
$("#task-category").addEventListener("change", () => {
  const category = state.config.categories.find(c => c.id === $("#task-category").value);
  $("#form-code").textContent = state.editing && state.editing.category === category.id ? state.editing.code || "Se asignará al guardar" : `${category.code_prefix}… · consecutivo al guardar`;
});

let dragIndex = null;
$("#board").addEventListener("dragstart", event => {
  const card = event.target.closest("[data-task-index]");
  if (!card || state.busy) {
    event.preventDefault();
    return;
  }
  dragIndex = Number(card.dataset.taskIndex);
  event.dataTransfer.setData("text/plain", String(dragIndex));
  event.dataTransfer.effectAllowed = "move";
  card.classList.add("dragging");
});
$("#board").addEventListener("dragover", event => {
  const column = event.target.closest("[data-drop-status]");
  if (column && dragIndex !== null && !state.busy) {
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    column.classList.add("drop-target");
  }
});
$("#board").addEventListener("dragleave", event => {
  const column = event.target.closest("[data-drop-status]");
  if (column && !column.contains(event.relatedTarget)) column.classList.remove("drop-target");
});
$("#board").addEventListener("dragend", () => {
  dragIndex = null;
  document.querySelectorAll(".dragging,.drop-target").forEach(el => el.classList.remove("dragging", "drop-target"));
});
$("#board").addEventListener("drop", async event => {
  const column = event.target.closest("[data-drop-status]"), task = state.tasks[dragIndex];
  if (!column || !task || state.busy) return;
  event.preventDefault();
  dragIndex = null;
  document.querySelectorAll(".dragging,.drop-target").forEach(el => el.classList.remove("dragging", "drop-target"));
  const status = column.dataset.dropStatus;
  if (status === task.status) return;
  setBusy(true);
  let saved = false;
  try {
    const result = await api(`/api/tasks/${encodeURIComponent(task.category)}/${task.row}`, { method: "PUT", headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf }, body: JSON.stringify({ status, revision: task.revision }) });
    notice(`${result.task.code}: ${status}.`, "success");
    saved = true;
  } catch (error) {
    notice(error.message, "danger");
  }
  finally {
    setBusy(false);
  }
  if (saved) await refresh(true);
});
window.addEventListener("hashchange", switchView);
bootstrapAuth();
