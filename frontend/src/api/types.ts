/** Shapes returned by the PlateletIQ API. */

export interface Facility {
  id: string;
  name: string;
  short_name: string;
  code: string;
  city: string;
  state: string;
  district: string | null;
  tier: string | null;
  address: string | null;
  pincode: string | null;
  phone: string | null;
  email: string | null;
  latitude: number | null;
  longitude: number | null;
  geo_source: string | null;
  active: boolean;
  stock?: Record<string, ComponentStock>;
  straight_line_km?: number | null;
}

export interface ComponentStock {
  available: number;
  reserved: number;
  expiring_24h: number;
}

export interface Counterparty extends Facility {
  straight_line_km: number | null;
  available_units: number;
  expiring_24h: number;
  component_type: string;
}

export interface SessionUser {
  sub: string;
  email: string | null;
  name: string | null;
  picture?: string | null;
  bank_id: string | null;
  bank_name: string | null;
  role: string | null;
}

export interface AuthConfig {
  google_client_id: string | null;
  google_enabled: boolean;
  dev_signin_enabled: boolean;
}

export type TransferStatus =
  | "REQUESTED"
  | "ACCEPTED"
  | "UNITS_RESERVED"
  | "SHIPMENT_CREATED"
  | "AWB_ASSIGNED"
  | "PICKUP_OTP_REQUIRED"
  | "IN_TRANSIT"
  | "ARRIVED"
  | "DELIVERY_OTP_REQUIRED"
  | "TRANSFER_COMPLETED"
  | "DECLINED"
  | "CANCELLED"
  | "FAILED";

export type ViewerRole = "SENDER" | "RECEIVER" | null;

export interface OtpState {
  issued: boolean;
  expires_at: string | null;
  verifier_bank_id: string | null;
}

export interface RouteGeometry {
  type: "LineString";
  coordinates: [number, number][]; // [lng, lat]
}

export interface Transfer {
  id: string;
  direction: "SHORTAGE_PULL" | "WASTAGE_PUSH";
  status: TransferStatus;
  revision: number;
  viewer_role: ViewerRole;
  units: number;
  component_type: string;
  blood_group: string | null;
  priority: string;
  reason: string | null;
  decline_reason: string | null;
  opened_by_bank_id: string;
  source: Facility;
  destination: Facility;
  distance_km: number | null;
  eta_minutes: number | null;
  eta_remaining_minutes: number | null;
  route_provider: string | null;
  route_geometry: RouteGeometry | null;
  transport_provider: string | null;
  transport_provider_label: string | null;
  awb_code: string | null;
  courier_name: string | null;
  rider: { name: string | null; mobile: string | null; vehicle: string | null } | null;
  instructions: string[];
  reserved_units: number;
  certificate?: { available: boolean; archived_in: string | null; sha256: string | null };
  pickup_otp: OtpState;
  delivery_otp: OtpState;
  created_at: string | null;
  accepted_at: string | null;
  dispatched_at: string | null;
  completed_at: string | null;
  updated_at: string | null;

  /** Only present in the response to the action that issued the code. */
  pickup_otp_code?: string;
  pickup_otp_expires_at?: string;
  delivery_otp_code?: string;
  delivery_otp_expires_at?: string;
  delivery_otp_verifier_bank_id?: string;
  settlement?: { units_transferred: number; source_bank_id: string; destination_bank_id: string };
}

export type LocationSource =
  | "courier_live"
  | "courier_last_known"
  | "route_projection"
  | "origin"
  | "destination"
  | "unavailable";

export interface TrackedLocation {
  lat: number | null;
  lng: number | null;
  location_source: LocationSource;
  progress: number | null;
  fix_age_seconds: number | null;
}

export interface TimelineEntry {
  status: string;
  title: string;
  description: string | null;
  actor_bank_id: string | null;
  location_name: string | null;
  source: string;
  occurred_at: string | null;
}

export interface TrackedTransfer extends Transfer {
  location: TrackedLocation;
  timeline: TimelineEntry[];
}

// ── Notifications ────────────────────────────────────────────────────────────

export type Severity = "info" | "success" | "warning" | "critical";

export interface NotificationDelivery {
  id: string;
  channel: "email" | "telegram" | "discord" | "realtime";
  target: string | null;
  status: "PENDING" | "SENT" | "FAILED" | "SKIPPED";
  provider: string | null;
  attempts: number;
  error: string | null;
  sent_at: string | null;
}

export interface NotificationItem {
  id: string;
  bank_id: string | null;
  event_type: string;
  severity: Severity;
  title: string;
  body: string | null;
  transfer_id: string | null;
  link: string | null;
  details: [string, string | number][];
  certificate_url: string | null;
  dispatch_via: "qstash" | "inline" | null;
  dispatch_status: string;
  created_at: string | null;
  read_at: string | null;
  deliveries?: NotificationDelivery[];
  /** Realtime-only test events are never stored. */
  ephemeral?: boolean;
}

export interface NotificationSettings {
  bank_id: string;
  email_enabled: boolean;
  telegram_enabled: boolean;
  digest_enabled: boolean;
  expiry_alerts_enabled: boolean;
  extra_emails: string[];
  email_recipients: string[];
  telegram_chats: { id: string; label: string | null }[];
  channels: {
    email: { available: boolean; providers: string[] };
    telegram: { available: boolean; bot: string | null };
    realtime: { available: boolean };
    discord: { available: boolean };
    queue: { available: boolean; mode: "qstash" | "inline" };
  };
}

export interface TelegramLinkInfo {
  id: string;
  code: string;
  deep_link: string;
  bot: string;
  expires_in_minutes: number;
  webhook: boolean;
}

// ── Cloud services ───────────────────────────────────────────────────────────

export type IntegrationStatus = "live" | "fallback" | "off" | "build" | "frontend";

export interface Integration {
  key: string;
  name: string;
  category: string;
  role: string;
  status: IntegrationStatus;
  detail: string;
  free_tier: string;
  aws_equivalent: string;
  testable: boolean;
  env: string[];
  dashboard: string | null;
}

export interface IntegrationsPayload {
  services: Integration[];
  summary: Record<string, number>;
  deliveries_24h: Record<string, Record<string, number>>;
  queue: { mode: string; dead_letter_queue: number | null };
  custody_stream: {
    enabled: boolean;
    published: number;
    failed: number;
    last_published_at: string | null;
    last_error: string | null;
  };
  email_chain: string[];
  environment: string;
  release: string;
}

export interface IntegrationTestResult {
  key: string;
  ok: boolean;
  elapsed_ms: number;
  message: string;
  id?: string;
  url?: string;
  latency_ms?: number;
}

export interface JobRun {
  id: string;
  job: string;
  trigger: string;
  scope: string;
  status: "RUNNING" | "SUCCEEDED" | "FAILED";
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  summary: Record<string, unknown> | null;
  error: string | null;
}

export interface JobsPayload {
  jobs: {
    name: string;
    cron_utc: string;
    description: string;
    heartbeat: boolean;
    scheduled_in_qstash: boolean;
    last_run: JobRun | null;
  }[];
  recent_runs: JobRun[];
  scheduler: string;
}

export interface ClientConfig {
  realtime: { enabled: boolean; key?: string; cluster?: string };
  telegram_bot: string | null;
  environment: string;
  release: string;
  status_page_url: string | null;
}
