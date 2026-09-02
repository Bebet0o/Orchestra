import { ControllerClientError, createControllerClient } from "/assets/controller-client.js";

const routes = Object.freeze({
  "/": {
    key: "dashboard",
    title: "Tableau de bord",
    message: "Connectez-vous pour lire le tableau de bord opérationnel.",
  },
  "/dashboard": {
    key: "dashboard",
    title: "Tableau de bord",
    message: "Connectez-vous pour lire le tableau de bord opérationnel.",
  },
  "/projects": {
    key: "projects",
    title: "Projets",
    message: "Connectez-vous pour créer, importer et administrer les projets via le Controller.",
  },
  "/blueprints": {
    key: "blueprints",
    title: "Blueprints",
    message: "Connectez-vous pour créer, valider, modifier et versionner les Blueprints.",
  },
  "/objectives": {
    key: "objectives",
    title: "Objectifs",
    message: "Connectez-vous pour créer et contrôler les objectifs via le Controller.",
  },
  "/executions": {
    key: "executions",
    title: "Exécutions",
    message: "Les plans, tâches, workers et sandboxes détaillés seront affichés au jalon 2V.",
  },
  "/reviews": {
    key: "reviews",
    title: "Reviews",
    message: "Les décisions humaines et Recovery seront disponibles au jalon 2W.",
  },
  "/events": {
    key: "events",
    title: "Événements",
    message: "Le flux temps réel et la réconciliation seront activés au jalon 2X.",
  },
  "/administration": {
    key: "administration",
    title: "Administration",
    message: "Les diagnostics bornés seront ajoutés progressivement sans exposer de secrets.",
  },
});

const client = createControllerClient();
const sessionPanel = document.getElementById("session-panel");
const sessionStatus = document.getElementById("session-status");
const sessionDetail = document.getElementById("session-detail");
const loginForm = document.getElementById("login-form");
const passwordInput = document.getElementById("operator-password");
const loginButton = document.getElementById("login-button");
const logoutButton = document.getElementById("logout-button");
const connectionBadge = document.getElementById("controller-connection");
const connectionMessage = document.getElementById("controller-message");
const dashboardPanel = document.getElementById("dashboard-panel");
const dashboardRefresh = document.getElementById("dashboard-refresh");
const dashboardStatus = document.getElementById("dashboard-status");
const dashboardCoverage = document.getElementById("dashboard-coverage");
const routePanel = document.getElementById("route-panel");
const projectPanel = document.getElementById("project-panel");
const projectRefresh = document.getElementById("project-refresh");
const projectStatus = document.getElementById("project-status");
const projectCoverage = document.getElementById("project-coverage");
const projectCount = document.getElementById("project-count");
const projectAdminList = document.getElementById("project-admin-list");
const projectCreateForm = document.getElementById("project-create-form");
const projectCreateMode = document.getElementById("project-create-mode");
const projectCreateUrlLabel = document.getElementById("project-create-url-label");
const projectCreateUrl = document.getElementById("project-create-url");
const projectCreateSubmit = document.getElementById("project-create-submit");
const projectDetailCard = document.getElementById("project-detail-card");
const projectDetailTitle = document.getElementById("project-detail-title");
const projectDetailState = document.getElementById("project-detail-state");
const projectDetailMeta = document.getElementById("project-detail-meta");
const projectUpdateForm = document.getElementById("project-update-form");
const projectUpdateSubmit = document.getElementById("project-update-submit");
const projectCommandReason = document.getElementById("project-command-reason");
const projectCommandButtons = document.getElementById("project-command-buttons");
const blueprintPanel = document.getElementById("blueprint-panel");
const blueprintRefresh = document.getElementById("blueprint-refresh");
const blueprintStatus = document.getElementById("blueprint-status");
const blueprintCount = document.getElementById("blueprint-count");
const blueprintList = document.getElementById("blueprint-list");
const blueprintNew = document.getElementById("blueprint-new");
const blueprintEditorTitle = document.getElementById("blueprint-editor-title");
const blueprintEditorMode = document.getElementById("blueprint-editor-mode");
const blueprintMeta = document.getElementById("blueprint-meta");
const blueprintSource = document.getElementById("blueprint-source");
const blueprintValidate = document.getElementById("blueprint-validate");
const blueprintSave = document.getElementById("blueprint-save");
const blueprintValidity = document.getElementById("blueprint-validity");
const blueprintDiagnostics = document.getElementById("blueprint-diagnostics");
const blueprintPreview = document.getElementById("blueprint-preview");
const blueprintRevisionCount = document.getElementById("blueprint-revision-count");
const blueprintRevisions = document.getElementById("blueprint-revisions");
const blueprintDiffFrom = document.getElementById("blueprint-diff-from");
const blueprintDiffTo = document.getElementById("blueprint-diff-to");
const blueprintCompare = document.getElementById("blueprint-compare");
const blueprintDiff = document.getElementById("blueprint-diff");
const objectivePanel = document.getElementById("objective-panel");
const objectiveRefresh = document.getElementById("objective-refresh");
const objectiveStatus = document.getElementById("objective-status");
const objectiveCoverage = document.getElementById("objective-coverage");
const objectiveCount = document.getElementById("objective-count");
const objectiveList = document.getElementById("objective-list");
const objectiveCreateForm = document.getElementById("objective-create-form");
const objectiveCreateProject = document.getElementById("objective-create-project");
const objectiveCreateSubmit = document.getElementById("objective-create-submit");
const objectiveDetailCard = document.getElementById("objective-detail-card");
const objectiveDetailTitle = document.getElementById("objective-detail-title");
const objectiveDetailState = document.getElementById("objective-detail-state");
const objectiveDetailMeta = document.getElementById("objective-detail-meta");
const objectiveDetailDescription = document.getElementById("objective-detail-description");
const objectiveDetailFacts = document.getElementById("objective-detail-facts");
const objectiveOperation = document.getElementById("objective-operation");
const objectiveCommandReason = document.getElementById("objective-command-reason");
const objectiveCommandButtons = document.getElementById("objective-command-buttons");

const dashboardResources = Object.freeze([
  Object.freeze({ key: "projects", load: () => client.projects() }),
  Object.freeze({ key: "objectives", load: () => client.objectives() }),
  Object.freeze({ key: "reviews", load: () => client.reviews() }),
  Object.freeze({ key: "recoveries", load: () => client.recoveries() }),
  Object.freeze({ key: "plans", load: () => client.plans() }),
  Object.freeze({ key: "assignments", load: () => client.reviewerAssignments() }),
]);

const activeObjectiveStates = new Set(["planned", "planning", "running", "blocked", "paused"]);
const activePlanTerminalStates = new Set(["succeeded", "failed", "cancelled", "completed"]);
const objectiveAttentionStates = new Set(["blocked", "failed"]);
const reviewAttentionStates = new Set(["rejected", "pending", "running", "human_review", "debt"]);
const recoveryAttentionStates = new Set(["blocked", "failed", "pending", "recovery_required"]);
const assignmentAttentionStates = new Set(["assigned", "claimed", "failed", "pending"]);

let authenticated = false;
let dashboardLoading = false;
let dashboardGeneration = 0;
let dashboardLoaded = false;
let projectLoading = false;
let projectGeneration = 0;
let projectsLoaded = false;
let selectedProjectId = "";
let selectedProjectEtag = "";
let selectedProject = null;
let blueprintLoading = false;
let blueprintGeneration = 0;
let blueprintsLoaded = false;
let selectedBlueprintId = "";
let selectedBlueprintEtag = "";
let selectedBlueprintRevision = 0;
let blueprintValidated = false;
let objectiveLoading = false;
let objectiveGeneration = 0;
let objectivesLoaded = false;
let selectedObjectiveId = "";
let selectedObjective = null;

function canonicalPath(pathname) {
  if (pathname.length > 1 && pathname.endsWith("/")) {
    return pathname.slice(0, -1);
  }
  return pathname;
}

function routeFor(pathname) {
  return routes[canonicalPath(pathname)] ?? routes["/"];
}

function currentRoute() {
  return routeFor(window.location.pathname);
}

function safeText(value, fallback = "Non renseigné", maximum = 120) {
  if (typeof value !== "string") {
    return fallback;
  }
  const normalized = value.replace(/[\u0000-\u001f\u007f]/g, " ").trim();
  return normalized.slice(0, maximum) || fallback;
}

function safeState(item) {
  return safeText(item && item.state, "inconnu", 40).toLowerCase();
}

function safeId(item, fallback) {
  return safeText(item && item.id, fallback, 96);
}

function displayFunctionalPanel() {
  const key = currentRoute().key;
  const dashboardRoute = key === "dashboard";
  const projectsRoute = key === "projects";
  const blueprintsRoute = key === "blueprints";
  const objectivesRoute = key === "objectives";
  dashboardPanel.hidden = !dashboardRoute || !authenticated;
  projectPanel.hidden = !projectsRoute || !authenticated;
  blueprintPanel.hidden = !blueprintsRoute || !authenticated;
  objectivePanel.hidden = !objectivesRoute || !authenticated;
  routePanel.hidden = authenticated && (dashboardRoute || projectsRoute || blueprintsRoute || objectivesRoute);
}

function render(pathname, focusMain = false) {
  const route = routeFor(pathname);
  document.title = `${route.title} · Orchestra Console`;
  document.getElementById("page-title").textContent = route.title;
  document.getElementById("route-title").textContent = route.title;
  document.getElementById("route-message").textContent = route.message;

  document.querySelectorAll("nav a[data-route]").forEach((link) => {
    const active = link.dataset.route === route.key;
    link.removeAttribute("aria-current");
    if (active) {
      link.setAttribute("aria-current", "page");
    }
  });

  displayFunctionalPanel();
  if (route.key === "dashboard" && authenticated && !dashboardLoaded) {
    void refreshDashboard();
  }
  if (route.key === "projects" && authenticated && !projectsLoaded) {
    void refreshProjects();
  }
  if (route.key === "blueprints" && authenticated && !blueprintsLoaded) {
    void refreshBlueprints();
  }
  if (route.key === "objectives" && authenticated && !objectivesLoaded) {
    void refreshObjectives();
  }

  if (focusMain) {
    document.getElementById("main-content").focus({ preventScroll: true });
  }
}

function setConnection(kind, label, message) {
  connectionBadge.dataset.state = kind;
  connectionBadge.textContent = label;
  connectionMessage.textContent = message;
}

function showSignedOut(message = "Authentification requise pour accéder aux données du Controller.") {
  authenticated = false;
  dashboardGeneration += 1;
  dashboardLoading = false;
  dashboardLoaded = false;
  dashboardRefresh.disabled = false;
  clearProjectState();
  clearBlueprintState();
  clearObjectiveState();
  sessionPanel.dataset.state = "signed-out";
  sessionStatus.textContent = "Session fermée";
  sessionDetail.textContent = message;
  loginForm.hidden = false;
  logoutButton.hidden = true;
  passwordInput.value = "";
  setConnection("signed-out", "Controller accessible", "La session navigateur n’est pas ouverte.");
  displayFunctionalPanel();
}

function showAuthenticated(session, capabilities) {
  const actor = session.actor_id === "operator" ? "Opérateur local" : "Session authentifiée";
  const features = capabilities && capabilities.features && typeof capabilities.features === "object"
    ? Object.values(capabilities.features).filter(Boolean).length
    : 0;
  authenticated = true;
  dashboardLoading = false;
  dashboardRefresh.disabled = false;
  sessionPanel.dataset.state = "authenticated";
  sessionStatus.textContent = actor;
  sessionDetail.textContent = `${features} capacités Controller annoncées. Les collections du dashboard restent bornées.`;
  loginForm.hidden = true;
  logoutButton.hidden = false;
  passwordInput.value = "";
  setConnection("authenticated", "Controller connecté", "Session vérifiée par l’état autoritaire du Controller.");
  displayFunctionalPanel();
  if (currentRoute().key === "dashboard") {
    void refreshDashboard();
  }
  if (currentRoute().key === "projects") {
    void refreshProjects();
  }
  if (currentRoute().key === "blueprints") {
    void refreshBlueprints();
  }
  if (currentRoute().key === "objectives") {
    void refreshObjectives();
  }
}

function showUnavailable(error) {
  authenticated = false;
  dashboardGeneration += 1;
  dashboardLoading = false;
  dashboardLoaded = false;
  dashboardRefresh.disabled = false;
  clearProjectState();
  clearBlueprintState();
  clearObjectiveState();
  sessionPanel.dataset.state = "unavailable";
  sessionStatus.textContent = "Controller indisponible";
  const requestSuffix = error instanceof ControllerClientError && error.requestId
    ? ` Référence : ${error.requestId}.`
    : "";
  sessionDetail.textContent = `Aucune commande n’est mise en attente dans le navigateur.${requestSuffix}`;
  loginForm.hidden = true;
  logoutButton.hidden = true;
  passwordInput.value = "";
  setConnection("unavailable", "Mode dégradé", "La navigation locale reste disponible en lecture statique.");
  displayFunctionalPanel();
}

function clearList(list) {
  list.replaceChildren();
}

function appendEmpty(list, message) {
  const item = document.createElement("li");
  item.className = "empty-state";
  item.textContent = message;
  list.append(item);
}

function appendOperationalItem(list, { label, title, state, detail }) {
  const item = document.createElement("li");
  const top = document.createElement("div");
  const type = document.createElement("span");
  const heading = document.createElement("strong");
  const badge = document.createElement("span");
  const description = document.createElement("p");

  top.className = "operational-item-heading";
  type.className = "operational-type";
  type.textContent = label;
  heading.textContent = title;
  badge.className = "state-badge";
  badge.dataset.state = state;
  badge.textContent = state;
  description.textContent = detail;

  top.append(type, badge);
  item.append(top, heading, description);
  list.append(item);
}

function collectionItems(collections, key) {
  return collections[key] ? collections[key].items : [];
}

function attentionEntries(collections) {
  const entries = [];
  for (const objective of collectionItems(collections, "objectives")) {
    const state = safeState(objective);
    if (objectiveAttentionStates.has(state)) {
      const projects = Array.isArray(objective.project_ids)
        ? objective.project_ids.map((value) => safeText(value, "", 48)).filter(Boolean).join(", ")
        : "Projet non renseigné";
      entries.push({
        label: "Objectif",
        title: safeText(objective.title, safeId(objective, "Objectif")),
        state,
        detail: projects || "Projet non renseigné",
      });
    }
  }
  for (const review of collectionItems(collections, "reviews")) {
    const state = safeState(review);
    if (reviewAttentionStates.has(state)) {
      entries.push({
        label: "Review",
        title: safeId(review, "Review"),
        state,
        detail: safeText(review.summary, `Projet ${safeText(review.project_id, "inconnu", 48)}`),
      });
    }
  }
  for (const recovery of collectionItems(collections, "recoveries")) {
    const state = safeState(recovery);
    if (recoveryAttentionStates.has(state)) {
      entries.push({
        label: "Recovery",
        title: safeId(recovery, "Recovery"),
        state,
        detail: `Projet ${safeText(recovery.project_id, "inconnu", 48)}`,
      });
    }
  }
  for (const assignment of collectionItems(collections, "assignments")) {
    const state = safeState(assignment);
    if (assignmentAttentionStates.has(state)) {
      entries.push({
        label: "Review assignée",
        title: safeId(assignment, "Assignation"),
        state,
        detail: `Run ${safeText(assignment.run_id, "non renseigné", 72)}`,
      });
    }
  }
  return entries.slice(0, 8);
}

function renderDashboard(collections, errors) {
  const projects = collectionItems(collections, "projects");
  const objectives = collectionItems(collections, "objectives");
  const plans = collectionItems(collections, "plans");
  const enabledProjects = projects.filter((item) => safeState(item) === "enabled");
  const activeObjectives = objectives.filter((item) => activeObjectiveStates.has(safeState(item)));
  const attention = attentionEntries(collections);

  document.getElementById("metric-projects").textContent = String(enabledProjects.length);
  document.getElementById("metric-projects-detail").textContent = `${projects.length} projet(s) visible(s) sur la page bornée.`;
  document.getElementById("metric-objectives").textContent = String(activeObjectives.length);
  document.getElementById("metric-objectives-detail").textContent = `${objectives.length} objectif(s) visible(s), états Controller conservés.`;
  document.getElementById("metric-attention").textContent = String(attention.length);
  document.getElementById("metric-attention-detail").textContent = attention.length
    ? "Éléments visibles nécessitant une vérification opérateur."
    : "Aucun blocage explicite dans les données reçues.";
  document.getElementById("attention-count").textContent = String(attention.length);

  const attentionList = document.getElementById("attention-list");
  clearList(attentionList);
  if (attention.length === 0) {
    appendEmpty(attentionList, "Aucun élément d’attention explicite dans les collections disponibles.");
  } else {
    attention.forEach((entry) => appendOperationalItem(attentionList, entry));
  }

  const activeList = document.getElementById("active-work-list");
  clearList(activeList);
  const activePlans = plans
    .filter((item) => !activePlanTerminalStates.has(safeState(item)))
    .slice(0, 6);
  if (activePlans.length === 0) {
    appendEmpty(activeList, "Aucun plan actif visible sur la première page.");
  } else {
    for (const plan of activePlans) {
      const counts = plan && typeof plan.task_counts === "object" && plan.task_counts !== null
        ? plan.task_counts
        : {};
      appendOperationalItem(activeList, {
        label: "Plan",
        title: safeId(plan, "Plan"),
        state: safeState(plan),
        detail: `${Number.isSafeInteger(counts.total) ? counts.total : 0} tâche(s) · objectif ${safeText(plan.objective_id, "inconnu", 72)}`,
      });
    }
  }

  const projectList = document.getElementById("project-list");
  clearList(projectList);
  if (projects.length === 0) {
    appendEmpty(projectList, "Aucun projet visible dans la projection Controller.");
  } else {
    for (const project of projects.slice(0, 6)) {
      appendOperationalItem(projectList, {
        label: "Projet",
        title: safeText(project.name, safeId(project, "Projet")),
        state: safeState(project),
        detail: `Branche ${safeText(project.default_branch, "non renseignée", 48)}`,
      });
    }
  }

  const truncated = Object.values(collections).some((collection) => collection.truncated);
  const unavailable = errors.length;
  const coverage = [];
  coverage.push(truncated
    ? "Une ou plusieurs collections possèdent une page suivante : le dashboard reste volontairement partiel."
    : "Toutes les premières pages reçues sont complètes selon leurs métadonnées.");
  if (unavailable) {
    coverage.push(`${unavailable} collection(s) indisponible(s) : les autres données restent affichées sans extrapolation.`);
  }
  dashboardCoverage.textContent = coverage.join(" ");

  const now = new Date();
  const readableTime = Number.isNaN(now.getTime())
    ? "lecture terminée"
    : now.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  dashboardStatus.textContent = unavailable
    ? `Lecture partielle à ${readableTime}.`
    : `Lecture complète des six collections à ${readableTime}.`;
}

async function refreshDashboard() {
  if (!authenticated || dashboardLoading) {
    return;
  }
  dashboardLoading = true;
  dashboardRefresh.disabled = true;
  dashboardStatus.textContent = "Lecture des projections Controller…";
  const generation = ++dashboardGeneration;
  try {
    const settled = await Promise.allSettled(dashboardResources.map((resource) => resource.load()));
    if (generation !== dashboardGeneration || !authenticated) {
      return;
    }
    const collections = {};
    const errors = [];
    settled.forEach((result, index) => {
      const resource = dashboardResources[index];
      if (result.status === "fulfilled") {
        collections[resource.key] = result.value;
      } else {
        errors.push(result.reason);
      }
    });
    const sessionError = errors.find((error) => error instanceof ControllerClientError && error.status === 401);
    if (sessionError) {
      showSignedOut("Session expirée. Reconnectez-vous pour actualiser les données.");
      return;
    }
    if (Object.keys(collections).length === 0) {
      dashboardStatus.textContent = "Aucune collection opérationnelle n’est disponible.";
      dashboardCoverage.textContent = "Le dashboard ne conserve aucune ancienne valeur et n’invente aucun état.";
      return;
    }
    renderDashboard(collections, errors);
    dashboardLoaded = true;
  } finally {
    if (generation === dashboardGeneration) {
      dashboardLoading = false;
      dashboardRefresh.disabled = false;
    }
  }
}

function clearProjectState() {
  projectGeneration += 1;
  projectLoading = false;
  projectsLoaded = false;
  selectedProjectId = "";
  selectedProjectEtag = "";
  selectedProject = null;
  projectRefresh.disabled = false;
  projectCount.textContent = "0";
  projectAdminList.replaceChildren();
  projectDetailCard.hidden = true;
  projectCommandReason.value = "";
}

function projectErrorMessage(error, fallback) {
  const reference = error instanceof ControllerClientError && error.requestId
    ? ` Référence : ${error.requestId}.`
    : "";
  const title = error instanceof Error ? safeText(error.message, fallback, 160) : fallback;
  return `${title}${reference}`;
}

function setProjectBusy(busy) {
  projectLoading = busy;
  projectRefresh.disabled = busy;
  projectCreateSubmit.disabled = busy;
  projectUpdateSubmit.disabled = busy;
  projectCreateForm.querySelectorAll("input, select").forEach((control) => {
    control.disabled = busy;
  });
  projectUpdateForm.querySelectorAll("input").forEach((control) => {
    control.disabled = busy;
  });
  projectCommandButtons.querySelectorAll("button").forEach((button) => {
    button.disabled = busy;
  });
}

function renderProjectList(collection) {
  projectAdminList.replaceChildren();
  projectCount.textContent = String(collection.items.length);
  projectCoverage.textContent = collection.truncated
    ? "Une page suivante existe : la liste reste volontairement bornée."
    : "La première page reçue est complète selon les métadonnées Controller.";
  if (collection.items.length === 0) {
    appendEmpty(projectAdminList, "Aucun projet enregistré.");
    return;
  }
  for (const project of collection.items) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    const heading = document.createElement("strong");
    const detail = document.createElement("span");
    const state = document.createElement("span");
    const identifier = safeId(project, "projet");
    button.type = "button";
    button.dataset.projectId = identifier;
    button.className = "project-list-button";
    if (identifier === selectedProjectId) {
      button.setAttribute("aria-current", "true");
    }
    heading.textContent = safeText(project.name, identifier, 120);
    detail.textContent = `${identifier} · ${safeText(project.default_branch, "branche inconnue", 64)}`;
    state.className = "state-badge";
    state.dataset.state = safeState(project);
    state.textContent = safeState(project);
    button.append(heading, detail, state);
    item.append(button);
    projectAdminList.append(item);
  }
}

function renderProjectDetail(project, etag) {
  selectedProject = project;
  selectedProjectId = safeId(project, "");
  selectedProjectEtag = typeof etag === "string" ? etag : "";
  projectDetailCard.hidden = false;
  projectDetailTitle.textContent = safeText(project.name, selectedProjectId, 120);
  const state = safeState(project);
  projectDetailState.dataset.state = state;
  projectDetailState.textContent = state;
  projectDetailMeta.textContent = [
    `Identifiant ${selectedProjectId}`,
    `branche ${safeText(project.default_branch, "inconnue", 64)}`,
    `mode ${safeText(project.repository && project.repository.mode, "non renseigné", 32)}`,
    `révision ${Number.isInteger(project.resource_revision) ? project.resource_revision : "inconnue"}`,
  ].join(" · ");
  document.getElementById("project-update-name").value = safeText(project.name, "", 120);
  document.getElementById("project-update-policy").value = safeText(project.policy_id, "default", 63);
  document.getElementById("project-update-sandbox").value = typeof project.sandbox_profile_id === "string"
    ? safeText(project.sandbox_profile_id, "", 63)
    : "";
  projectCommandReason.value = "";
  projectCommandButtons.querySelectorAll("button[data-project-command]").forEach((button) => {
    const command = button.dataset.projectCommand;
    button.hidden = (command === "enable" && state === "enabled")
      || (command === "disable" && state !== "enabled")
      || (state === "archived" && command !== "rescan");
  });
}

async function selectProject(identifier) {
  if (!authenticated || projectLoading) {
    return;
  }
  setProjectBusy(true);
  projectStatus.textContent = `Lecture du projet ${safeText(identifier, "sélectionné", 63)}…`;
  try {
    const result = await client.project(identifier);
    renderProjectDetail(result.project, result.etag);
    projectStatus.textContent = "Détail projet chargé depuis le Controller.";
    const collection = await client.projects();
    renderProjectList(collection);
  } catch (error) {
    if (error instanceof ControllerClientError && error.status === 401) {
      showSignedOut("Session expirée. Reconnectez-vous pour administrer les projets.");
      return;
    }
    projectStatus.textContent = projectErrorMessage(error, "Lecture du projet impossible.");
  } finally {
    setProjectBusy(false);
  }
}

async function refreshProjects() {
  if (!authenticated || projectLoading) {
    return;
  }
  const generation = ++projectGeneration;
  setProjectBusy(true);
  projectStatus.textContent = "Lecture du registre projet…";
  try {
    const collection = await client.projects();
    if (generation !== projectGeneration || !authenticated) {
      return;
    }
    renderProjectList(collection);
    projectsLoaded = true;
    projectStatus.textContent = `${collection.items.length} projet(s) reçu(s) du Controller.`;
    if (selectedProjectId && collection.items.some((item) => safeId(item, "") === selectedProjectId)) {
      const result = await client.project(selectedProjectId);
      if (generation === projectGeneration && authenticated) {
        renderProjectDetail(result.project, result.etag);
      }
    } else {
      selectedProjectId = "";
      selectedProjectEtag = "";
      selectedProject = null;
      projectDetailCard.hidden = true;
    }
  } catch (error) {
    if (error instanceof ControllerClientError && error.status === 401) {
      showSignedOut("Session expirée. Reconnectez-vous pour administrer les projets.");
      return;
    }
    projectStatus.textContent = projectErrorMessage(error, "Registre projet indisponible.");
  } finally {
    if (generation === projectGeneration) {
      setProjectBusy(false);
    }
  }
}

async function runProjectMutation(label, operation) {
  if (!authenticated || projectLoading) {
    return;
  }
  setProjectBusy(true);
  projectStatus.textContent = `${label}…`;
  try {
    const accepted = await operation();
    const operationId = safeText(accepted.operation_id, "opération acceptée", 96);
    projectStatus.textContent = `${label} acceptée par le Controller · ${operationId}.`;
    projectsLoaded = false;
    setProjectBusy(false);
    await refreshProjects();
  } catch (error) {
    if (error instanceof ControllerClientError && error.status === 401) {
      showSignedOut("Session expirée. Reconnectez-vous avant toute nouvelle commande.");
      return;
    }
    projectStatus.textContent = projectErrorMessage(error, `${label} impossible.`);
  } finally {
    setProjectBusy(false);
  }
}

async function submitProjectCreate() {
  const mode = projectCreateMode.value;
  const sandbox = document.getElementById("project-create-sandbox").value.trim();
  const intent = {
    name: document.getElementById("project-create-name").value.trim(),
    slug: document.getElementById("project-create-slug").value.trim(),
    repository: {
      mode,
      default_branch: document.getElementById("project-create-branch").value.trim(),
      url: mode === "clone" ? projectCreateUrl.value.trim() : null,
    },
    policy_id: document.getElementById("project-create-policy").value.trim(),
    sandbox_profile_id: sandbox || null,
  };
  await runProjectMutation("Création du projet", () => client.createProject(intent));
  if (!projectLoading) {
    projectCreateForm.reset();
    projectCreateMode.value = "existing";
    document.getElementById("project-create-branch").value = "main";
    document.getElementById("project-create-policy").value = "default";
    projectCreateUrlLabel.hidden = true;
  }
}

function clearBlueprintState() {
  blueprintGeneration += 1;
  blueprintLoading = false;
  blueprintsLoaded = false;
  selectedBlueprintId = "";
  selectedBlueprintEtag = "";
  selectedBlueprintRevision = 0;
  blueprintValidated = false;
  blueprintRefresh.disabled = false;
  blueprintValidate.disabled = false;
  blueprintSave.disabled = false;
  blueprintCount.textContent = "0";
  blueprintRevisionCount.textContent = "0";
  blueprintList.replaceChildren();
  blueprintRevisions.replaceChildren();
  blueprintDiagnostics.replaceChildren();
  blueprintDiffFrom.replaceChildren();
  blueprintDiffTo.replaceChildren();
  blueprintSource.value = "";
  blueprintPreview.textContent = "Aucune prévisualisation.";
  blueprintDiff.textContent = "Aucune comparaison.";
  blueprintValidity.textContent = "non validé";
  blueprintValidity.dataset.state = "unknown";
  blueprintEditorMode.textContent = "nouveau";
  blueprintEditorMode.dataset.state = "draft";
  blueprintEditorTitle.textContent = "Éditeur Blueprint";
  blueprintMeta.textContent = "Chargez le modèle ou sélectionnez un profil.";
  blueprintSave.textContent = "Créer";
}

function setBlueprintBusy(busy) {
  blueprintLoading = busy;
  blueprintRefresh.disabled = busy;
  blueprintNew.disabled = busy;
  blueprintValidate.disabled = busy;
  blueprintSave.disabled = busy;
  blueprintCompare.disabled = busy;
  blueprintSource.disabled = busy;
}

function renderBlueprintList(collection) {
  blueprintList.replaceChildren();
  blueprintCount.textContent = String(collection.items.length);
  if (collection.items.length === 0) {
    appendEmpty(blueprintList, "Aucun Blueprint enregistré.");
    return;
  }
  for (const profile of collection.items) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    const heading = document.createElement("strong");
    const detail = document.createElement("span");
    const state = document.createElement("span");
    const identifier = safeId(profile, "");
    button.type = "button";
    button.dataset.blueprintId = identifier;
    button.className = "project-list-button";
    if (identifier === selectedBlueprintId) {
      button.setAttribute("aria-current", "true");
    }
    heading.textContent = safeText(profile.name, safeText(profile.profile_name, identifier, 63), 120);
    detail.textContent = `${safeText(profile.profile_name, "profil", 63)} · révision ${Number.isInteger(profile.source_revision) ? profile.source_revision : "?"}`;
    state.className = "state-badge";
    state.dataset.state = safeState(profile);
    state.textContent = safeState(profile);
    button.append(heading, detail, state);
    item.append(button);
    blueprintList.append(item);
  }
}

function renderBlueprintDiagnostics(preview) {
  blueprintDiagnostics.replaceChildren();
  const diagnostics = Array.isArray(preview && preview.diagnostics) ? preview.diagnostics.slice(0, 100) : [];
  const valid = preview && preview.valid === true;
  blueprintValidated = valid;
  blueprintValidity.textContent = valid ? "valide" : "invalide";
  blueprintValidity.dataset.state = valid ? "ready" : "failed";
  if (diagnostics.length === 0) {
    appendEmpty(blueprintDiagnostics, valid ? "Aucun diagnostic bloquant." : "Aucun diagnostic disponible.");
  } else {
    for (const diagnostic of diagnostics) {
      appendOperationalItem(blueprintDiagnostics, {
        label: safeText(diagnostic.severity, "diagnostic", 16),
        title: safeText(diagnostic.code, "validation", 128),
        state: safeText(diagnostic.severity, "unknown", 16).toLowerCase(),
        detail: `${safeText(diagnostic.path, "/", 256)} · ${safeText(diagnostic.message, "Diagnostic borné", 500)}`,
      });
    }
  }
  const projection = {
    canonical: preview && preview.canonical ? preview.canonical : null,
    runtime_config: preview && preview.runtime_config ? preview.runtime_config : null,
    source_sha256: preview && typeof preview.source_sha256 === "string" ? preview.source_sha256 : null,
    canonical_sha256: preview && typeof preview.canonical_sha256 === "string" ? preview.canonical_sha256 : null,
  };
  blueprintPreview.textContent = JSON.stringify(projection, null, 2);
}

function renderBlueprintRevisions(collection) {
  const revisions = collection.items;
  blueprintRevisions.replaceChildren();
  blueprintDiffFrom.replaceChildren();
  blueprintDiffTo.replaceChildren();
  blueprintRevisionCount.textContent = String(revisions.length);
  if (revisions.length === 0) {
    appendEmpty(blueprintRevisions, "Aucune révision disponible.");
    return;
  }
  for (const revision of revisions) {
    const number = Number(revision.source_revision);
    const item = document.createElement("li");
    const button = document.createElement("button");
    const heading = document.createElement("strong");
    const detail = document.createElement("span");
    button.type = "button";
    button.dataset.blueprintRevision = String(number);
    button.className = "project-list-button";
    heading.textContent = `Révision ${number}`;
    detail.textContent = `${safeText(revision.canonical_sha256, "empreinte inconnue", 64).slice(0, 16)}… · ${safeText(revision.created_at, "date inconnue", 40)}`;
    button.append(heading, detail);
    item.append(button);
    blueprintRevisions.append(item);
    for (const select of [blueprintDiffFrom, blueprintDiffTo]) {
      const option = document.createElement("option");
      option.value = String(number);
      option.textContent = `Révision ${number}`;
      select.append(option);
    }
  }
  if (revisions.length > 1) {
    blueprintDiffFrom.value = String(revisions[revisions.length - 1].source_revision);
    blueprintDiffTo.value = String(revisions[0].source_revision);
  }
}

async function loadBlueprint(identifier) {
  if (!authenticated || blueprintLoading) {
    return;
  }
  setBlueprintBusy(true);
  blueprintStatus.textContent = "Lecture du Blueprint sélectionné…";
  try {
    const result = await client.blueprint(identifier);
    const current = result.blueprint;
    const profile = current.profile || {};
    const revision = current.revision || {};
    selectedBlueprintId = safeId(profile, "");
    selectedBlueprintEtag = result.etag;
    selectedBlueprintRevision = Number.isInteger(profile.source_revision) ? profile.source_revision : 0;
    blueprintSource.value = typeof revision.source === "string" ? revision.source : "";
    blueprintEditorTitle.textContent = safeText(profile.name, safeText(profile.profile_name, "Blueprint", 63), 120);
    blueprintEditorMode.textContent = "édition";
    blueprintEditorMode.dataset.state = safeState(profile);
    blueprintMeta.textContent = `${selectedBlueprintId} · révision source ${selectedBlueprintRevision} · révision ressource ${Number.isInteger(profile.resource_revision) ? profile.resource_revision : "?"}`;
    blueprintSave.textContent = "Créer une nouvelle révision";
    renderBlueprintDiagnostics({
      valid: true,
      diagnostics: revision.diagnostics || [],
      canonical: revision.canonical || null,
      runtime_config: revision.runtime_config || null,
      source_sha256: revision.source_sha256 || null,
      canonical_sha256: revision.canonical_sha256 || null,
    });
    const revisions = await client.blueprintRevisions(selectedBlueprintId);
    renderBlueprintRevisions(revisions);
    const collection = await client.blueprints();
    renderBlueprintList(collection);
    blueprintStatus.textContent = "Blueprint et historique chargés depuis le Controller.";
  } catch (error) {
    if (error instanceof ControllerClientError && error.status === 401) {
      showSignedOut("Session expirée. Reconnectez-vous pour administrer les Blueprints.");
      return;
    }
    blueprintStatus.textContent = projectErrorMessage(error, "Lecture du Blueprint impossible.");
  } finally {
    setBlueprintBusy(false);
  }
}

async function refreshBlueprints() {
  if (!authenticated || blueprintLoading) {
    return;
  }
  const generation = ++blueprintGeneration;
  setBlueprintBusy(true);
  blueprintStatus.textContent = "Lecture des Blueprints…";
  try {
    const collection = await client.blueprints();
    if (generation !== blueprintGeneration || !authenticated) {
      return;
    }
    renderBlueprintList(collection);
    blueprintsLoaded = true;
    blueprintStatus.textContent = `${collection.items.length} Blueprint(s) reçu(s) du Controller.`;
    if (selectedBlueprintId && collection.items.some((item) => safeId(item, "") === selectedBlueprintId)) {
      setBlueprintBusy(false);
      await loadBlueprint(selectedBlueprintId);
    }
  } catch (error) {
    if (error instanceof ControllerClientError && error.status === 401) {
      showSignedOut("Session expirée. Reconnectez-vous pour administrer les Blueprints.");
      return;
    }
    blueprintStatus.textContent = projectErrorMessage(error, "Registre Blueprint indisponible.");
  } finally {
    if (generation === blueprintGeneration) {
      setBlueprintBusy(false);
    }
  }
}

async function loadBlueprintTemplate() {
  if (!authenticated || blueprintLoading) {
    return;
  }
  setBlueprintBusy(true);
  try {
    const template = await client.blueprintTemplate();
    selectedBlueprintId = "";
    selectedBlueprintEtag = "";
    selectedBlueprintRevision = 0;
    blueprintSource.value = typeof template.source === "string" ? template.source : "";
    blueprintEditorTitle.textContent = "Nouveau Blueprint";
    blueprintEditorMode.textContent = "nouveau";
    blueprintEditorMode.dataset.state = "draft";
    blueprintMeta.textContent = "Modèle officiel chargé. La création reste séparée de la validation.";
    blueprintSave.textContent = "Créer";
    blueprintDiagnostics.replaceChildren();
    appendEmpty(blueprintDiagnostics, "Validez la source avant de la créer.");
    blueprintPreview.textContent = "Aucune prévisualisation.";
    blueprintRevisions.replaceChildren();
    blueprintDiffFrom.replaceChildren();
    blueprintDiffTo.replaceChildren();
    blueprintRevisionCount.textContent = "0";
    blueprintDiff.textContent = "Aucune comparaison.";
    blueprintValidated = false;
    blueprintValidity.textContent = "non validé";
    blueprintValidity.dataset.state = "unknown";
    blueprintStatus.textContent = "Modèle Blueprint chargé depuis le Controller.";
  } catch (error) {
    blueprintStatus.textContent = projectErrorMessage(error, "Modèle Blueprint indisponible.");
  } finally {
    setBlueprintBusy(false);
  }
}

async function validateBlueprintEditor() {
  if (!authenticated || blueprintLoading) {
    return;
  }
  setBlueprintBusy(true);
  blueprintStatus.textContent = "Validation stricte du Blueprint…";
  try {
    const preview = await client.validateBlueprint(blueprintSource.value);
    renderBlueprintDiagnostics(preview);
    blueprintStatus.textContent = preview.valid
      ? "Blueprint valide. La configuration canonique et runtime est prévisualisée."
      : "Blueprint invalide. Corrigez les diagnostics avant persistance.";
  } catch (error) {
    blueprintValidated = false;
    blueprintStatus.textContent = projectErrorMessage(error, "Validation Blueprint impossible.");
  } finally {
    setBlueprintBusy(false);
  }
}

async function saveBlueprintEditor() {
  if (!authenticated || blueprintLoading) {
    return;
  }
  if (!blueprintValidated) {
    blueprintStatus.textContent = "Validez la source avant de la persister.";
    return;
  }
  setBlueprintBusy(true);
  blueprintStatus.textContent = selectedBlueprintId ? "Création d’une nouvelle révision…" : "Création du Blueprint…";
  try {
    const operation = selectedBlueprintId
      ? await client.updateBlueprint(selectedBlueprintId, selectedBlueprintEtag, blueprintSource.value)
      : await client.createBlueprint(blueprintSource.value);
    const target = operation && operation.result && typeof operation.result.sandbox_id === "string"
      ? operation.result.sandbox_id
      : selectedBlueprintId;
    blueprintStatus.textContent = `Opération ${safeText(operation.id, "acceptée", 96)} terminée.`;
    blueprintsLoaded = false;
    setBlueprintBusy(false);
    if (target) {
      await loadBlueprint(target);
    } else {
      await refreshBlueprints();
    }
  } catch (error) {
    if (error instanceof ControllerClientError && error.status === 401) {
      showSignedOut("Session expirée. Reconnectez-vous avant toute persistance Blueprint.");
      return;
    }
    blueprintStatus.textContent = projectErrorMessage(error, "Persistance Blueprint impossible.");
  } finally {
    setBlueprintBusy(false);
  }
}

async function loadHistoricalRevision(revision) {
  if (!selectedBlueprintId || blueprintLoading) {
    return;
  }
  setBlueprintBusy(true);
  try {
    const historical = await client.blueprintRevision(selectedBlueprintId, revision);
    blueprintSource.value = typeof historical.source === "string" ? historical.source : "";
    renderBlueprintDiagnostics({
      valid: true,
      diagnostics: historical.diagnostics || [],
      canonical: historical.canonical || null,
      runtime_config: historical.runtime_config || null,
      source_sha256: historical.source_sha256 || null,
      canonical_sha256: historical.canonical_sha256 || null,
    });
    blueprintMeta.textContent = `Révision historique ${revision} chargée en lecture. Enregistrer créera une nouvelle révision depuis l’ETag courant.`;
    blueprintStatus.textContent = `Révision ${revision} chargée.`;
  } catch (error) {
    blueprintStatus.textContent = projectErrorMessage(error, "Révision Blueprint indisponible.");
  } finally {
    setBlueprintBusy(false);
  }
}

async function compareBlueprintHistory() {
  if (!selectedBlueprintId || blueprintLoading) {
    return;
  }
  const fromRevision = Number(blueprintDiffFrom.value);
  const toRevision = Number(blueprintDiffTo.value);
  if (!Number.isInteger(fromRevision) || !Number.isInteger(toRevision) || fromRevision === toRevision) {
    blueprintStatus.textContent = "Choisissez deux révisions différentes.";
    return;
  }
  setBlueprintBusy(true);
  try {
    const comparison = await client.compareBlueprintRevisions(selectedBlueprintId, fromRevision, toRevision);
    blueprintDiff.textContent = JSON.stringify(comparison, null, 2);
    blueprintStatus.textContent = `${Array.isArray(comparison.changes) ? comparison.changes.length : 0} chemin(s) canonique(s) modifié(s).`;
  } catch (error) {
    blueprintStatus.textContent = projectErrorMessage(error, "Comparaison Blueprint impossible.");
  } finally {
    setBlueprintBusy(false);
  }
}

function clearObjectiveState() {
  objectiveGeneration += 1;
  objectiveLoading = false;
  objectivesLoaded = false;
  selectedObjectiveId = "";
  selectedObjective = null;
  objectiveRefresh.disabled = false;
  objectiveCreateSubmit.disabled = false;
  objectiveCount.textContent = "0";
  objectiveList.replaceChildren();
  objectiveCreateProject.replaceChildren();
  const option = document.createElement("option");
  option.value = "";
  option.textContent = "Sélectionnez un projet actif";
  objectiveCreateProject.append(option);
  objectiveDetailCard.hidden = true;
  objectiveCommandReason.value = "";
  objectiveOperation.textContent = "Aucune opération chargée.";
}

function setObjectiveBusy(busy) {
  objectiveLoading = busy;
  objectiveRefresh.disabled = busy;
  objectiveCreateSubmit.disabled = busy;
  objectiveCreateForm.querySelectorAll("input, select, textarea, button").forEach((control) => {
    control.disabled = busy;
  });
  objectiveCommandButtons.querySelectorAll("button").forEach((button) => {
    button.disabled = busy;
  });
}

function renderObjectiveProjectOptions(collection) {
  const previous = objectiveCreateProject.value;
  objectiveCreateProject.replaceChildren();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "Sélectionnez un projet actif";
  objectiveCreateProject.append(placeholder);
  for (const project of collection.items) {
    if (safeState(project) !== "enabled") {
      continue;
    }
    const identifier = safeId(project, "");
    if (!identifier) {
      continue;
    }
    const option = document.createElement("option");
    option.value = identifier;
    option.textContent = `${safeText(project.name, identifier, 120)} · ${identifier}`;
    objectiveCreateProject.append(option);
  }
  if ([...objectiveCreateProject.options].some((option) => option.value === previous)) {
    objectiveCreateProject.value = previous;
  }
}

function renderObjectiveList(collection) {
  objectiveList.replaceChildren();
  objectiveCount.textContent = String(collection.items.length);
  objectiveCoverage.textContent = collection.truncated
    ? "Une page suivante existe : la liste reste volontairement bornée."
    : "La première page reçue est complète selon les métadonnées Controller.";
  if (collection.items.length === 0) {
    appendEmpty(objectiveList, "Aucun objectif visible.");
    return;
  }
  for (const objective of collection.items) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    const heading = document.createElement("strong");
    const detail = document.createElement("span");
    const state = document.createElement("span");
    const identifier = safeId(objective, "objectif");
    const projects = Array.isArray(objective.project_ids)
      ? objective.project_ids.map((value) => safeText(value, "", 63)).filter(Boolean).join(", ")
      : "projet inconnu";
    button.type = "button";
    button.dataset.objectiveId = identifier;
    button.className = "objective-list-button";
    if (identifier === selectedObjectiveId) {
      button.setAttribute("aria-current", "true");
    }
    heading.textContent = safeText(objective.title, identifier, 200);
    detail.textContent = `${projects || "projet inconnu"} · priorité ${Number.isInteger(objective.priority) ? objective.priority : "?"}`;
    state.className = "state-badge";
    state.dataset.state = safeState(objective);
    state.textContent = safeState(objective);
    button.append(heading, detail, state);
    item.append(button);
    objectiveList.append(item);
  }
}

function appendObjectiveFact(label, value) {
  const wrapper = document.createElement("div");
  const term = document.createElement("dt");
  const description = document.createElement("dd");
  term.textContent = label;
  description.textContent = value;
  wrapper.append(term, description);
  objectiveDetailFacts.append(wrapper);
}

function renderObjectiveOperation(operation) {
  if (!operation || typeof operation !== "object" || Array.isArray(operation)) {
    objectiveOperation.textContent = "Aucune opération chargée.";
    return;
  }
  const target = operation.target && typeof operation.target === "object" ? operation.target : {};
  const summary = {
    id: safeText(operation.id, "inconnue", 96),
    kind: safeText(operation.kind, "inconnu", 80),
    state: safeText(operation.state, "inconnu", 40),
    target: {
      type: safeText(target.type, "inconnu", 40),
      id: safeText(target.id, "inconnu", 96),
    },
    created_at: safeText(operation.created_at, "non renseigné", 64),
    finished_at: operation.finished_at ? safeText(operation.finished_at, "non renseigné", 64) : null,
    result: operation.result && typeof operation.result === "object" ? operation.result : {},
  };
  objectiveOperation.textContent = JSON.stringify(summary, null, 2);
}

function renderObjectiveDetail(objective) {
  selectedObjective = objective;
  selectedObjectiveId = safeId(objective, "");
  objectiveDetailCard.hidden = false;
  objectiveDetailTitle.textContent = safeText(objective.title, selectedObjectiveId, 200);
  const state = safeState(objective);
  const rawState = safeText(objective.raw_state, "UNKNOWN", 40).toUpperCase();
  objectiveDetailState.dataset.state = state;
  objectiveDetailState.textContent = state;
  const projects = Array.isArray(objective.project_ids)
    ? objective.project_ids.map((value) => safeText(value, "", 63)).filter(Boolean).join(", ")
    : "non renseigné";
  objectiveDetailMeta.textContent = [
    selectedObjectiveId,
    `projet(s) ${projects || "non renseigné"}`,
    `priorité ${Number.isInteger(objective.priority) ? objective.priority : "?"}`,
    `révision ${Number.isInteger(objective.resource_revision) ? objective.resource_revision : "?"}`,
  ].join(" · ");
  objectiveDetailDescription.textContent = safeText(objective.description, "Description indisponible.", 16_384);
  objectiveDetailFacts.replaceChildren();
  appendObjectiveFact("État Controller", rawState.toLowerCase());
  appendObjectiveFact("Transition demandée", safeText(objective.requested_transition, "aucune", 64));
  appendObjectiveFact("Plan", safeText(objective.plan_id, "aucun", 96));
  appendObjectiveFact("Tentatives planning", String(Number.isInteger(objective.planning_attempt_count) ? objective.planning_attempt_count : 0));
  appendObjectiveFact("Tentatives totales", String(Number.isInteger(objective.attempt_count) ? objective.attempt_count : 0));
  appendObjectiveFact("Événements", String(Number.isInteger(objective.event_count) ? objective.event_count : 0));
  appendObjectiveFact("Pas avant", safeText(objective.not_before, "immédiat", 64));
  appendObjectiveFact("Parallélisme", String(Number.isInteger(objective.max_parallel_tasks) ? objective.max_parallel_tasks : 1));
  appendObjectiveFact("Erreur signalée", objective.has_error ? "oui" : "non");
  objectiveCommandReason.value = "";
  const allowed = {
    pause: ["QUEUED", "PLANNING", "RUNNING"].includes(rawState),
    resume: rawState === "PAUSED",
    cancel: ["QUEUED", "PLANNING", "RUNNING", "PAUSE_REQUESTED", "PAUSED"].includes(rawState),
  };
  objectiveCommandButtons.querySelectorAll("button[data-objective-command]").forEach((button) => {
    button.hidden = !allowed[button.dataset.objectiveCommand];
  });
}

async function followObjectiveOperation(operation) {
  renderObjectiveOperation(operation);
  const identifier = safeText(operation && operation.id, "", 96);
  if (!identifier) {
    return operation;
  }
  let current = await client.operation(identifier);
  renderObjectiveOperation(current);
  for (let attempt = 1; attempt < 3; attempt += 1) {
    const state = safeText(current && current.state, "", 40).toLowerCase();
    if (["succeeded", "failed", "cancelled"].includes(state)) {
      break;
    }
    await new Promise((resolve) => globalThis.setTimeout(resolve, 250));
    current = await client.operation(identifier);
    renderObjectiveOperation(current);
  }
  return current;
}

async function selectObjective(identifier) {
  if (!authenticated || objectiveLoading) {
    return;
  }
  setObjectiveBusy(true);
  objectiveStatus.textContent = `Lecture de l’objectif ${safeText(identifier, "sélectionné", 96)}…`;
  try {
    const objective = await client.objective(identifier);
    renderObjectiveDetail(objective);
    if (typeof objective.latest_operation_id === "string" && objective.latest_operation_id) {
      renderObjectiveOperation(await client.operation(objective.latest_operation_id));
    } else {
      renderObjectiveOperation(null);
    }
    const collection = await client.objectives();
    renderObjectiveList(collection);
    objectiveStatus.textContent = "Détail objectif chargé depuis le Controller.";
  } catch (error) {
    if (error instanceof ControllerClientError && error.status === 401) {
      showSignedOut("Session expirée. Reconnectez-vous pour administrer les objectifs.");
      return;
    }
    objectiveStatus.textContent = projectErrorMessage(error, "Lecture de l’objectif impossible.");
  } finally {
    setObjectiveBusy(false);
  }
}

async function refreshObjectives() {
  if (!authenticated || objectiveLoading) {
    return;
  }
  const generation = ++objectiveGeneration;
  setObjectiveBusy(true);
  objectiveStatus.textContent = "Lecture des objectifs et projets actifs…";
  try {
    const [objectiveResult, projectResult] = await Promise.allSettled([
      client.objectives(),
      client.projects(),
    ]);
    if (generation !== objectiveGeneration || !authenticated) {
      return;
    }
    if (objectiveResult.status !== "fulfilled") {
      throw objectiveResult.reason;
    }
    renderObjectiveList(objectiveResult.value);
    if (projectResult.status === "fulfilled") {
      renderObjectiveProjectOptions(projectResult.value);
    } else {
      renderObjectiveProjectOptions({ items: [], truncated: false });
    }
    objectivesLoaded = true;
    objectiveStatus.textContent = projectResult.status === "fulfilled"
      ? `${objectiveResult.value.items.length} objectif(s) reçu(s) du Controller.`
      : `${objectiveResult.value.items.length} objectif(s) reçu(s), mais la création est indisponible sans registre projet.`;
    if (selectedObjectiveId && objectiveResult.value.items.some((item) => safeId(item, "") === selectedObjectiveId)) {
      const objective = await client.objective(selectedObjectiveId);
      if (generation === objectiveGeneration && authenticated) {
        renderObjectiveDetail(objective);
      }
    } else {
      selectedObjectiveId = "";
      selectedObjective = null;
      objectiveDetailCard.hidden = true;
    }
  } catch (error) {
    if (error instanceof ControllerClientError && error.status === 401) {
      showSignedOut("Session expirée. Reconnectez-vous pour administrer les objectifs.");
      return;
    }
    objectiveStatus.textContent = projectErrorMessage(error, "Registre objectif indisponible.");
  } finally {
    if (generation === objectiveGeneration) {
      setObjectiveBusy(false);
    }
  }
}

async function runObjectiveMutation(label, operation) {
  if (!authenticated || objectiveLoading) {
    return;
  }
  setObjectiveBusy(true);
  objectiveStatus.textContent = `${label}…`;
  try {
    const accepted = await operation();
    const completed = await followObjectiveOperation(accepted);
    const state = safeText(completed && completed.state, "acceptée", 40);
    objectiveStatus.textContent = `${label} ${state} par le Controller.`;
    const target = completed && completed.target && typeof completed.target === "object"
      ? safeText(completed.target.id, "", 96)
      : "";
    objectivesLoaded = false;
    setObjectiveBusy(false);
    await refreshObjectives();
    if (target) {
      await selectObjective(target);
    }
  } catch (error) {
    if (error instanceof ControllerClientError && error.status === 401) {
      showSignedOut("Session expirée. Reconnectez-vous avant toute nouvelle commande.");
      return;
    }
    objectiveStatus.textContent = projectErrorMessage(error, `${label} impossible.`);
  } finally {
    setObjectiveBusy(false);
  }
}

function objectiveNotBefore(value) {
  if (!value) {
    return null;
  }
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) {
    throw new ControllerClientError("Date de lancement invalide.", {
      status: 400,
      code: "invalid_not_before",
    });
  }
  return timestamp.toISOString();
}

async function submitObjectiveCreate() {
  const constraints = document.getElementById("objective-create-constraints").value
    .split(/\r?\n/)
    .map((value) => value.trim())
    .filter((value, index, values) => value && values.indexOf(value) === index);
  const intent = {
    project_ids: [objectiveCreateProject.value],
    title: document.getElementById("objective-create-title-input").value.trim(),
    description: document.getElementById("objective-create-description").value.trim(),
    constraints,
    priority: Number(document.getElementById("objective-create-priority").value),
    not_before: objectiveNotBefore(document.getElementById("objective-create-not-before").value),
    max_parallel_tasks: Number(document.getElementById("objective-create-parallel").value),
    planning_max_attempts: Number(document.getElementById("objective-create-planning-attempts").value),
  };
  await runObjectiveMutation("Création de l’objectif", () => client.createObjective(intent));
  if (!objectiveLoading) {
    const project = objectiveCreateProject.value;
    objectiveCreateForm.reset();
    objectiveCreateProject.value = project;
    document.getElementById("objective-create-priority").value = "100";
    document.getElementById("objective-create-parallel").value = "1";
    document.getElementById("objective-create-planning-attempts").value = "3";
  }
}

async function refreshSession() {
  setConnection("checking", "Vérification…", "Lecture de la session auprès du Controller.");
  try {
    const session = await client.session();
    if (!session.authenticated) {
      showSignedOut();
      return;
    }
    const capabilities = await client.capabilities();
    showAuthenticated(session, capabilities);
  } catch (error) {
    if (error instanceof ControllerClientError && error.status === 401) {
      showSignedOut();
      return;
    }
    showUnavailable(error);
  }
}

document.addEventListener("click", (event) => {
  const link = event.target.closest("a[href]");
  if (!link || link.origin !== window.location.origin) {
    return;
  }
  const path = canonicalPath(link.pathname);
  if (!Object.hasOwn(routes, path)) {
    return;
  }
  event.preventDefault();
  window.history.pushState({}, "", path);
  render(path, true);
});

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  loginButton.disabled = true;
  sessionStatus.textContent = "Connexion en cours…";
  sessionDetail.textContent = "Le mot de passe est envoyé uniquement au Controller et n’est pas conservé par la Console.";
  try {
    await client.login("operator", passwordInput.value);
    passwordInput.value = "";
    await refreshSession();
  } catch (error) {
    passwordInput.value = "";
    if (error instanceof ControllerClientError && [401, 429].includes(error.status)) {
      showSignedOut("Connexion refusée. Vérifiez le mot de passe ou attendez avant une nouvelle tentative.");
    } else {
      showUnavailable(error);
    }
  } finally {
    loginButton.disabled = false;
  }
});

logoutButton.addEventListener("click", async () => {
  logoutButton.disabled = true;
  sessionStatus.textContent = "Déconnexion en cours…";
  try {
    await client.logout();
    await refreshSession();
  } catch (error) {
    showUnavailable(error);
  } finally {
    logoutButton.disabled = false;
  }
});

dashboardRefresh.addEventListener("click", () => {
  dashboardLoaded = false;
  void refreshDashboard();
});

projectRefresh.addEventListener("click", () => {
  projectsLoaded = false;
  void refreshProjects();
});

projectCreateMode.addEventListener("change", () => {
  const clone = projectCreateMode.value === "clone";
  projectCreateUrlLabel.hidden = !clone;
  projectCreateUrl.required = clone;
  if (!clone) {
    projectCreateUrl.value = "";
  }
});

projectCreateForm.addEventListener("submit", (event) => {
  event.preventDefault();
  void submitProjectCreate();
});

projectAdminList.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-project-id]");
  if (button) {
    void selectProject(button.dataset.projectId || "");
  }
});

projectUpdateForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!selectedProjectId || !selectedProjectEtag) {
    projectStatus.textContent = "Sélectionnez un projet avant de modifier ses métadonnées.";
    return;
  }
  const sandbox = document.getElementById("project-update-sandbox").value.trim();
  const changes = {
    name: document.getElementById("project-update-name").value.trim(),
    policy_id: document.getElementById("project-update-policy").value.trim(),
    sandbox_profile_id: sandbox || null,
  };
  void runProjectMutation(
    "Mise à jour du projet",
    () => client.updateProject(selectedProjectId, selectedProjectEtag, changes),
  );
});

projectCommandButtons.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-project-command]");
  if (!button || !selectedProjectId || !selectedProjectEtag) {
    return;
  }
  const command = button.dataset.projectCommand || "";
  if (command === "archive") {
    const accepted = globalThis.confirm(
      `Archiver ${selectedProjectId} désactive définitivement son usage opérationnel. Continuer ?`,
    );
    if (!accepted) {
      projectStatus.textContent = "Archivage annulé avant envoi au Controller.";
      return;
    }
  }
  const labels = {
    enable: "Activation du projet",
    disable: "Désactivation du projet",
    rescan: "Rescan du dépôt",
    archive: "Archivage du projet",
  };
  const reason = projectCommandReason.value.trim() || null;
  void runProjectMutation(
    labels[command] || "Commande projet",
    () => client.commandProject(selectedProjectId, command, selectedProjectEtag, reason),
  );
});

blueprintRefresh.addEventListener("click", () => {
  blueprintsLoaded = false;
  void refreshBlueprints();
});

blueprintNew.addEventListener("click", () => {
  void loadBlueprintTemplate();
});

blueprintList.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-blueprint-id]");
  if (button) {
    void loadBlueprint(button.dataset.blueprintId || "");
  }
});

blueprintRevisions.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-blueprint-revision]");
  if (button) {
    void loadHistoricalRevision(Number(button.dataset.blueprintRevision));
  }
});

blueprintValidate.addEventListener("click", () => {
  void validateBlueprintEditor();
});

blueprintSave.addEventListener("click", () => {
  void saveBlueprintEditor();
});

blueprintCompare.addEventListener("click", () => {
  void compareBlueprintHistory();
});

blueprintSource.addEventListener("input", () => {
  blueprintValidated = false;
  blueprintValidity.textContent = "à revalider";
  blueprintValidity.dataset.state = "unknown";
});

objectiveRefresh.addEventListener("click", () => {
  objectivesLoaded = false;
  void refreshObjectives();
});

objectiveCreateForm.addEventListener("submit", (event) => {
  event.preventDefault();
  void submitObjectiveCreate();
});

objectiveList.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-objective-id]");
  if (button) {
    void selectObjective(button.dataset.objectiveId || "");
  }
});

objectiveCommandButtons.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-objective-command]");
  if (!button || !selectedObjectiveId) {
    return;
  }
  const command = button.dataset.objectiveCommand || "";
  if (command === "cancel") {
    const accepted = globalThis.confirm(
      `Annuler ${selectedObjectiveId} arrête sa progression et annule les tâches non démarrées. Continuer ?`,
    );
    if (!accepted) {
      objectiveStatus.textContent = "Annulation abandonnée avant envoi au Controller.";
      return;
    }
  }
  const labels = {
    pause: "Mise en pause de l’objectif",
    resume: "Reprise de l’objectif",
    cancel: "Annulation de l’objectif",
  };
  const reason = objectiveCommandReason.value.trim() || null;
  void runObjectiveMutation(
    labels[command] || "Commande objectif",
    () => client.commandObjective(selectedObjectiveId, command, reason),
  );
});

window.addEventListener("popstate", () => render(window.location.pathname));
render(window.location.pathname);
refreshSession();
