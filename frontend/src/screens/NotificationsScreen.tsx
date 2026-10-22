import { useEffect, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import QRCode from "qrcode";
import { notificationApi } from "../api/endpoints";
import type { NotificationDelivery, NotificationItem, NotificationSettings, TelegramLinkInfo } from "../api/types";
import { useAuth } from "../contexts/AuthContext";
import { useLive } from "../contexts/LiveContext";
import { SEVERITY_COLOURS } from "../components/LiveWidgets";

const CHANNEL_LABELS: Record<string, string> = {
  email: "Email",
  telegram: "Telegram",
  discord: "Discord ops",
  realtime: "Live push",
};

const PROVIDER_LABELS: Record<string, string> = {
  gmail: "Gmail API",
  brevo: "Brevo",
  resend: "Resend",
  ses: "Amazon SES",
  pusher: "Pusher",
  telegram: "Bot API",
  discord: "Webhook",
};

export function timeAgo(iso: string | null): string {
  if (!iso) return "";
  const seconds = Math.max(0, (Date.now() - new Date(iso.endsWith("Z") ? iso : `${iso}Z`).getTime()) / 1000);
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}

export default function NotificationsScreen({ onOpenTransfer }: { onOpenTransfer: (id: string) => void }) {
  const { user } = useAuth();
  const bankId = user?.bank_id ?? null;
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<"all" | "unread">("all");
  const [banner, setBanner] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  const { data: items, isLoading, error } = useQuery({
    queryKey: ["notifications", "inbox", bankId, filter],
    queryFn: () => notificationApi.list(filter === "unread"),
    enabled: Boolean(bankId),
    refetchInterval: 10_000,
  });

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["notifications"] });

  const markRead = useMutation({ mutationFn: (id: string) => notificationApi.markRead(id), onSuccess: refresh });
  const markAll = useMutation({ mutationFn: notificationApi.markAllRead, onSuccess: refresh });
  const sendTest = useMutation({
    mutationFn: notificationApi.sendTest,
    onSuccess: (created) => {
      refresh();
      const channels = new Set((created[0]?.deliveries ?? []).map((d) => CHANNEL_LABELS[d.channel] ?? d.channel));
      setBanner({
        tone: "ok",
        text: channels.size
          ? `Test sent through ${[...channels].join(", ")}. Delivery results appear below within a few seconds.`
          : "Test recorded in the inbox. No external channel is configured yet — see Cloud services.",
      });
    },
    onError: (err) => setBanner({ tone: "error", text: err instanceof Error ? err.message : "Test failed." }),
  });

  return (
    <div className="px-8 py-7 max-w-[1180px]">
      <header className="flex flex-wrap items-start justify-between gap-4 mb-6">
        <div>
          <h1 className="text-[26px] font-semibold text-[#1D1D1F] tracking-tight">Notifications</h1>
          <p className="text-[14px] text-[#6E6E73] mt-1 max-w-[620px] leading-relaxed">
            Every alert for {user?.bank_name}, and exactly how each one left the building: email, Telegram,
            the Discord ops channel and the live push. Custody codes are never sent through any of them.
          </p>
        </div>
        <div className="flex gap-2.5">
          <button
            onClick={() => sendTest.mutate()}
            disabled={sendTest.isPending}
            className="px-4 py-2.5 text-white text-[14px] font-medium rounded-full cursor-pointer disabled:opacity-50"
            style={{ background: "#0071E3" }}
          >
            {sendTest.isPending ? "Sending…" : "Send test notification"}
          </button>
          <button
            onClick={() => markAll.mutate()}
            className="px-4 py-2.5 text-[14px] font-medium rounded-full cursor-pointer"
            style={{ background: "#F0F0F2", color: "#1D1D1F" }}
          >
            Mark all read
          </button>
        </div>
      </header>

      {banner && (
        <div
          className="rounded-[12px] px-4 py-3 mb-6 flex items-start justify-between gap-4"
          style={banner.tone === "ok" ? { background: "#E7F5EA", border: "1px solid #B7E0C1" } : { background: "#FDECEC", border: "1px solid #F5C6C6" }}
        >
          <p className="text-[13px]" style={{ color: banner.tone === "ok" ? "#1A7431" : "#B3261E" }}>{banner.text}</p>
          <button onClick={() => setBanner(null)} className="text-[13px] opacity-60 cursor-pointer">Dismiss</button>
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-[1fr_360px]">
        <section>
          <div className="flex items-center gap-2 mb-3">
            {(["all", "unread"] as const).map((value) => (
              <button
                key={value}
                onClick={() => setFilter(value)}
                className="px-3.5 py-1.5 rounded-full text-[13px] font-medium cursor-pointer"
                style={filter === value ? { background: "#1D1D1F", color: "white" } : { background: "#F0F0F2", color: "#6E6E73" }}
              >
                {value === "all" ? "All" : "Unread"}
              </button>
            ))}
          </div>

          {isLoading && <p className="text-[14px] text-[#6E6E73]">Loading…</p>}
          {error && <p className="text-[14px] text-[#B3261E]">{error instanceof Error ? error.message : "Could not load notifications."}</p>}
          {items && items.length === 0 && (
            <div className="bg-white rounded-[14px] p-8 text-center" style={{ border: "1px solid #E5E5E7" }}>
              <p className="text-[14px] text-[#6E6E73]">Nothing here yet. Open a transfer, or send a test notification.</p>
            </div>
          )}

          {items?.map((item) => (
            <InboxItem
              key={item.id}
              item={item}
              onRead={() => !item.read_at && markRead.mutate(item.id)}
              onOpenTransfer={onOpenTransfer}
            />
          ))}
        </section>

        <aside className="flex flex-col gap-4">
          <ChannelSettings />
          <TelegramCard />
          <BrowserCard />
        </aside>
      </div>
    </div>
  );
}

// ── Inbox ────────────────────────────────────────────────────────────────────

function InboxItem({
  item,
  onRead,
  onOpenTransfer,
}: {
  item: NotificationItem;
  onRead: () => void;
  onOpenTransfer: (id: string) => void;
}) {
  const tone = SEVERITY_COLOURS[item.severity] ?? SEVERITY_COLOURS.info;
  const unread = !item.read_at;
  return (
    <article
      onClick={onRead}
      className="bg-white rounded-[14px] p-5 mb-3 cursor-default"
      style={{ border: unread ? `1px solid ${tone.fg}40` : "1px solid #E5E5E7" }}
    >
      <div className="flex items-start gap-3">
        <span className="w-2.5 h-2.5 rounded-full mt-1.5 flex-shrink-0" style={{ background: unread ? tone.fg : "#D2D2D7" }} />
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <p className="text-[15px] font-semibold text-[#1D1D1F] leading-snug">{item.title}</p>
            <span className="text-[12px] text-[#AEAEB2]">{timeAgo(item.created_at)}</span>
          </div>
          {item.body && <p className="text-[13px] text-[#6E6E73] mt-1 leading-relaxed">{item.body}</p>}

          {item.details.length > 0 && (
            <div className="flex flex-wrap gap-x-5 gap-y-1 mt-2.5">
              {item.details.map(([label, value]) => (
                <span key={label} className="text-[12px] text-[#6E6E73]">
                  {label}: <span className="text-[#1D1D1F] font-medium">{String(value)}</span>
                </span>
              ))}
            </div>
          )}

          <div className="flex flex-wrap items-center gap-2 mt-3">
            <span
              className="text-[11px] font-semibold px-2 py-0.5 rounded-full"
              style={{ background: "#F5F5F7", color: "#6E6E73" }}
              title="How the slow channels were dispatched"
            >
              {item.dispatch_via === "qstash" ? "via QStash queue" : item.dispatch_via === "inline" ? "delivered inline" : "in-app"}
              {" · "}
              {item.dispatch_status.replace("_", " ").toLowerCase()}
            </span>
            {(item.deliveries ?? []).map((d) => (
              <DeliveryChip key={d.id} delivery={d} />
            ))}
          </div>

          {(item.transfer_id || item.certificate_url) && (
            <div className="flex gap-2 mt-3">
              {item.transfer_id && (
                <button
                  onClick={() => onOpenTransfer(item.transfer_id!)}
                  className="text-[12px] font-medium px-3 py-1.5 rounded-full cursor-pointer"
                  style={{ background: "#F0F0F2", color: "#1D1D1F" }}
                >
                  Open {item.transfer_id}
                </button>
              )}
              {item.certificate_url && (
                <a
                  href={item.certificate_url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-[12px] font-medium px-3 py-1.5 rounded-full"
                  style={{ background: "#E7F5EA", color: "#1A7431" }}
                >
                  Custody certificate (PDF)
                </a>
              )}
            </div>
          )}
        </div>
      </div>
    </article>
  );
}

function DeliveryChip({ delivery }: { delivery: NotificationDelivery }) {
  const ok = delivery.status === "SENT";
  const failed = delivery.status === "FAILED";
  const colours = ok
    ? { bg: "#E7F5EA", fg: "#1A7431" }
    : failed
      ? { bg: "#FDECEC", fg: "#B3261E" }
      : { bg: "#FFF4E5", fg: "#9A5B00" };
  const provider = delivery.provider ? PROVIDER_LABELS[delivery.provider] ?? delivery.provider : null;
  const title = [delivery.target, delivery.error, delivery.attempts ? `${delivery.attempts} attempt(s)` : null]
    .filter(Boolean)
    .join("\n");
  return (
    <span className="text-[11px] font-semibold px-2 py-0.5 rounded-full" style={{ background: colours.bg, color: colours.fg }} title={title}>
      {ok ? "✓" : failed ? "✕" : "…"} {CHANNEL_LABELS[delivery.channel] ?? delivery.channel}
      {delivery.channel === "email" && delivery.target ? ` ${delivery.target}` : ""}
      {provider && delivery.channel !== "realtime" ? ` · ${provider}` : ""}
      {ok && delivery.error ? " (failover)" : ""}
    </span>
  );
}

// ── Side panels ──────────────────────────────────────────────────────────────

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="bg-white rounded-[14px] p-5" style={{ border: "1px solid #E5E5E7" }}>
      <p className="text-[11px] font-semibold text-[#AEAEB2] uppercase tracking-widest mb-3">{title}</p>
      {children}
    </div>
  );
}

function Toggle({ label, hint, checked, disabled, onChange }: {
  label: string; hint?: string; checked: boolean; disabled?: boolean; onChange: (value: boolean) => void;
}) {
  return (
    <label className={`flex items-start justify-between gap-3 py-2 ${disabled ? "opacity-50" : "cursor-pointer"}`}>
      <span>
        <span className="block text-[13px] font-medium text-[#1D1D1F]">{label}</span>
        {hint && <span className="block text-[12px] text-[#6E6E73] mt-0.5 leading-snug">{hint}</span>}
      </span>
      <input
        type="checkbox"
        className="mt-1 w-4 h-4 accent-[#0071E3]"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
    </label>
  );
}

function ChannelSettings() {
  const queryClient = useQueryClient();
  const { data: settings } = useQuery({ queryKey: ["notification-settings"], queryFn: notificationApi.getSettings });
  const [extra, setExtra] = useState("");
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    if (settings) setExtra(settings.extra_emails.join(", "));
  }, [settings]);

  const update = useMutation({
    mutationFn: (patch: Partial<NotificationSettings>) => notificationApi.updateSettings(patch),
    onSuccess: (next) => {
      queryClient.setQueryData(["notification-settings"], next);
      setMessage("Saved.");
    },
    onError: (err) => setMessage(err instanceof Error ? err.message : "Could not save."),
  });

  if (!settings) return <Panel title="Channels"><p className="text-[13px] text-[#6E6E73]">Loading…</p></Panel>;
  const ch = settings.channels;

  return (
    <Panel title="Channels for this facility">
      <Toggle
        label="Email"
        hint={ch.email.available ? `Providers in order: ${ch.email.providers.join(" → ")}` : "No email provider configured on the API."}
        checked={settings.email_enabled && ch.email.available}
        disabled={!ch.email.available}
        onChange={(value) => update.mutate({ email_enabled: value })}
      />
      <Toggle
        label="Telegram"
        hint={ch.telegram.available ? `${settings.telegram_chats.length} linked chat(s)` : "No bot configured on the API."}
        checked={settings.telegram_enabled && ch.telegram.available}
        disabled={!ch.telegram.available}
        onChange={(value) => update.mutate({ telegram_enabled: value })}
      />
      <Toggle
        label="Daily digest (08:00 IST)"
        hint="Stock, expiring units, transfers in progress and the 7-day forecast."
        checked={settings.digest_enabled}
        onChange={(value) => update.mutate({ digest_enabled: value })}
      />
      <Toggle
        label="Expiry alerts"
        hint="Once a day when units will expire within 24 hours."
        checked={settings.expiry_alerts_enabled}
        onChange={(value) => update.mutate({ expiry_alerts_enabled: value })}
      />

      <div className="mt-3 pt-3" style={{ borderTop: "1px solid #F0F0F0" }}>
        <p className="text-[12px] text-[#6E6E73] leading-relaxed">
          Email goes to everyone signed in at this facility
          {settings.email_recipients.length ? ` (${settings.email_recipients.join(", ")})` : ""}, plus:
        </p>
        <input
          value={extra}
          onChange={(e) => setExtra(e.target.value)}
          placeholder="extra@hospital.org, duty.officer@gmail.com"
          className="w-full mt-2 px-3 py-2 text-[13px] rounded-[10px] outline-none"
          style={{ border: "1px solid #E5E5E7" }}
        />
        <button
          onClick={() => update.mutate({ extra_emails: extra.split(",").map((s) => s.trim()).filter(Boolean) })}
          className="mt-2 px-3.5 py-1.5 text-[12px] font-medium rounded-full cursor-pointer"
          style={{ background: "#1D1D1F", color: "white" }}
        >
          Save recipients
        </button>
        {message && <p className="text-[12px] text-[#6E6E73] mt-2">{message}</p>}
      </div>

      <p className="text-[12px] text-[#AEAEB2] mt-3 leading-relaxed">
        Slow channels are {ch.queue.mode === "qstash" ? "queued through Upstash QStash with retries and a dead-letter queue" : "delivered in a background worker (QStash not configured)"}.
        {ch.discord.available ? " Every transfer also reaches the Discord ops channel." : ""}
      </p>
    </Panel>
  );
}

function TelegramCard() {
  const queryClient = useQueryClient();
  const { data: settings } = useQuery({ queryKey: ["notification-settings"], queryFn: notificationApi.getSettings });
  const [link, setLink] = useState<TelegramLinkInfo | null>(null);
  const [qr, setQr] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: notificationApi.createTelegramLink,
    onSuccess: async (info) => {
      setLink(info);
      setStatus("PENDING");
      setError(null);
      setQr(await QRCode.toDataURL(info.deep_link, { margin: 1, width: 220, color: { dark: "#1D1D1F" } }));
    },
    onError: (err) => setError(err instanceof Error ? err.message : "Could not create a link."),
  });

  const revoke = useMutation({
    mutationFn: (id: string) => notificationApi.revokeTelegramLink(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notification-settings"] }),
  });

  // Wait for the chat to press Start (webhook, or polling fallback on the API).
  useEffect(() => {
    if (!link || status !== "PENDING") return;
    const timer = window.setInterval(async () => {
      try {
        const result = await notificationApi.telegramLinkStatus(link.id);
        if (result.status !== "PENDING") {
          setStatus(result.status);
          queryClient.invalidateQueries({ queryKey: ["notification-settings"] });
        }
      } catch {
        /* keep waiting */
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [link, status, queryClient]);

  if (!settings) return null;
  const available = settings.channels.telegram.available;

  return (
    <Panel title="Telegram">
      {!available && (
        <p className="text-[13px] text-[#6E6E73] leading-relaxed">
          Set <code className="text-[12px]">TELEGRAM_BOT_TOKEN</code> on the API (create a bot with @BotFather) to
          send alerts to phones.
        </p>
      )}

      {available && settings.telegram_chats.length > 0 && (
        <div className="mb-3">
          {settings.telegram_chats.map((chat) => (
            <div key={chat.id} className="flex items-center justify-between py-1.5">
              <span className="text-[13px] text-[#1D1D1F]">✓ {chat.label ?? "Linked chat"}</span>
              <button onClick={() => revoke.mutate(chat.id)} className="text-[12px] text-[#B3261E] cursor-pointer">
                Unlink
              </button>
            </div>
          ))}
        </div>
      )}

      {available && link && status === "PENDING" && (
        <div className="flex flex-col items-center text-center">
          {qr && <img src={qr} alt="Scan to link Telegram" className="w-[180px] h-[180px]" />}
          <p className="text-[12px] text-[#6E6E73] mt-2 leading-relaxed">
            Scan with your phone camera, or open the link, then press <b>Start</b> in Telegram.
            The code expires in {link.expires_in_minutes} minutes.
          </p>
          <a
            href={link.deep_link}
            target="_blank"
            rel="noreferrer"
            className="mt-2 px-3.5 py-1.5 text-[12px] font-medium rounded-full"
            style={{ background: "#229ED9", color: "white" }}
          >
            Open @{link.bot}
          </a>
          <p className="text-[11px] text-[#AEAEB2] mt-2">Waiting for Telegram…</p>
        </div>
      )}

      {available && status === "LINKED" && (
        <p className="text-[13px] text-[#1A7431] mb-2">Linked. Alerts for this facility now reach that chat.</p>
      )}

      {available && (!link || status !== "PENDING") && (
        <button
          onClick={() => create.mutate()}
          disabled={create.isPending}
          className="px-3.5 py-2 text-[13px] font-medium rounded-full cursor-pointer disabled:opacity-50"
          style={{ background: "#229ED9", color: "white" }}
        >
          {settings.telegram_chats.length ? "Link another chat" : "Link a Telegram chat"}
        </button>
      )}
      {error && <p className="text-[12px] text-[#B3261E] mt-2">{error}</p>}
    </Panel>
  );
}

function BrowserCard() {
  const { browserPermission, requestBrowserPermission, mode } = useLive();
  return (
    <Panel title="This browser">
      <p className="text-[13px] text-[#6E6E73] leading-relaxed">
        {mode === "pusher"
          ? "Connected to your facility's private Pusher channel: alerts arrive the instant they happen."
          : "Realtime is not connected, so this console checks for new alerts every 15 seconds."}
      </p>
      {browserPermission === "unsupported" ? (
        <p className="text-[12px] text-[#AEAEB2] mt-2">This browser does not support desktop notifications.</p>
      ) : browserPermission === "granted" ? (
        <p className="text-[12px] text-[#1A7431] mt-2">Desktop notifications are on for when this tab is in the background.</p>
      ) : (
        <button
          onClick={requestBrowserPermission}
          className="mt-3 px-3.5 py-2 text-[13px] font-medium rounded-full cursor-pointer"
          style={{ background: "#F0F0F2", color: "#1D1D1F" }}
          disabled={browserPermission === "denied"}
        >
          {browserPermission === "denied" ? "Blocked in browser settings" : "Enable desktop notifications"}
        </button>
      )}
    </Panel>
  );
}
