import { Fragment, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { jobsApi, systemApi } from "../api/endpoints";
import type { Integration, IntegrationStatus, IntegrationTestResult } from "../api/types";
import { timeAgo } from "./NotificationsScreen";

/**
 * Every cloud service the platform uses, with its live status computed by the
 * API right now — not a slide. Each card names the role it plays here, the free
 * tier it runs on and the AWS service that would do the same job, and most can
 * be exercised end to end with the Test button.
 */

const STATUS_STYLE: Record<IntegrationStatus, { label: string; bg: string; fg: string; dot: string }> = {
  live: { label: "Live", bg: "#E7F5EA", fg: "#1A7431", dot: "#1A8A2C" },
  fallback: { label: "Fallback", bg: "#FFF4E5", fg: "#9A5B00", dot: "#B25000" },
  off: { label: "Not configured", bg: "#F0F0F2", fg: "#6E6E73", dot: "#C7C7CC" },
  build: { label: "Build / deploy", bg: "#EEF0FF", fg: "#4A3FB3", dot: "#6E5FD8" },
  frontend: { label: "Browser", bg: "#E8F1FD", fg: "#0058B0", dot: "#0071E3" },
};

const CATEGORY_ORDER = [
  "Hosting & delivery",
  "Data & storage",
  "Messaging & scheduling",
  "Notifications",
  "Observability",
  "Security & identity",
  "AWS compliance stack",
  "DevOps",
  "Maps & logistics",
];

/** What only the browser can know: where it was served from, and its own SDKs. */
function withBrowserFacts(services: Integration[]): Integration[] {
  const onVercel = Boolean(import.meta.env.VITE_VERCEL_ENV) || window.location.hostname.endsWith(".vercel.app");
  const sentryBrowser = Boolean(import.meta.env.VITE_SENTRY_DSN);
  return services.map((service) => {
    if (service.key === "vercel") {
      return {
        ...service,
        status: onVercel ? "live" : "off",
        detail: onVercel
          ? `Served by Vercel (${import.meta.env.VITE_VERCEL_ENV ?? "production"}${import.meta.env.VITE_VERCEL_GIT_COMMIT_SHA ? ` @ ${import.meta.env.VITE_VERCEL_GIT_COMMIT_SHA.slice(0, 7)}` : ""})`
          : `Served from ${window.location.host} (not Vercel)`,
      };
    }
    if (service.key === "vercel_analytics") {
      return {
        ...service,
        status: onVercel ? "live" : "off",
        detail: onVercel ? "Analytics and Speed Insights scripts are loaded on this page." : "Only active on Vercel deployments.",
      };
    }
    if (service.key === "sentry") {
      return { ...service, detail: `${service.detail} · browser SDK ${sentryBrowser ? "on" : "off"}` };
    }
    return service;
  });
}

const LANES: { title: string; caption: string; nodes: [string, string][] }[] = [
  {
    title: "Request path",
    caption: "Every screen",
    nodes: [["vercel", "Vercel CDN"], ["google_oauth", "Google sign-in"], ["render", "Render · FastAPI"], ["upstash_redis", "Upstash Redis"], ["supabase_postgres", "Supabase Postgres"]],
  },
  {
    title: "Live updates",
    caption: "Other hospital acts",
    nodes: [["render", "API publishes"], ["pusher", "Pusher private channel"], ["vercel", "Both consoles refresh"]],
  },
  {
    title: "Notifications",
    caption: "Transfer steps, alerts",
    nodes: [["qstash", "QStash queue · retries · DLQ"], ["email_gmail", "Gmail API"], ["email_brevo", "Brevo"], ["email_resend", "Resend"], ["aws_ses", "SES"], ["telegram", "Telegram"], ["discord", "Discord ops"]],
  },
  {
    title: "Schedules",
    caption: "Hourly · 08:00 · 01:00 IST",
    nodes: [["qstash", "QStash cron"], ["render", "/jobs/*"], ["heartbeats", "Heartbeat monitor"]],
  },
  {
    title: "Compliance (AWS)",
    caption: "Every custody event",
    nodes: [["aws_sns", "SNS"], ["aws_sqs", "SQS + DLQ"], ["aws_lambda", "Lambda"], ["aws_dynamodb", "DynamoDB hash chain"], ["aws_eventbridge", "EventBridge → report"], ["aws_s3", "S3 certificates"]],
  },
  {
    title: "Watching it",
    caption: "Errors, logs, uptime",
    nodes: [["sentry", "Sentry"], ["betterstack_logs", "Better Stack Logs"], ["uptimerobot", "UptimeRobot + status page"], ["aws_cloudwatch", "CloudWatch alarms"], ["aws_budgets", "AWS Budgets"]],
  },
];

export default function CloudServicesScreen() {
  const queryClient = useQueryClient();
  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ["integrations"],
    queryFn: systemApi.integrations,
    refetchInterval: 30_000,
  });
  const { data: jobs } = useQuery({ queryKey: ["jobs"], queryFn: jobsApi.list, refetchInterval: 30_000 });
  const { data: config } = useQuery({ queryKey: ["client-config"], queryFn: systemApi.clientConfig, staleTime: Infinity });

  const services = useMemo(() => (data ? withBrowserFacts(data.services) : []), [data]);
  const byKey = useMemo(() => Object.fromEntries(services.map((s) => [s.key, s])), [services]);
  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const s of services) c[s.status] = (c[s.status] ?? 0) + 1;
    return c;
  }, [services]);

  const runJob = useMutation({
    mutationFn: (name: string) => jobsApi.runMine(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      queryClient.invalidateQueries({ queryKey: ["notifications"] });
    },
  });

  return (
    <div className="px-8 py-7 max-w-[1240px]">
      <header className="flex flex-wrap items-start justify-between gap-4 mb-6">
        <div>
          <h1 className="text-[26px] font-semibold text-[#1D1D1F] tracking-tight">Cloud services</h1>
          <p className="text-[14px] text-[#6E6E73] mt-1 max-w-[680px] leading-relaxed">
            {services.length} managed services, all on free tiers. Status is computed by the running API, so
            anything marked <b>Live</b> is configured and answering right now. Use <b>Test</b> to exercise one end to end.
          </p>
          <div className="flex flex-wrap gap-2 mt-3">
            {(["live", "fallback", "frontend", "build", "off"] as IntegrationStatus[]).map((status) =>
              counts[status] ? (
                <span key={status} className="text-[12px] font-semibold px-2.5 py-1 rounded-full" style={{ background: STATUS_STYLE[status].bg, color: STATUS_STYLE[status].fg }}>
                  {counts[status]} {STATUS_STYLE[status].label.toLowerCase()}
                </span>
              ) : null,
            )}
            {data && (
              <span className="text-[12px] px-2.5 py-1 rounded-full" style={{ background: "#F5F5F7", color: "#6E6E73" }}>
                {data.environment} · release {data.release.slice(0, 7)}
              </span>
            )}
          </div>
        </div>
        <div className="flex gap-2.5">
          {config?.status_page_url && (
            <a href={config.status_page_url} target="_blank" rel="noreferrer" className="px-4 py-2.5 text-[14px] font-medium rounded-full" style={{ background: "#F0F0F2", color: "#1D1D1F" }}>
              Public status page
            </a>
          )}
          <button onClick={() => refetch()} className="px-4 py-2.5 text-[14px] font-medium rounded-full cursor-pointer" style={{ background: "#1D1D1F", color: "white" }}>
            {isFetching ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </header>

      {isLoading && <p className="text-[14px] text-[#6E6E73]">Checking services…</p>}
      {error && <p className="text-[14px] text-[#B3261E]">{error instanceof Error ? error.message : "Could not load the catalogue."}</p>}

      {data && (
        <>
          {/* How the pieces connect */}
          <section className="bg-white rounded-[14px] p-5 mb-6" style={{ border: "1px solid #E5E5E7" }}>
            <p className="text-[11px] font-semibold text-[#AEAEB2] uppercase tracking-widest mb-4">How a request moves through the platform</p>
            <div className="flex flex-col gap-3">
              {LANES.map((lane) => (
                <div key={lane.title} className="flex items-center gap-4">
                  <div className="w-[150px] flex-shrink-0">
                    <p className="text-[13px] font-semibold text-[#1D1D1F]">{lane.title}</p>
                    <p className="text-[11px] text-[#AEAEB2]">{lane.caption}</p>
                  </div>
                  <div className="flex flex-wrap items-center gap-1.5">
                    {lane.nodes.map(([key, label], i) => {
                      const style = STATUS_STYLE[byKey[key]?.status ?? "off"];
                      return (
                        <Fragment key={`${key}-${i}`}>
                          {i > 0 && <span className="text-[#C7C7CC] text-[13px]">→</span>}
                          <span
                            className="flex items-center gap-1.5 text-[12px] font-medium px-2.5 py-1.5 rounded-[9px]"
                            style={{ background: style.bg, color: style.fg }}
                            title={byKey[key]?.detail}
                          >
                            <span className="w-1.5 h-1.5 rounded-full" style={{ background: style.dot }} />
                            {label}
                          </span>
                        </Fragment>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          </section>

          {/* Pipeline numbers */}
          <section className="grid gap-4 md:grid-cols-3 mb-6">
            <Stat
              title="Deliveries, last 24 h"
              value={Object.values(data.deliveries_24h).reduce((sum, s) => sum + (s.SENT ?? 0), 0)}
              caption={
                Object.entries(data.deliveries_24h)
                  .map(([channel, s]) => `${channel}: ${s.SENT ?? 0} sent${s.FAILED ? `, ${s.FAILED} failed` : ""}`)
                  .join(" · ") || "Nothing sent yet."
              }
            />
            <Stat
              title="Notification queue"
              value={data.queue.mode === "qstash" ? "QStash" : "Inline"}
              caption={
                data.queue.mode === "qstash"
                  ? `Dead-letter queue: ${data.queue.dead_letter_queue ?? "?"} message(s)`
                  : "Background worker; the hourly sweep redrives anything stuck."
              }
            />
            <Stat
              title="Custody events → AWS"
              value={data.custody_stream.enabled ? data.custody_stream.published : "Off"}
              caption={
                data.custody_stream.enabled
                  ? `${data.custody_stream.failed} failed since start${data.custody_stream.last_published_at ? ` · last ${timeAgo(data.custody_stream.last_published_at)}` : ""}`
                  : "Deploy the AWS stack to mirror every custody event into DynamoDB."
              }
            />
          </section>

          {/* Scheduled jobs */}
          {jobs && (
            <section className="bg-white rounded-[14px] p-5 mb-6" style={{ border: "1px solid #E5E5E7" }}>
              <div className="flex items-baseline justify-between mb-3">
                <p className="text-[11px] font-semibold text-[#AEAEB2] uppercase tracking-widest">Scheduled jobs</p>
                <p className="text-[12px] text-[#6E6E73]">Scheduler: {jobs.scheduler}</p>
              </div>
              <div className="grid gap-3 md:grid-cols-3">
                {jobs.jobs.map((job) => (
                  <div key={job.name} className="rounded-[12px] p-4" style={{ background: "#F5F5F7" }}>
                    <p className="text-[14px] font-semibold text-[#1D1D1F] font-mono">{job.name}</p>
                    <p className="text-[12px] text-[#6E6E73] mt-0.5">
                      {job.description} · <span className="font-mono">{job.cron_utc}</span> UTC
                    </p>
                    <p className="text-[12px] mt-2" style={{ color: job.scheduled_in_qstash ? "#1A7431" : "#9A5B00" }}>
                      {job.scheduled_in_qstash ? "✓ Scheduled in QStash" : "Not scheduled yet (scripts/setup_qstash_schedules.py)"}
                    </p>
                    <p className="text-[12px]" style={{ color: job.heartbeat ? "#1A7431" : "#AEAEB2" }}>
                      {job.heartbeat ? "✓ Heartbeat monitored" : "No heartbeat URL"}
                    </p>
                    {job.last_run && (
                      <p className="text-[12px] text-[#6E6E73] mt-1">
                        Last run {timeAgo(job.last_run.started_at)} by {job.last_run.trigger}:{" "}
                        <span style={{ color: job.last_run.status === "SUCCEEDED" ? "#1A7431" : "#B3261E" }}>
                          {job.last_run.status.toLowerCase()}
                        </span>
                      </p>
                    )}
                    <button
                      onClick={() => runJob.mutate(job.name)}
                      disabled={runJob.isPending}
                      className="mt-3 px-3 py-1.5 text-[12px] font-medium rounded-full cursor-pointer disabled:opacity-50"
                      style={{ background: "#1D1D1F", color: "white" }}
                    >
                      {runJob.isPending && runJob.variables === job.name ? "Running…" : "Run now for my facility"}
                    </button>
                  </div>
                ))}
              </div>
              {runJob.data && (
                <pre className="mt-3 text-[11px] leading-relaxed rounded-[10px] p-3 overflow-x-auto" style={{ background: "#1D1D1F", color: "#E5E5E7" }}>
                  {JSON.stringify(runJob.data.summary ?? runJob.data.error, null, 2)}
                </pre>
              )}
              {runJob.error && <p className="text-[12px] text-[#B3261E] mt-2">{runJob.error instanceof Error ? runJob.error.message : "Job failed."}</p>}
            </section>
          )}

          {/* The catalogue */}
          {CATEGORY_ORDER.map((category) => {
            const inCategory = services.filter((s) => s.category === category);
            if (!inCategory.length) return null;
            return (
              <section key={category} className="mb-6">
                <p className="text-[11px] font-semibold text-[#AEAEB2] uppercase tracking-widest mb-3">{category}</p>
                <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                  {inCategory.map((service) => (
                    <ServiceCard key={service.key} service={service} />
                  ))}
                </div>
              </section>
            );
          })}
        </>
      )}
    </div>
  );
}

function Stat({ title, value, caption }: { title: string; value: string | number; caption: string }) {
  return (
    <div className="bg-white rounded-[14px] p-5" style={{ border: "1px solid #E5E5E7" }}>
      <p className="text-[11px] font-semibold text-[#AEAEB2] uppercase tracking-widest">{title}</p>
      <p className="text-[26px] font-semibold text-[#1D1D1F] mt-1">{value}</p>
      <p className="text-[12px] text-[#6E6E73] mt-1 leading-relaxed">{caption}</p>
    </div>
  );
}

function ServiceCard({ service }: { service: Integration }) {
  const style = STATUS_STYLE[service.status];
  const [result, setResult] = useState<IntegrationTestResult | null>(null);
  const [testError, setTestError] = useState<string | null>(null);
  const [showEnv, setShowEnv] = useState(false);

  const test = useMutation({
    mutationFn: () => systemApi.test(service.key),
    onMutate: () => {
      setResult(null);
      setTestError(null);
    },
    onSuccess: setResult,
    onError: (err) => setTestError(err instanceof Error ? err.message : "Test failed."),
  });

  return (
    <article className="bg-white rounded-[14px] p-4 flex flex-col" style={{ border: "1px solid #E5E5E7" }}>
      <div className="flex items-start justify-between gap-3">
        <p className="text-[14px] font-semibold text-[#1D1D1F] leading-snug">{service.name}</p>
        <span className="flex-shrink-0 flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full" style={{ background: style.bg, color: style.fg }}>
          <span className="w-1.5 h-1.5 rounded-full" style={{ background: style.dot }} />
          {style.label}
        </span>
      </div>
      <p className="text-[12px] text-[#1D1D1F] mt-1.5 leading-relaxed">{service.role}</p>
      <p className="text-[12px] text-[#6E6E73] mt-1 leading-relaxed">{service.detail}</p>

      <dl className="mt-3 pt-3 grid grid-cols-[86px_1fr] gap-y-1 text-[11px]" style={{ borderTop: "1px solid #F0F0F0" }}>
        <dt className="text-[#AEAEB2]">Free tier</dt>
        <dd className="text-[#6E6E73]">{service.free_tier}</dd>
        <dt className="text-[#AEAEB2]">AWS equivalent</dt>
        <dd className="text-[#6E6E73]">{service.aws_equivalent}</dd>
      </dl>

      {showEnv && service.env.length > 0 && (
        <p className="mt-2 text-[11px] font-mono text-[#6E6E73] break-all">{service.env.join("  ")}</p>
      )}

      <div className="mt-auto pt-3 flex flex-wrap items-center gap-2">
        {service.testable && (
          <button
            onClick={() => test.mutate()}
            disabled={test.isPending || service.status === "off"}
            title={service.status === "off" ? "Configure this service first (see Env vars)" : undefined}
            className="px-3 py-1.5 text-[12px] font-medium rounded-full cursor-pointer disabled:opacity-50"
            style={{ background: "#0071E3", color: "white" }}
          >
            {test.isPending ? "Testing…" : "Test"}
          </button>
        )}
        {service.dashboard && (
          <a href={service.dashboard} target="_blank" rel="noreferrer" className="px-3 py-1.5 text-[12px] font-medium rounded-full" style={{ background: "#F0F0F2", color: "#1D1D1F" }}>
            Dashboard
          </a>
        )}
        {service.env.length > 0 && (
          <button onClick={() => setShowEnv((v) => !v)} className="text-[11px] text-[#AEAEB2] cursor-pointer ml-auto">
            {showEnv ? "Hide" : "Env vars"}
          </button>
        )}
      </div>

      {result && (
        <p className="mt-2 text-[12px] leading-relaxed" style={{ color: result.ok ? "#1A7431" : "#B3261E" }}>
          {result.ok ? "✓" : "✕"} {result.message} <span className="text-[#AEAEB2]">({result.elapsed_ms} ms)</span>
          {result.url && (
            <>
              {" "}
              <a href={result.url} target="_blank" rel="noreferrer" className="underline">open</a>
            </>
          )}
        </p>
      )}
      {testError && <p className="mt-2 text-[12px] text-[#B3261E] leading-relaxed">✕ {testError}</p>}
    </article>
  );
}
