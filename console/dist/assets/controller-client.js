const ALLOWED_ENDPOINTS = Object.freeze({
  session: Object.freeze({ method: "GET", path: "/api/v1/auth/session" }),
  login: Object.freeze({ method: "POST", path: "/api/v1/auth/login" }),
  csrf: Object.freeze({ method: "POST", path: "/api/v1/auth/csrf" }),
  logout: Object.freeze({ method: "POST", path: "/api/v1/auth/logout" }),
  capabilities: Object.freeze({ method: "GET", path: "/api/v1/system/capabilities" }),
  projects: Object.freeze({ method: "GET", path: "/api/v1/projects" }),
  objectives: Object.freeze({ method: "GET", path: "/api/v1/objectives" }),
  reviews: Object.freeze({ method: "GET", path: "/api/v1/reviews" }),
  recoveries: Object.freeze({ method: "GET", path: "/api/v1/recoveries" }),
  plans: Object.freeze({ method: "GET", path: "/api/v1/plans" }),
  reviewerAssignments: Object.freeze({ method: "GET", path: "/api/v1/reviewer-assignments" }),
});

const PROJECT_ID_PATTERN = /^[a-z][a-z0-9-]{1,62}$/;
const SANDBOX_ID_PATTERN = /^sandbox-[0-9a-f]{32}$/;
const OBJECTIVE_ID_PATTERN = /^objective-[0-9a-f]{32}$/;
const OPERATION_ID_PATTERN = /^operation-[0-9a-f]{32}$/;
const PROJECT_COMMANDS = new Set(["enable", "disable", "rescan", "archive"]);
const OBJECTIVE_COMMANDS = new Set(["pause", "resume", "cancel"]);
const REQUEST_TIMEOUT_MS = 7000;
const MAX_ERROR_TEXT = 160;
const MAX_COLLECTION_ITEMS = 200;

export class ControllerClientError extends Error {
  constructor(message, { status = 0, code = "controller_unavailable", requestId = "" } = {}) {
    super(message);
    this.name = "ControllerClientError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

function idempotencyKey(label) {
  if (!globalThis.crypto || typeof globalThis.crypto.randomUUID !== "function") {
    throw new ControllerClientError("Le navigateur ne fournit pas une source aléatoire sûre.", {
      code: "secure_random_unavailable",
    });
  }
  return `console-${label}-${globalThis.crypto.randomUUID()}`;
}

function safeText(value, fallback) {
  if (typeof value !== "string") {
    return fallback;
  }
  const normalized = value.replace(/[\u0000-\u001f\u007f]/g, " ").trim();
  return normalized.slice(0, MAX_ERROR_TEXT) || fallback;
}

function projectId(value) {
  if (typeof value !== "string" || !PROJECT_ID_PATTERN.test(value)) {
    throw new ControllerClientError("Identifiant projet invalide.", {
      status: 400,
      code: "invalid_project_id",
    });
  }
  return value;
}

function sandboxId(value) {
  if (typeof value !== "string" || !SANDBOX_ID_PATTERN.test(value)) {
    throw new ControllerClientError("Identifiant Blueprint invalide.", {
      status: 400,
      code: "invalid_sandbox_id",
    });
  }
  return value;
}

function objectiveId(value) {
  if (typeof value !== "string" || !OBJECTIVE_ID_PATTERN.test(value)) {
    throw new ControllerClientError("Identifiant objectif invalide.", {
      status: 400,
      code: "invalid_objective_id",
    });
  }
  return value;
}

function operationId(value) {
  if (typeof value !== "string" || !OPERATION_ID_PATTERN.test(value)) {
    throw new ControllerClientError("Identifiant opération invalide.", {
      status: 400,
      code: "invalid_operation_id",
    });
  }
  return value;
}

function objectiveIntent(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ControllerClientError("Intention objectif invalide.", {
      status: 400,
      code: "invalid_objective",
    });
  }
  return value;
}

function revisionNumber(value) {
  if (!Number.isInteger(value) || value < 1 || value > Number.MAX_SAFE_INTEGER) {
    throw new ControllerClientError("Révision Blueprint invalide.", {
      status: 400,
      code: "invalid_blueprint_revision",
    });
  }
  return value;
}

function blueprintSource(value) {
  if (typeof value !== "string" || value.length === 0 || new TextEncoder().encode(value).length > 256 * 1024) {
    throw new ControllerClientError("Source Blueprint invalide ou trop volumineuse.", {
      status: 400,
      code: "invalid_blueprint_source",
    });
  }
  return value;
}

async function parsePayload(response) {
  const contentType = response.headers.get("content-type") || "";
  const mediaType = contentType.split(";", 1)[0].trim().toLowerCase();
  if (!["application/json", "application/problem+json"].includes(mediaType)) {
    throw new ControllerClientError("Réponse Controller invalide.", {
      status: response.status,
      code: "invalid_controller_response",
      requestId: response.headers.get("x-request-id") || "",
    });
  }
  try {
    const payload = await response.json();
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new TypeError("payload");
    }
    return payload;
  } catch (error) {
    if (error instanceof ControllerClientError) {
      throw error;
    }
    throw new ControllerClientError("Réponse Controller invalide.", {
      status: response.status,
      code: "invalid_controller_response",
      requestId: response.headers.get("x-request-id") || "",
    });
  }
}

async function request(endpoint, {
  body,
  csrfToken,
  idempotencyLabel,
  ifMatch,
  includeEtag = false,
} = {}) {
  const resolved = typeof endpoint === "string" ? ALLOWED_ENDPOINTS[endpoint] : endpoint;
  if (!resolved || !["GET", "POST", "PATCH"].includes(resolved.method)) {
    throw new ControllerClientError("Opération Controller non autorisée.", {
      code: "unsupported_controller_operation",
    });
  }

  const headers = new Headers({ Accept: "application/json" });
  let encodedBody;
  if (["POST", "PATCH"].includes(resolved.method)) {
    headers.set("Content-Type", "application/json");
    headers.set("Idempotency-Key", idempotencyKey(idempotencyLabel || "mutation"));
    encodedBody = JSON.stringify(body ?? {});
  }
  if (csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  if (ifMatch) {
    headers.set("If-Match", ifMatch);
  }

  const controller = new AbortController();
  const timeout = globalThis.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let response;
  try {
    response = await fetch(resolved.path, {
      method: resolved.method,
      headers,
      body: encodedBody,
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      referrerPolicy: "no-referrer",
      signal: controller.signal,
    });
  } catch (error) {
    const timeoutMessage = error && error.name === "AbortError"
      ? "Le Controller ne répond pas dans le délai prévu."
      : "Le Controller est indisponible.";
    throw new ControllerClientError(timeoutMessage, { code: "controller_unavailable" });
  } finally {
    globalThis.clearTimeout(timeout);
  }

  const payload = await parsePayload(response);
  if (!response.ok) {
    throw new ControllerClientError(
      safeText(payload.title, "La requête Controller a échoué."),
      {
        status: response.status,
        code: safeText(payload.code, "controller_request_failed"),
        requestId: safeText(payload.request_id, response.headers.get("x-request-id") || ""),
      },
    );
  }
  if (includeEtag) {
    return Object.freeze({ payload, etag: response.headers.get("etag") || "" });
  }
  return payload;
}

function dataObject(payload) {
  if (!payload.data || typeof payload.data !== "object" || Array.isArray(payload.data)) {
    throw new ControllerClientError("Réponse Controller incomplète.", {
      code: "invalid_controller_response",
    });
  }
  return payload.data;
}

function collection(payload) {
  if (!Array.isArray(payload.data) || payload.data.length > MAX_COLLECTION_ITEMS) {
    throw new ControllerClientError("Collection Controller invalide.", {
      code: "invalid_controller_response",
    });
  }
  const meta = payload.meta && typeof payload.meta === "object" && !Array.isArray(payload.meta)
    ? payload.meta
    : {};
  const nextCursor = typeof meta.next_cursor === "string" ? meta.next_cursor : "";
  return Object.freeze({
    items: Object.freeze(payload.data.slice()),
    truncated: nextCursor.length > 0,
  });
}

async function csrfToken() {
  const csrf = dataObject(await request("csrf", {
    body: {},
    idempotencyLabel: "csrf",
  }));
  if (typeof csrf.token !== "string" || !csrf.token.startsWith("csrf1.")) {
    throw new ControllerClientError("Jeton de sécurité Controller invalide.", {
      code: "invalid_csrf_response",
    });
  }
  return csrf.token;
}

export function createControllerClient() {
  return Object.freeze({
    async session() {
      return dataObject(await request("session"));
    },
    async login(username, password) {
      if (username !== "operator" || typeof password !== "string" || password.length === 0) {
        throw new ControllerClientError("Identifiants invalides.", {
          status: 400,
          code: "invalid_credentials",
        });
      }
      return dataObject(await request("login", {
        body: { username, password },
        idempotencyLabel: "login",
      }));
    },
    async capabilities() {
      return dataObject(await request("capabilities"));
    },

    async blueprints() {
      return collection(await request({ method: "GET", path: "/api/v1/blueprints" }));
    },
    async blueprintTemplate() {
      return dataObject(await request({ method: "GET", path: "/api/v1/blueprints/template" }));
    },
    async blueprint(identifier) {
      const result = await request({
        method: "GET",
        path: `/api/v1/blueprints/${sandboxId(identifier)}`,
      }, { includeEtag: true });
      return Object.freeze({ blueprint: dataObject(result.payload), etag: result.etag });
    },
    async blueprintRevisions(identifier) {
      return collection(await request({
        method: "GET",
        path: `/api/v1/blueprints/${sandboxId(identifier)}/revisions`,
      }));
    },
    async blueprintRevision(identifier, revision) {
      return dataObject(await request({
        method: "GET",
        path: `/api/v1/blueprints/${sandboxId(identifier)}/revisions/${revisionNumber(revision)}`,
      }));
    },
    async compareBlueprintRevisions(identifier, fromRevision, toRevision) {
      const from = revisionNumber(fromRevision);
      const to = revisionNumber(toRevision);
      return dataObject(await request({
        method: "GET",
        path: `/api/v1/blueprints/${sandboxId(identifier)}/diff?from=${from}&to=${to}`,
      }));
    },
    async validateBlueprint(source) {
      const csrf = await csrfToken();
      return dataObject(await request({ method: "POST", path: "/api/v1/blueprints/validate" }, {
        body: { source: blueprintSource(source) },
        csrfToken: csrf,
        idempotencyLabel: "blueprint-validate",
      }));
    },
    async createBlueprint(source) {
      const csrf = await csrfToken();
      return dataObject(await request({ method: "POST", path: "/api/v1/blueprints" }, {
        body: { source: blueprintSource(source) },
        csrfToken: csrf,
        idempotencyLabel: "blueprint-create",
      }));
    },
    async updateBlueprint(identifier, etag, source) {
      const csrf = await csrfToken();
      return dataObject(await request({
        method: "PATCH",
        path: `/api/v1/blueprints/${sandboxId(identifier)}`,
      }, {
        body: { source: blueprintSource(source) },
        csrfToken: csrf,
        ifMatch: etag,
        idempotencyLabel: "blueprint-update",
      }));
    },
    async projects() {
      return collection(await request("projects"));
    },
    async project(identifier) {
      const result = await request({
        method: "GET",
        path: `/api/v1/projects/${projectId(identifier)}`,
      }, { includeEtag: true });
      return Object.freeze({ project: dataObject(result.payload), etag: result.etag });
    },
    async createProject(intent) {
      const csrf = await csrfToken();
      return dataObject(await request({ method: "POST", path: "/api/v1/projects" }, {
        body: intent,
        csrfToken: csrf,
        idempotencyLabel: "project-create",
      }));
    },
    async updateProject(identifier, etag, changes) {
      const csrf = await csrfToken();
      return dataObject(await request({
        method: "PATCH",
        path: `/api/v1/projects/${projectId(identifier)}`,
      }, {
        body: changes,
        csrfToken: csrf,
        ifMatch: etag,
        idempotencyLabel: "project-update",
      }));
    },
    async commandProject(identifier, command, etag, reason = null) {
      if (!PROJECT_COMMANDS.has(command)) {
        throw new ControllerClientError("Commande projet non autorisée.", {
          status: 400,
          code: "unsupported_project_command",
        });
      }
      const csrf = await csrfToken();
      return dataObject(await request({
        method: "POST",
        path: `/api/v1/projects/${projectId(identifier)}/commands/${command}`,
      }, {
        body: { reason },
        csrfToken: csrf,
        ifMatch: etag,
        idempotencyLabel: `project-${command}`,
      }));
    },
    async objectives() {
      return collection(await request("objectives"));
    },
    async objective(identifier) {
      return dataObject(await request({
        method: "GET",
        path: `/api/v1/objectives/${objectiveId(identifier)}`,
      }));
    },
    async operation(identifier) {
      return dataObject(await request({
        method: "GET",
        path: `/api/v1/operations/${operationId(identifier)}`,
      }));
    },
    async createObjective(intent) {
      const csrf = await csrfToken();
      return dataObject(await request({ method: "POST", path: "/api/v1/objectives" }, {
        body: objectiveIntent(intent),
        csrfToken: csrf,
        idempotencyLabel: "objective-create",
      }));
    },
    async commandObjective(identifier, command, reason = null) {
      if (!OBJECTIVE_COMMANDS.has(command)) {
        throw new ControllerClientError("Commande objectif non autorisée.", {
          status: 400,
          code: "unsupported_objective_command",
        });
      }
      const csrf = await csrfToken();
      return dataObject(await request({
        method: "POST",
        path: `/api/v1/objectives/${objectiveId(identifier)}/commands/${command}`,
      }, {
        body: { reason },
        csrfToken: csrf,
        idempotencyLabel: `objective-${command}`,
      }));
    },
    async reviews() {
      return collection(await request("reviews"));
    },
    async recoveries() {
      return collection(await request("recoveries"));
    },
    async plans() {
      return collection(await request("plans"));
    },
    async reviewerAssignments() {
      return collection(await request("reviewerAssignments"));
    },
    async logout() {
      const csrf = await csrfToken();
      return dataObject(await request("logout", {
        body: {},
        csrfToken: csrf,
        idempotencyLabel: "logout",
      }));
    },
  });
}
