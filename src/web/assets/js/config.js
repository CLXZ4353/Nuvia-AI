const DEFAULT_RUNTIME_CONFIG = Object.freeze({
  api_base_url: "/api",
  request_timeout_ms: 20_000,
  environment: "production",
  timezone: "UTC",
  features: {
    newsletter: false,
    telegram: false,
    push: false,
  },
});

const CONFIG_REQUEST_TIMEOUT_MS = 4_000;

function normalizeRuntimeConfig(value) {
  const payload = value && typeof value === "object" ? value : {};
  const timeout = Number(payload.request_timeout_ms);
  return {
    ...DEFAULT_RUNTIME_CONFIG,
    ...payload,
    api_base_url: String(payload.api_base_url || DEFAULT_RUNTIME_CONFIG.api_base_url).replace(/\/+$/, "") || "/api",
    request_timeout_ms: Number.isFinite(timeout)
      ? Math.max(1_000, Math.min(timeout, 120_000))
      : DEFAULT_RUNTIME_CONFIG.request_timeout_ms,
    features: {
      ...DEFAULT_RUNTIME_CONFIG.features,
      ...(payload.features && typeof payload.features === "object" ? payload.features : {}),
    },
  };
}

/** Load the public, deployment-specific values once before any API request. */
export async function loadRuntimeConfig() {
  const controller = new AbortController();
  const timeout = globalThis.setTimeout(() => controller.abort(), CONFIG_REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch("/api/config", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
    if (!response.ok) {
      return { ...DEFAULT_RUNTIME_CONFIG, unavailable: true };
    }
    return normalizeRuntimeConfig(await response.json());
  } catch {
    return { ...DEFAULT_RUNTIME_CONFIG, unavailable: true };
  } finally {
    globalThis.clearTimeout(timeout);
  }
}
