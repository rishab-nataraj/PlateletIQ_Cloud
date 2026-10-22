import { useEffect, useRef, useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Analytics } from "@vercel/analytics/react";
import { SpeedInsights } from "@vercel/speed-insights/react";
import { AuthProvider, useAuth } from "./contexts/AuthContext";
import { LiveProvider } from "./contexts/LiveContext";
import { LiveStatus, ToastStack } from "./components/LiveWidgets";
import NotificationsScreen from "./screens/NotificationsScreen";
import CloudServicesScreen from "./screens/CloudServicesScreen";
import LoginScreen from "./screens/LoginScreen";
import FacilitySelectScreen from "./screens/FacilitySelectScreen";
import OverviewScreen from "./screens/OverviewScreen";
import ForecastScreen from "./screens/ForecastScreen";
import InventoryScreen from "./screens/InventoryScreen";
import ActionsScreen from "./screens/ActionsScreen";
import AnalyticsScreen from "./screens/AnalyticsScreen";
import TransfersScreen from "./screens/TransfersScreen";
import NetworkScreen from "./screens/NetworkScreen";
import WasteRecoveryScreen from "./screens/WasteRecoveryScreen";
import CampPlanningScreen from "./screens/CampPlanningScreen";
import DataUploadScreen from "./screens/DataUploadScreen";
import TransferTrackingScreen from "./screens/TransferTrackingScreen";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      retry: 1,
      refetchOnWindowFocus: true,
    },
  },
});

type Screen =
  | "overview" | "forecast" | "inventory" | "actions" | "analytics"
  | "transfers" | "tracking" | "network" | "waste" | "camp" | "upload"
  | "notifications" | "cloud";

const SCREENS: Screen[] = [
  "overview", "forecast", "inventory", "actions", "analytics", "transfers", "tracking",
  "network", "waste", "camp", "upload", "notifications", "cloud",
];

/**
 * Links in emails and Telegram messages open a specific screen:
 * ``/?screen=transfers&transfer=TRF-1A2B3C4D``. Read once at load, before the
 * sign-in flow rewrites the URL, and applied when a facility is bound.
 */
const initialParams = (() => {
  if (typeof window === "undefined") return { screen: null as Screen | null, transfer: null as string | null };
  const params = new URLSearchParams(window.location.search);
  const screen = params.get("screen") as Screen | null;
  return { screen: screen && SCREENS.includes(screen) ? screen : null, transfer: params.get("transfer") };
})();

const NAV_GROUPS: { label: string | null; items: { id: Screen; label: string }[] }[] = [
  {
    label: null,
    items: [
      { id: "overview", label: "Overview" },
      { id: "forecast", label: "Forecast" },
      { id: "inventory", label: "Inventory" },
      { id: "actions", label: "Actions" },
      { id: "upload", label: "Data setup" },
      { id: "analytics", label: "Analytics" },
    ],
  },
  {
    label: "Network",
    items: [
      { id: "transfers", label: "Transfers" },
      { id: "tracking", label: "Live tracking" },
      { id: "network", label: "Network" },
    ],
  },
  {
    label: "Planning",
    items: [
      { id: "waste", label: "Waste recovery" },
      { id: "camp", label: "Camp planning" },
    ],
  },
  {
    label: "Cloud",
    items: [
      { id: "notifications", label: "Notifications" },
      { id: "cloud", label: "Cloud services" },
    ],
  },
];

function MainApp() {
  const { user, stage, isLoading, signOut, changeFacility } = useAuth();
  const [screen, setScreen] = useState<Screen>("overview");
  const [trackingId, setTrackingId] = useState<string | null>(null);
  const deepLinkApplied = useRef(false);

  useEffect(() => {
    if (stage !== "facility" || deepLinkApplied.current) return;
    deepLinkApplied.current = true;
    if (initialParams.screen) {
      if (initialParams.screen === "tracking" && initialParams.transfer) setTrackingId(initialParams.transfer);
      setScreen(initialParams.screen);
      window.history.replaceState({}, document.title, window.location.pathname);
    }
  }, [stage]);

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center" style={{ background: "#F5F5F7" }}>
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 rounded-full animate-spin" style={{ border: "3px solid #0071E3", borderTopColor: "transparent" }} />
          <p className="text-[13px] font-medium text-[#6E6E73]">Restoring session…</p>
        </div>
      </div>
    );
  }

  // Stage one: who are you?
  if (stage === "anonymous") return <LoginScreen />;

  // Stage two: which facility? Nothing below this line runs without a bank_id.
  if (stage === "identity") return <FacilitySelectScreen />;

  function openTracking(id: string) {
    setTrackingId(id);
    setScreen("tracking");
  }

  function renderScreen() {
    switch (screen) {
      case "overview": return <OverviewScreen />;
      case "forecast": return <ForecastScreen />;
      case "inventory": return <InventoryScreen />;
      case "actions": return <ActionsScreen />;
      case "analytics": return <AnalyticsScreen />;
      case "transfers": return <TransfersScreen onViewTracking={openTracking} />;
      case "tracking":
        return (
          <TransferTrackingScreen
            transferId={trackingId}
            onBack={() => { setTrackingId(null); setScreen("transfers"); }}
          />
        );
      case "network": return <NetworkScreen />;
      case "waste": return <WasteRecoveryScreen />;
      case "camp": return <CampPlanningScreen />;
      case "upload": return <DataUploadScreen />;
      case "notifications": return <NotificationsScreen onOpenTransfer={() => setScreen("transfers")} />;
      case "cloud": return <CloudServicesScreen />;
    }
  }

  const today = new Date().toLocaleDateString("en-IN", {
    weekday: "long", day: "numeric", month: "long", year: "numeric",
  });

  return (
    <div className="flex h-screen overflow-hidden" style={{ background: "#F5F5F7" }}>
      <aside
        className="w-[248px] flex flex-col flex-shrink-0 overflow-y-auto"
        style={{ background: "white", borderRight: "1px solid #E5E5E7" }}
      >
        <div className="px-6 pt-7 pb-5">
          <h1 className="text-[18px] font-bold text-[#1D1D1F]" style={{ letterSpacing: "-0.4px" }}>
            PlateletIQ
          </h1>
        </div>

        {/* Which hospital this console is. Deliberately prominent: on a second
            laptop this is the only thing that differs. */}
        <div className="mx-4 mb-5 rounded-[11px] px-4 py-3" style={{ background: "#F5F5F7" }}>
          <p className="text-[10px] font-semibold text-[#AEAEB2] uppercase tracking-wider">Signed in at</p>
          <p className="text-[13px] font-semibold text-[#1D1D1F] mt-1 leading-snug">{user?.bank_name}</p>
          <p className="text-[11px] text-[#6E6E73] mt-0.5 font-mono">{user?.bank_id}</p>
          <button
            onClick={changeFacility}
            className="text-[11px] mt-2 cursor-pointer transition-colors"
            style={{ color: "#0071E3" }}
          >
            Switch facility
          </button>
        </div>

        <nav className="flex-1 px-3 pb-4">
          {NAV_GROUPS.map((group, groupIndex) => (
            <div key={groupIndex} className={groupIndex > 0 ? "mt-5" : ""}>
              {group.label && (
                <p className="px-3 mb-1 text-[10px] font-semibold text-[#AEAEB2] uppercase tracking-widest">
                  {group.label}
                </p>
              )}
              {group.items.map((item) => {
                const active = screen === item.id;
                return (
                  <button
                    key={item.id}
                    onClick={() => setScreen(item.id)}
                    className="w-full text-left px-3 py-2 rounded-[8px] text-[14px] transition-colors mb-0.5 cursor-pointer"
                    style={{
                      color: active ? "#1D1D1F" : "#6E6E73",
                      background: active ? "#F5F5F7" : "transparent",
                      fontWeight: active ? 500 : 400,
                    }}
                  >
                    {item.label}
                  </button>
                );
              })}
            </div>
          ))}
        </nav>

        <div className="px-6 py-4" style={{ borderTop: "1px solid #F0F0F0" }}>
          <div className="flex items-center gap-2 mb-1">
            {user?.picture ? (
              <img src={user.picture} alt="" className="w-6 h-6 rounded-full" />
            ) : (
              <div
                className="w-6 h-6 rounded-full text-white text-[11px] font-semibold flex items-center justify-center"
                style={{ background: "#0071E3" }}
              >
                {(user?.name ?? user?.email ?? "?").charAt(0).toUpperCase()}
              </div>
            )}
            <p className="text-[13px] font-medium text-[#1D1D1F] truncate">{user?.name}</p>
          </div>
          <p className="text-[11px] text-[#6E6E73] truncate">{user?.email}</p>
          <p className="text-[11px] text-[#AEAEB2] mt-0.5">{user?.role}</p>
          <button
            onClick={signOut}
            className="text-[11px] text-[#AEAEB2] mt-2 hover:text-[#6E6E73] transition-colors font-medium cursor-pointer"
          >
            Sign out
          </button>
        </div>
      </aside>

      <div className="flex-1 flex flex-col overflow-hidden">
        <div
          className="h-11 flex items-center px-7 gap-2 flex-shrink-0"
          style={{ background: "white", borderBottom: "1px solid #E5E5E7" }}
        >
          <span className="text-[13px] font-medium text-[#1D1D1F]">Chennai, Tamil Nadu</span>
          <span className="text-[#E5E5E7] font-light">·</span>
          <span className="text-[13px] text-[#6E6E73]">{today}</span>
          <span className="text-[#E5E5E7] font-light">·</span>
          {/* Build marker: if this is missing, an older copy of the UI is
              being served on this port. */}
          <span className="text-[12px] text-[#AEAEB2]">Transfer pipeline v4 · cloud</span>
          <LiveStatus onOpenInbox={() => setScreen("notifications")} />
        </div>

        <div className="flex-1 overflow-y-auto">{renderScreen()}</div>
      </div>

      <ToastStack onOpen={(transferId) => setScreen(transferId ? "transfers" : "notifications")} />
    </div>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <LiveProvider>
          <MainApp />
        </LiveProvider>
      </AuthProvider>
      {/* Vercel Web Analytics + Speed Insights: no-ops outside Vercel. */}
      <Analytics />
      <SpeedInsights />
    </QueryClientProvider>
  );
}
