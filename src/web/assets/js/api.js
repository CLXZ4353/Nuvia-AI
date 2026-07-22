export class ApiError extends Error {
  constructor(message, { status = 0, payload = null, timeout = false } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
    this.timeout = timeout;
  }
}

function apiUrl(baseUrl, path, query = undefined) {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const url = new URL(`${baseUrl}${normalizedPath}`, window.location.origin);
  if (query) {
    Object.entries(query).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, String(value));
    });
  }
  return url;
}

async function readPayload(response) {
  const type = response.headers.get("content-type") || "";
  if (!type.includes("application/json")) return null;
  try {
    return await response.json();
  } catch {
    return null;
  }
}

/** A single same-origin API client; UI components never construct endpoints. */
export class ApiClient {
  constructor(runtimeConfig, { onUnauthorized = () => {} } = {}) {
    this.baseUrl = String(runtimeConfig.api_base_url || "/api").replace(/\/+$/, "") || "/api";
    this.timeoutMs = Number(runtimeConfig.request_timeout_ms) || 20_000;
    this.onUnauthorized = onUnauthorized;
  }

  async request(path, { method = "GET", body, query, signal, timeoutMs = this.timeoutMs } = {}) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    try {
      const response = await fetch(apiUrl(this.baseUrl, path, query), {
        method,
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });
      const payload = await readPayload(response);
      if (response.status === 401) this.onUnauthorized(payload);
      if (!response.ok || payload?.ok === false) {
        throw new ApiError(payload?.message || "La richiesta non è riuscita.", {
          status: response.status,
          payload,
        });
      }
      return payload || {};
    } catch (error) {
      if (error?.name === "AbortError") {
        throw new ApiError("La richiesta ha richiesto troppo tempo. Riprova.", { timeout: true });
      }
      if (error instanceof ApiError) throw error;
      throw new ApiError("Non riesco a raggiungere il servizio. Verifica la connessione e riprova.");
    } finally {
      window.clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
  }

  me() { return this.request("/me"); }
  currentDigest() { return this.request("/digest/current"); }
  digest({ id = "", date = "" } = {}) { return this.request("/digest", { query: { id, date } }); }
  history({ q = "", offset = 0, limit = 12, signal } = {}) {
    return this.request("/digests", { query: { q, offset, limit }, signal });
  }
  saved() { return this.request("/saved"); }
  saveNews(newsId) { return this.request("/saved", { method: "POST", body: { news_id: newsId, action: "save" } }); }
  removeNews(newsId) { return this.request("/saved", { method: "POST", body: { news_id: newsId, action: "remove" } }); }
  settings() { return this.request("/settings"); }
  updateProfile(email) { return this.request("/settings/profile", { method: "POST", body: { email } }); }
  updatePreferences(preferences) { return this.request("/settings/preferences", { method: "POST", body: preferences }); }
  updateSuspension(body) { return this.request("/settings/suspension", { method: "POST", body }); }
  newsletter(email) { return this.request("/newsletter", { method: "POST", body: { email } }); }
  telegramOnboarding() { return this.request("/telegram/onboarding"); }
  firebaseConfig() { return this.request("/firebase/config"); }
  pushStatus(token) { return this.request("/push/status", { method: "POST", body: { token } }); }
  subscribePush(token) { return this.request("/push/subscribe", { method: "POST", body: { token } }); }
  unsubscribePush(token) { return this.request("/push/unsubscribe", { method: "POST", body: { token } }); }
  assistantChat({ message, context = {}, comparisonContext = null, history = [], attachment = null, responseFormat = "text" }) {
    return this.request("/assistant", {
      method: "POST",
      body: {
        message,
        context,
        history,
        response_format: responseFormat,
        ...(comparisonContext ? { comparison_context: comparisonContext } : {}),
        ...(attachment ? { attachment } : {}),
      },
      timeoutMs: Math.max(this.timeoutMs, 75_000),
    });
  }
  assistantSpeech(text) {
    return this.request("/assistant/speech", {
      method: "POST",
      body: { text },
      timeoutMs: Math.max(this.timeoutMs, 60_000),
    });
  }
  assistantTranscribe({ filename, mimeType, data }) {
    return this.request("/assistant/transcribe", {
      method: "POST",
      body: { audio: { filename, mimeType, data } },
      timeoutMs: Math.max(this.timeoutMs, 45_000),
    });
  }
  login(credentials) { return this.request("/auth/login", { method: "POST", body: credentials }); }
  register(details) { return this.request("/auth/register", { method: "POST", body: details }); }
  logout() { return this.request("/auth/logout", { method: "POST", body: {} }); }
}
