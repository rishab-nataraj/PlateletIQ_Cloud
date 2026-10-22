import { apiClient } from "./client";
import type {
  AuthConfig,
  ClientConfig,
  Counterparty,
  Facility,
  IntegrationTestResult,
  IntegrationsPayload,
  JobRun,
  JobsPayload,
  NotificationItem,
  NotificationSettings,
  SessionUser,
  TelegramLinkInfo,
  TrackedTransfer,
  Transfer,
} from "./types";

/**
 * The facility this session is signed in at.
 *
 * Set once by AuthContext when a facility is chosen. Bank-scoped endpoints read
 * it rather than a module constant, so the same build serves any facility and
 * two laptops running it behave as two different hospitals.
 */
let activeBankId: string | null = null;

export function setActiveBankId(bankId: string | null): void {
  activeBankId = bankId;
}

export function getActiveBankId(): string | null {
  return activeBankId;
}

function requireBankId(): string {
  if (!activeBankId) {
    throw new Error("No facility selected for this session.");
  }
  return activeBankId;
}

async function unwrap<T>(promise: Promise<{ data: { data: T } }>): Promise<T> {
  const response = await promise;
  return response.data.data;
}

/**
 * Same unwrap, but deliberately untyped.
 *
 * The forecast/inventory/analytics screens predate the typed API layer and
 * read these payloads structurally. Giving them a real model is worth doing,
 * but inventing one here would be a guess — so they keep the shape they have
 * always had until each screen is migrated.
 */
async function unwrapUntyped(promise: Promise<{ data: { data: unknown } }>): Promise<any> {
  const response = await promise;
  return response.data.data;
}

// ── Auth ─────────────────────────────────────────────────────────────────────

export const authApi = {
  getConfig: (): Promise<AuthConfig> => unwrap(apiClient.get("/auth/config")),

  getMe: (): Promise<SessionUser & { facility_required: boolean }> =>
    unwrap(apiClient.get("/auth/me")),

  /** Exchange a Google Identity Services credential for an identity token. */
  verifyGoogleCredential: (
    credential: string,
  ): Promise<{ token: string; user: SessionUser; facility_required: boolean }> =>
    unwrap(apiClient.post("/auth/google/verify", { credential })),

  startGoogleRedirect: (): Promise<{ redirect_url: string }> =>
    unwrap(apiClient.get("/auth/google/start")),

  /** Local sign-in; only works when the server sets ALLOW_DEV_SIGNIN=1. */
  devSignIn: (
    email: string,
    name?: string,
  ): Promise<{ token: string; user: SessionUser; facility_required: boolean }> =>
    unwrap(apiClient.post("/auth/dev-signin", { email, name })),

  selectFacility: (
    facilityId: string,
    role = "OFFICER",
  ): Promise<{ token: string; user: SessionUser; facility: Facility }> =>
    unwrap(apiClient.post("/auth/select-facility", { facility_id: facilityId, role })),

  getMemberships: (): Promise<{ role: string; facility: Facility }[]> =>
    unwrap(apiClient.get("/auth/memberships")),
};

// ── Facility registry ────────────────────────────────────────────────────────

export const facilityApi = {
  list: (options?: { includeStock?: boolean; near?: string }): Promise<Facility[]> =>
    unwrap(
      apiClient.get("/facilities", {
        params: { include_stock: options?.includeStock ?? true, near: options?.near },
      }),
    ),

  get: (facilityId: string): Promise<Facility> => unwrap(apiClient.get(`/facilities/${facilityId}`)),

  /** Facilities we could transfer with, nearest first, with their live stock. */
  counterparties: (componentType = "SDP"): Promise<Counterparty[]> =>
    unwrap(
      apiClient.get(`/facilities/${requireBankId()}/counterparties`, {
        params: { component_type: componentType },
      }),
    ),
};

// ── Transfers ────────────────────────────────────────────────────────────────

export interface CreateTransferPayload {
  counterparty_bank_id: string;
  direction: "SHORTAGE_PULL" | "WASTAGE_PUSH";
  units: number;
  component_type: string;
  blood_group?: string | null;
  priority?: string;
  reason?: string | null;
  provider?: string | null;
}

export const transferApi = {
  list: (scope: "all" | "incoming" | "outgoing" | "active" = "all"): Promise<Transfer[]> =>
    unwrap(apiClient.get("/transfers", { params: { scope } })),

  get: (id: string): Promise<Transfer> => unwrap(apiClient.get(`/transfers/${id}`)),

  track: (id: string): Promise<TrackedTransfer> => unwrap(apiClient.get(`/transfers/${id}/track`)),

  create: (payload: CreateTransferPayload): Promise<Transfer> =>
    unwrap(apiClient.post("/transfers", payload)),

  /**
   * Sender authorises. The response carries `pickup_otp_code` — the only time
   * that code is ever returned, and only to the facility that issued it.
   */
  accept: (id: string): Promise<Transfer> => unwrap(apiClient.post(`/transfers/${id}/accept`)),

  decline: (id: string, reason?: string): Promise<Transfer> =>
    unwrap(apiClient.post(`/transfers/${id}/decline`, { reason })),

  cancel: (id: string, reason?: string): Promise<Transfer> =>
    unwrap(apiClient.post(`/transfers/${id}/cancel`, { reason })),

  reissuePickupOtp: (id: string): Promise<{ transfer_id: string; otp_code: string; expires_at: string }> =>
    unwrap(apiClient.post(`/transfers/${id}/pickup/reissue`)),

  /** Sender issues a fresh receipt code when the first was lost off-screen. */
  reissueDeliveryOtp: (
    id: string,
  ): Promise<{ transfer_id: string; otp_code: string; expires_at: string; verifier_bank_id: string }> =>
    unwrap(apiClient.post(`/transfers/${id}/delivery/reissue`)),

  /** Sender confirms handover. Response carries the receipt code for the receiver. */
  verifyPickup: (id: string, otp: string): Promise<Transfer> =>
    unwrap(apiClient.post(`/transfers/${id}/pickup/verify`, { otp })),

  /** Receiver redeems the sender's receipt code. This settles both ledgers. */
  verifyDelivery: (id: string, otp: string): Promise<Transfer> =>
    unwrap(apiClient.post(`/transfers/${id}/delivery/verify`, { otp })),

  /**
   * The chain-of-custody certificate. Archived copies (S3 / Supabase) come back
   * as a short-lived link; otherwise the API renders the PDF on the spot.
   */
  certificate: async (id: string): Promise<string> => {
    const response = await apiClient.get(`/transfers/${id}/certificate`, { responseType: "blob" });
    const blob = response.data as Blob;
    if (blob.type === "application/pdf") return URL.createObjectURL(blob);
    const payload = JSON.parse(await blob.text()) as { data: { url: string } };
    return payload.data.url;
  },
};

// ── Notifications ────────────────────────────────────────────────────────────

export const notificationApi = {
  list: (unreadOnly = false): Promise<NotificationItem[]> =>
    unwrap(apiClient.get("/notifications", { params: { unread_only: unreadOnly, limit: 60 } })),
  unreadCount: (): Promise<{ unread: number }> => unwrap(apiClient.get("/notifications/unread-count")),
  markRead: (id: string) => unwrap(apiClient.post(`/notifications/${id}/read`)),
  markAllRead: () => unwrap(apiClient.post("/notifications/read-all")),
  getSettings: (): Promise<NotificationSettings> => unwrap(apiClient.get("/notifications/settings")),
  updateSettings: (patch: Partial<NotificationSettings>): Promise<NotificationSettings> =>
    unwrap(apiClient.put("/notifications/settings", patch)),
  sendTest: (): Promise<NotificationItem[]> => unwrap(apiClient.post("/notifications/test")),
  createTelegramLink: (): Promise<TelegramLinkInfo> => unwrap(apiClient.post("/notifications/telegram/link")),
  telegramLinkStatus: (id: string): Promise<{ id: string; status: string; chat?: string; warning?: string }> =>
    unwrap(apiClient.get(`/notifications/telegram/links/${id}`)),
  revokeTelegramLink: (id: string) => unwrap(apiClient.delete(`/notifications/telegram/links/${id}`)),
};

// ── Cloud services & jobs ────────────────────────────────────────────────────

export const systemApi = {
  clientConfig: (): Promise<ClientConfig> => unwrap(apiClient.get("/system/client-config")),
  integrations: (): Promise<IntegrationsPayload> => unwrap(apiClient.get("/system/integrations")),
  test: (key: string): Promise<IntegrationTestResult> => unwrap(apiClient.post(`/system/test/${key}`)),
};

export const jobsApi = {
  list: (): Promise<JobsPayload> => unwrap(apiClient.get("/jobs")),
  runMine: (name: string): Promise<JobRun> => unwrap(apiClient.post(`/jobs/${name}/run-mine`)),
};

// ── Bank-scoped operational data ─────────────────────────────────────────────

export const inventoryApi = {
  getSummary: () => unwrapUntyped(apiClient.get(`/banks/${requireBankId()}/inventory/summary`)),
  getUnits: (params?: Record<string, unknown>) =>
    unwrapUntyped(apiClient.get(`/banks/${requireBankId()}/inventory/units`, { params })),
  registerUnit: (payload: unknown) =>
    unwrapUntyped(apiClient.post(`/banks/${requireBankId()}/inventory/units`, payload)),
  issueUnit: (unitId: string, payload?: unknown) =>
    unwrapUntyped(apiClient.post(`/banks/${requireBankId()}/inventory/units/${unitId}/issue`, payload)),
  disposeUnit: (unitId: string, payload?: unknown) =>
    unwrapUntyped(apiClient.post(`/banks/${requireBankId()}/inventory/units/${unitId}/dispose`, payload)),
};

export const forecastApi = {
  getLatest: () => unwrapUntyped(apiClient.get(`/banks/${requireBankId()}/forecast/latest`)),
  triggerRun: () => unwrapUntyped(apiClient.post(`/banks/${requireBankId()}/forecast/runs`)),
  uploadHistory: (formData: FormData) =>
    unwrapUntyped(
      apiClient.post(`/banks/${requireBankId()}/forecast/upload-history`, formData, {
        headers: { "Content-Type": "multipart/form-data" },
      }),
    ),
};

export const recommendationApi = {
  getCurrent: () => unwrapUntyped(apiClient.get(`/banks/${requireBankId()}/recommendation/current`)),
  confirm: (id: string) =>
    unwrapUntyped(apiClient.post(`/banks/${requireBankId()}/recommendations/${id}/confirm`)),
  adjust: (id: string, payload: { quantity: number; reason: string }) =>
    unwrapUntyped(apiClient.post(`/banks/${requireBankId()}/recommendations/${id}/adjust`, payload)),
};

export const requisitionApi = {
  list: () => unwrapUntyped(apiClient.get(`/banks/${requireBankId()}/requisitions`)),
  create: (payload: unknown) => unwrapUntyped(apiClient.post(`/banks/${requireBankId()}/requisitions`, payload)),
  fulfill: (id: string, payload?: unknown) =>
    unwrapUntyped(apiClient.post(`/banks/${requireBankId()}/requisitions/${id}/fulfill`, payload)),
};

export const analyticsApi = {
  getForecastAnalytics: () => unwrapUntyped(apiClient.get(`/banks/${requireBankId()}/analytics/forecast`)),
  getWasteAnalytics: () => unwrapUntyped(apiClient.get(`/banks/${requireBankId()}/analytics/waste`)),
  getModelHealth: () => unwrapUntyped(apiClient.get(`/banks/${requireBankId()}/analytics/model-health`)),
};

export const campApi = {
  getSeasonal: () => unwrapUntyped(apiClient.get(`/banks/${requireBankId()}/camps/seasonal`)),
};

export const networkApi = {
  getNetwork: () => unwrapUntyped(apiClient.get("/network")),
  getRisks: () => unwrapUntyped(apiClient.get("/network/risks")),
};
