"use strict";
window.authState = {user:null};

async function authRequest(path, options={}) {
  const response = await fetch(path, {cache:"no-store", ...options});
  let data = {};
  try {data = await response.json();} catch {}
  if(response.status === 403 && String(data.error || "").includes("Sesión de página vencida")) {
    window.__forceReload = true;
    window.location.reload();
    throw new Error("Actualizando la sesión de seguridad…");
  }
  if(!response.ok) throw new Error(data.error || "No se pudo completar la operación.");
  return data;
}

function applyUser(user) {
  authState.user = user || null;
  document.querySelector("#current-user").textContent = user ? user.username : "";
  document.querySelector("#logout").hidden = !user;
  document.querySelectorAll(".admin-only").forEach(element=>element.hidden = user?.role !== "admin");
}

function showLogin(message="") {
  applyUser(null);
  const dialog = document.querySelector("#login-dialog");
  const error = document.querySelector("#login-error");
  error.textContent = message;
  error.hidden = !message;
  if(!dialog.open) dialog.showModal();
  document.querySelector("#login-username").focus();
}

window.handleUnauthorized = () => showLogin("Tu sesión terminó. Inicia sesión de nuevo.");

window.bootstrapAuth = async function() {
  try {
    const session = await authRequest("/api/session");
    if(!session.authenticated) return showLogin();
    applyUser(session.user);
    switchView();
    await refresh();
  } catch(error) {showLogin(error.message);}
};

document.querySelector("#login-form").addEventListener("submit", async event=>{
  event.preventDefault();
  const error = document.querySelector("#login-error");
  error.hidden = true;
  const button = event.submitter;
  button.disabled = true;
  try {
    const result = await authRequest("/api/login", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({username:document.querySelector("#login-username").value,password:document.querySelector("#login-password").value})});
    applyUser(result.user);
    document.querySelector("#login-password").value = "";
    document.querySelector("#login-dialog").close();
    switchView();
    await refresh();
  } catch(exc) {error.textContent=exc.message;error.hidden=false;}
  finally {button.disabled=false;}
});

document.querySelector("#logout").addEventListener("click", async ()=>{
  try {await authRequest("/api/logout", {method:"POST",headers:{"X-CSRF-Token":state.csrf}});} catch {}
  state.config=null; state.tasks=[]; state.loaded=false; state.csrf="";
  location.hash="board";
  showLogin();
});
document.querySelector("#login-dialog").addEventListener("cancel", event=>event.preventDefault());

function auditDescription(item) {
  const names = {"auth.login":"Inicio de sesión","auth.logout":"Cierre de sesión","auth.login_failed":"Intento de acceso fallido","user.bootstrap_admin":"Administrador permanente creado","user.create":"Usuario creado","task.create":"Tarea creada","task.update":"Tarea actualizada"};
  const details = item.details || {};
  const subject = details.code || details.username || details.user_id || "";
  return `${names[item.action] || item.action}${subject ? ` · ${subject}` : ""}`;
}

window.loadAdmin = async function() {
  if(authState.user?.role !== "admin" || !state.csrf) return;
  try {
    const [usersResult,auditResult] = await Promise.all([authRequest("/api/users"),authRequest("/api/audit")]);
    document.querySelector("#users-list").innerHTML = usersResult.users.map(user=>`<div class="user-row"><span class="team-avatar">${escapeHtml(user.username.slice(0,1).toUpperCase())}</span><div><strong>${escapeHtml(user.username)}</strong><small>${escapeHtml(user.role === "admin" ? "Administrador" : "Usuario")}${user.permanent ? " · Permanente" : ""}</small></div></div>`).join("") || '<p class="muted-note">No hay usuarios.</p>';
    document.querySelector("#audit-list").innerHTML = auditResult.audit.map(item=>`<div class="audit-row"><div><strong>${escapeHtml(auditDescription(item))}</strong><small>por ${escapeHtml(item.actor || "system")}</small></div><time>${escapeHtml(new Date(item.created_at).toLocaleString("es-CO"))}</time></div>`).join("") || '<p class="muted-note">Aún no hay actividad.</p>';
  } catch(error) {notice(error.message,"danger");}
};

document.querySelector("#user-form").addEventListener("submit", async event=>{
  event.preventDefault();
  const message=document.querySelector("#user-form-message"), button=event.submitter;
  button.disabled=true; message.textContent="Creando…";
  try {
    const result=await authRequest("/api/users",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":state.csrf},body:JSON.stringify({username:document.querySelector("#new-username").value,password:document.querySelector("#new-password").value,role:document.querySelector("#new-role").value})});
    event.target.reset(); message.textContent=`Usuario ${result.user.username} creado.`; await loadAdmin();
  } catch(error) {message.textContent=error.message;}
  finally {button.disabled=false;}
});
