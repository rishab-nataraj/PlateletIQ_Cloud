import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { API_BASE_URL, getStoredToken } from "../api/client";
import { notificationApi, systemApi } from "../api/endpoints";
import type { NotificationItem, Severity } from "../api/types";
import { useAuth } from "./AuthContext";

/**
 * Live updates for the signed-in facility.
 *
 * With Pusher configured on the API, the console subscribes to the facility's
 * private channel and reacts the moment the other hospital acts: a toast, a
 * browser notification if the tab is in the background, and an immediate
 * refetch of whatever changed. Without Pusher it falls back to polling, and
 * says so in the top bar — nothing pretends to be live when it is not.
 */

export type LiveMode = "connecting" | "pusher" | "polling";

export interface Toast {
  id: string;
  title: string;
  body: string | null;
  severity: Severity;
  transferId: string | null;
  link: string | null;
}

interface LiveValue {
  mode: LiveMode;
  connected: boolean;
  unread: number;
  toasts: Toast[];
  dismiss: (id: string) => void;
  browserPermission: NotificationPermission | "unsupported";
  requestBrowserPermission: () => Promise<void>;
}

const LiveContext = createContext<LiveValue | undefined>(undefined);

function browserPermissionNow(): NotificationPermission | "unsupported" {
  return typeof window !== "undefined" && "Notification" in window ? Notification.permission : "unsupported";
}

export const LiveProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { user } = useAuth();
  const bankId = user?.bank_id ?? null;
  const queryClient = useQueryClient();

  const [mode, setMode] = useState<LiveMode>("polling");
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [browserPermission, setBrowserPermission] = useState(browserPermissionNow());
  const seen = useRef<Set<string> | null>(null);

  const dismiss = useCallback((id: string) => setToasts((all) => all.filter((t) => t.id !== id)), []);

  const announce = useCallback(
    (item: Pick<NotificationItem, "id" | "title" | "body" | "severity" | "transfer_id" | "link">) => {
      setToasts((all) => [
        { id: item.id, title: item.title, body: item.body, severity: item.severity, transferId: item.transfer_id, link: item.link },
        ...all.filter((t) => t.id !== item.id),
      ].slice(0, 4));
      window.setTimeout(() => dismiss(item.id), 8000);

      if (document.hidden && browserPermissionNow() === "granted") {
        try {
          new Notification(item.title, { body: item.body ?? undefined, tag: item.id });
        } catch {
          /* some browsers only allow notifications from a service worker */
        }
      }
    },
    [dismiss],
  );

  const { data: config } = useQuery({
    queryKey: ["client-config"],
    queryFn: systemApi.clientConfig,
    staleTime: Infinity,
    enabled: Boolean(bankId),
  });

  // Unread notifications: the bell's count, and a safety net that still raises
  // toasts when realtime is off (or a push was missed).
  const { data: unreadItems } = useQuery({
    queryKey: ["notifications", "unread", bankId],
    queryFn: () => notificationApi.list(true),
    enabled: Boolean(bankId),
    refetchInterval: mode === "pusher" ? 60_000 : 15_000,
  });

  useEffect(() => {
    if (!unreadItems) return;
    if (seen.current === null) {
      // First load: everything already waiting is not "new".
      seen.current = new Set(unreadItems.map((n) => n.id));
      return;
    }
    for (const item of unreadItems) {
      if (!seen.current.has(item.id)) {
        seen.current.add(item.id);
        announce(item);
      }
    }
  }, [unreadItems, announce]);

  useEffect(() => {
    seen.current = null;
  }, [bankId]);

  // Pusher subscription for this facility.
  useEffect(() => {
    if (!bankId || !config?.realtime.enabled || !config.realtime.key) {
      setMode("polling");
      return;
    }
    let cancelled = false;
    let disconnect: (() => void) | null = null;
    setMode("connecting");

    import("pusher-js").then(({ default: Pusher }) => {
      if (cancelled) return;
      const pusher = new Pusher(config.realtime.key!, {
        cluster: config.realtime.cluster!,
        channelAuthorization: {
          endpoint: `${API_BASE_URL}/realtime/auth`,
          transport: "ajax",
          headersProvider: () => ({ Authorization: `Bearer ${getStoredToken() ?? ""}` }),
        },
      });
      const channel = pusher.subscribe(`private-facility-${bankId}`);

      channel.bind("pusher:subscription_succeeded", () => setMode("pusher"));
      channel.bind("pusher:subscription_error", () => setMode("polling"));
      pusher.connection.bind("state_change", (states: { current: string }) => {
        if (states.current !== "connected") setMode(states.current === "connecting" ? "connecting" : "polling");
      });

      channel.bind("notification", (item: NotificationItem) => {
        seen.current?.add(item.id);
        announce(item);
        queryClient.invalidateQueries({ queryKey: ["notifications"] });
        if (item.transfer_id) queryClient.invalidateQueries({ queryKey: ["transfers"] });
      });
      channel.bind("transfer.updated", () => {
        queryClient.invalidateQueries({ queryKey: ["transfers"] });
        queryClient.invalidateQueries({ queryKey: ["track"] });
        queryClient.invalidateQueries({ queryKey: ["facility"] });
      });

      disconnect = () => {
        pusher.unsubscribe(`private-facility-${bankId}`);
        pusher.disconnect();
      };
    });

    return () => {
      cancelled = true;
      disconnect?.();
    };
  }, [bankId, config, announce, queryClient]);

  const requestBrowserPermission = useCallback(async () => {
    if (!("Notification" in window)) return;
    const result = await Notification.requestPermission();
    setBrowserPermission(result);
  }, []);

  const value = useMemo<LiveValue>(
    () => ({
      mode,
      connected: mode === "pusher",
      unread: unreadItems?.length ?? 0,
      toasts,
      dismiss,
      browserPermission,
      requestBrowserPermission,
    }),
    [mode, unreadItems, toasts, dismiss, browserPermission, requestBrowserPermission],
  );

  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
};

export function useLive(): LiveValue {
  const context = useContext(LiveContext);
  if (!context) throw new Error("useLive must be used inside LiveProvider");
  return context;
}

/** Poll fast without realtime, slowly as a safety net with it. */
export function usePollInterval(fastMs: number): number {
  const { connected } = useLive();
  return connected ? 20_000 : fastMs;
}
