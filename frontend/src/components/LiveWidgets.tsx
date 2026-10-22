import { AnimatePresence, motion } from "framer-motion";
import { useLive } from "../contexts/LiveContext";
import type { Severity } from "../api/types";

export const SEVERITY_COLOURS: Record<Severity, { fg: string; bg: string }> = {
  info: { fg: "#0058B0", bg: "#E8F1FD" },
  success: { fg: "#1A7431", bg: "#E7F5EA" },
  warning: { fg: "#9A5B00", bg: "#FFF4E5" },
  critical: { fg: "#B3261E", bg: "#FDECEC" },
};

/** Top-right stack of live alerts. Clicking one opens the related screen. */
export function ToastStack({ onOpen }: { onOpen: (transferId: string | null) => void }) {
  const { toasts, dismiss } = useLive();
  return (
    <div className="fixed top-14 right-5 z-[60] flex flex-col gap-2.5 w-[360px] pointer-events-none">
      <AnimatePresence initial={false}>
        {toasts.map((toast) => {
          const tone = SEVERITY_COLOURS[toast.severity] ?? SEVERITY_COLOURS.info;
          return (
            <motion.div
              key={toast.id}
              layout
              initial={{ opacity: 0, y: -12, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, x: 40 }}
              transition={{ duration: 0.25 }}
              className="pointer-events-auto bg-white rounded-[14px] overflow-hidden cursor-pointer"
              style={{ border: "1px solid #E5E5E7", boxShadow: "0 12px 32px rgba(0,0,0,0.12)" }}
              onClick={() => {
                onOpen(toast.transferId);
                dismiss(toast.id);
              }}
            >
              <div className="flex">
                <div className="w-1.5 flex-shrink-0" style={{ background: tone.fg }} />
                <div className="px-4 py-3 flex-1 min-w-0">
                  <div className="flex items-start justify-between gap-3">
                    <p className="text-[13px] font-semibold text-[#1D1D1F] leading-snug">{toast.title}</p>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        dismiss(toast.id);
                      }}
                      className="text-[#AEAEB2] hover:text-[#1D1D1F] text-[16px] leading-none"
                      aria-label="Dismiss"
                    >
                      ×
                    </button>
                  </div>
                  {toast.body && (
                    <p className="text-[12px] text-[#6E6E73] mt-1 leading-relaxed line-clamp-3">{toast.body}</p>
                  )}
                </div>
              </div>
            </motion.div>
          );
        })}
      </AnimatePresence>
    </div>
  );
}

/** Bell with the unread count, plus how live updates are arriving. */
export function LiveStatus({ onOpenInbox }: { onOpenInbox: () => void }) {
  const { unread, mode } = useLive();
  const label = mode === "pusher" ? "Live · Pusher" : mode === "connecting" ? "Connecting…" : "Polling";
  const dot = mode === "pusher" ? "#1A8A2C" : mode === "connecting" ? "#B25000" : "#AEAEB2";

  return (
    <div className="ml-auto flex items-center gap-3">
      <span className="flex items-center gap-1.5 text-[12px] text-[#6E6E73]" title="How this console receives updates">
        <span className={`w-2 h-2 rounded-full ${mode === "pusher" ? "animate-pulse" : ""}`} style={{ background: dot }} />
        {label}
      </span>
      <button
        onClick={onOpenInbox}
        className="relative w-8 h-8 rounded-full flex items-center justify-center cursor-pointer transition-colors hover:bg-[#F5F5F7]"
        aria-label={`${unread} unread notifications`}
      >
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="#1D1D1F" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
          <path d="M13.73 21a2 2 0 0 1-3.46 0" />
        </svg>
        {unread > 0 && (
          <span
            className="absolute -top-0.5 -right-0.5 min-w-[17px] h-[17px] px-1 rounded-full text-[10px] font-bold text-white flex items-center justify-center"
            style={{ background: "#C41230" }}
          >
            {unread > 99 ? "99+" : unread}
          </span>
        )}
      </button>
    </div>
  );
}
