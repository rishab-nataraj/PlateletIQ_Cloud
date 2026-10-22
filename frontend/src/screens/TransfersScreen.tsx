import { useCallback, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "../contexts/AuthContext";
import { usePollInterval } from "../contexts/LiveContext";
import { facilityApi, transferApi } from "../api/endpoints";
import type { Counterparty, Transfer } from "../api/types";
import { CodeEntry, IssuedCodeCard } from "../components/OtpPanels";

/**
 * How often each console re-reads the shared state when realtime is off.
 * With Pusher connected, changes are pushed and this drops to a 20 s safety net.
 */
const POLL_INTERVAL_MS = 2500;

/**
 * Codes the API has handed this console, keyed by transfer.
 *
 * This lives above the card list on purpose. A card moves between the
 * "needs your action" and "in progress" sections as its status advances, which
 * remounts it and would wipe component state — losing a code that the API will
 * never return again. Holding it here keeps it on screen across that move.
 *
 * In memory only: nothing is written to storage. If the tab is reloaded the
 * code is gone, and the sender re-issues it, which supersedes the old one.
 */
interface IssuedCodes {
  pickup?: { code: string; expiresAt?: string };
  delivery?: { code: string; expiresAt?: string };
}

const STATUS_LABELS: Record<string, string> = {
  REQUESTED: "Awaiting the sending facility",
  ACCEPTED: "Authorised",
  UNITS_RESERVED: "Units reserved",
  SHIPMENT_CREATED: "Courier booked",
  AWB_ASSIGNED: "Airway bill assigned",
  PICKUP_OTP_REQUIRED: "Awaiting pickup confirmation",
  IN_TRANSIT: "In transit",
  ARRIVED: "Arrived — awaiting receipt code",
  DELIVERY_OTP_REQUIRED: "Awaiting receipt code",
  TRANSFER_COMPLETED: "Delivered and settled",
  DECLINED: "Declined",
  CANCELLED: "Cancelled",
  FAILED: "Dispatch failed",
};

const STATUS_TONES: Record<string, { bg: string; fg: string }> = {
  REQUESTED: { bg: "#FFF4E5", fg: "#9A5B00" },
  IN_TRANSIT: { bg: "#E8F1FD", fg: "#0058B0" },
  ARRIVED: { bg: "#E8F1FD", fg: "#0058B0" },
  TRANSFER_COMPLETED: { bg: "#E7F5EA", fg: "#1A7431" },
  DECLINED: { bg: "#FDECEC", fg: "#B3261E" },
  CANCELLED: { bg: "#F0F0F2", fg: "#6E6E73" },
  FAILED: { bg: "#FDECEC", fg: "#B3261E" },
};

export default function TransfersScreen({ onViewTracking }: { onViewTracking?: (id: string) => void }) {
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const bankId = user?.bank_id ?? null;
  const pollInterval = usePollInterval(POLL_INTERVAL_MS);

  const [composer, setComposer] = useState<"SHORTAGE_PULL" | "WASTAGE_PUSH" | null>(null);
  const [banner, setBanner] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [issuedCodes, setIssuedCodes] = useState<Record<string, IssuedCodes>>({});

  const rememberCode = useCallback(
    (transferId: string, kind: keyof IssuedCodes, code: string, expiresAt?: string) => {
      setIssuedCodes((current) => ({
        ...current,
        [transferId]: { ...current[transferId], [kind]: { code, expiresAt } },
      }));
    },
    [],
  );

  /**
   * Both consoles poll the same endpoint. An action taken on one laptop shows
   * up on the other within one interval — there is no local mirror of the
   * state, so the two screens cannot disagree.
   */
  const { data: transfers, isLoading, error } = useQuery({
    queryKey: ["transfers", bankId],
    queryFn: () => transferApi.list("all"),
    refetchInterval: pollInterval,
    enabled: Boolean(bankId),
  });

  const { incoming, outgoing, history } = useMemo(() => {
    const rows = transfers ?? [];
    return {
      // Anything waiting on an action from this console.
      incoming: rows.filter((t) => needsMyAction(t)),
      outgoing: rows.filter((t) => !needsMyAction(t) && !isFinished(t)),
      history: rows.filter(isFinished),
    };
  }, [transfers]);

  function refresh() {
    queryClient.invalidateQueries({ queryKey: ["transfers", bankId] });
    queryClient.invalidateQueries({ queryKey: ["facility", bankId] });
  }

  return (
    <div className="px-8 py-7 max-w-[1180px]">
      <header className="flex flex-wrap items-start justify-between gap-4 mb-7">
        <div>
          <h1 className="text-[26px] font-semibold text-[#1D1D1F] tracking-tight">Transfers</h1>
          <p className="text-[14px] text-[#6E6E73] mt-1 max-w-[560px] leading-relaxed">
            Pull units you are short of, or push units that would otherwise expire. Custody is
            verified with a one-time code at each end.
          </p>
        </div>

        <div className="flex gap-2.5">
          <button
            onClick={() => setComposer("SHORTAGE_PULL")}
            className="px-4 py-2.5 text-white text-[14px] font-medium rounded-full cursor-pointer transition-colors"
            style={{ background: "#0071E3" }}
          >
            Request units
          </button>
          <button
            onClick={() => setComposer("WASTAGE_PUSH")}
            className="px-4 py-2.5 text-white text-[14px] font-medium rounded-full cursor-pointer transition-colors"
            style={{ background: "#B26A00" }}
          >
            Offer expiring units
          </button>
        </div>
      </header>

      {banner && (
        <div
          className="rounded-[12px] px-4 py-3 mb-6 flex items-start justify-between gap-4"
          style={
            banner.tone === "ok"
              ? { background: "#E7F5EA", border: "1px solid #B7E0C1" }
              : { background: "#FDECEC", border: "1px solid #F5C6C6" }
          }
        >
          <p className="text-[13px] leading-relaxed" style={{ color: banner.tone === "ok" ? "#1A7431" : "#B3261E" }}>
            {banner.text}
          </p>
          <button onClick={() => setBanner(null)} className="text-[13px] opacity-60 cursor-pointer">Dismiss</button>
        </div>
      )}

      {error && (
        <div className="rounded-[12px] px-5 py-4 mb-6" style={{ background: "#FDECEC", border: "1px solid #F5C6C6" }}>
          <p className="text-[14px] font-medium text-[#B3261E]">Could not load transfers.</p>
          <p className="text-[13px] text-[#B3261E] mt-1">{error instanceof Error ? error.message : "Unknown error."}</p>
        </div>
      )}

      {isLoading && <p className="text-[14px] text-[#6E6E73]">Loading transfers…</p>}

      <Section
        title="Needs your action"
        count={incoming.length}
        empty="Nothing is waiting on you right now."
        show={!isLoading}
      >
        {incoming.map((transfer) => (
          <TransferCard
            key={transfer.id}
            transfer={transfer}
            codes={issuedCodes[transfer.id]}
            onCodeIssued={rememberCode}
            onRefresh={refresh}
            onBanner={setBanner}
            onViewTracking={onViewTracking}
          />
        ))}
      </Section>

      <Section
        title="In progress"
        count={outgoing.length}
        empty="No transfers are in flight."
        show={!isLoading}
      >
        {outgoing.map((transfer) => (
          <TransferCard
            key={transfer.id}
            transfer={transfer}
            codes={issuedCodes[transfer.id]}
            onCodeIssued={rememberCode}
            onRefresh={refresh}
            onBanner={setBanner}
            onViewTracking={onViewTracking}
          />
        ))}
      </Section>

      {history.length > 0 && (
        <Section title="Closed" count={history.length} empty="" show>
          {history.slice(0, 12).map((transfer) => (
            <TransferCard
              key={transfer.id}
              transfer={transfer}
              codes={issuedCodes[transfer.id]}
              onCodeIssued={rememberCode}
              onRefresh={refresh}
              onBanner={setBanner}
              onViewTracking={onViewTracking}
            />
          ))}
        </Section>
      )}

      {composer && (
        <ComposerDrawer
          direction={composer}
          onClose={() => setComposer(null)}
          onCreated={(transfer) => {
            setComposer(null);
            refresh();
            setBanner({
              tone: "ok",
              text:
                transfer.direction === "SHORTAGE_PULL"
                  ? `Request ${transfer.id} sent to ${transfer.source.short_name}. They decide whether to release the units.`
                  : `Offer ${transfer.id} sent to ${transfer.destination.short_name}.`,
            });
          }}
        />
      )}
    </div>
  );
}

// ── Cards ────────────────────────────────────────────────────────────────────

function TransferCard({
  transfer,
  codes,
  onCodeIssued,
  onRefresh,
  onBanner,
  onViewTracking,
}: {
  transfer: Transfer;
  codes?: IssuedCodes;
  onCodeIssued: (id: string, kind: keyof IssuedCodes, code: string, expiresAt?: string) => void;
  onRefresh: () => void;
  onBanner: (banner: { tone: "ok" | "error"; text: string }) => void;
  onViewTracking?: (id: string) => void;
}) {
  const isSender = transfer.viewer_role === "SENDER";
  const counterparty = isSender ? transfer.destination : transfer.source;
  const tone = STATUS_TONES[transfer.status] ?? { bg: "#F0F0F2", fg: "#6E6E73" };

  const pickupCode = codes?.pickup ?? null;
  const receiptCode = codes?.delivery ?? null;
  const [entry, setEntry] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);

  function fail(err: unknown) {
    const message = err instanceof Error ? err.message : "The action failed.";
    setActionError(message);
    onBanner({ tone: "error", text: message });
  }

  const accept = useMutation({
    mutationFn: () => transferApi.accept(transfer.id),
    onSuccess: (result) => {
      setActionError(null);
      if (result.pickup_otp_code) {
        onCodeIssued(transfer.id, "pickup", result.pickup_otp_code, result.pickup_otp_expires_at);
      }
      onRefresh();
    },
    onError: fail,
  });

  const decline = useMutation({
    mutationFn: (reason: string) => transferApi.decline(transfer.id, reason),
    onSuccess: () => { setActionError(null); onRefresh(); },
    onError: fail,
  });

  const cancel = useMutation({
    mutationFn: (reason: string) => transferApi.cancel(transfer.id, reason),
    onSuccess: () => { setActionError(null); onRefresh(); },
    onError: fail,
  });

  const reissue = useMutation({
    mutationFn: () => transferApi.reissuePickupOtp(transfer.id),
    onSuccess: (result) => {
      setActionError(null);
      onCodeIssued(transfer.id, "pickup", result.otp_code, result.expires_at);
    },
    onError: fail,
  });

  const reissueReceipt = useMutation({
    mutationFn: () => transferApi.reissueDeliveryOtp(transfer.id),
    onSuccess: (result) => {
      setActionError(null);
      onCodeIssued(transfer.id, "delivery", result.otp_code, result.expires_at);
    },
    onError: fail,
  });

  const verifyPickup = useMutation({
    mutationFn: (code: string) => transferApi.verifyPickup(transfer.id, code),
    onSuccess: (result) => {
      setActionError(null);
      setEntry("");
      if (result.delivery_otp_code) {
        onCodeIssued(transfer.id, "delivery", result.delivery_otp_code, result.delivery_otp_expires_at);
      }
      onRefresh();
      onBanner({ tone: "ok", text: `${transfer.id} is on its way. Pass the receipt code to ${transfer.destination.short_name}.` });
    },
    onError: fail,
  });

  const verifyDelivery = useMutation({
    mutationFn: (code: string) => transferApi.verifyDelivery(transfer.id, code),
    onSuccess: (result) => {
      setActionError(null);
      setEntry("");
      onRefresh();
      onBanner({
        tone: "ok",
        text: `${result.settlement?.units_transferred ?? transfer.units} ${transfer.component_type} unit(s) received and added to your inventory.`,
      });
    },
    onError: fail,
  });

  const busy =
    accept.isPending || decline.isPending || cancel.isPending ||
    reissue.isPending || reissueReceipt.isPending ||
    verifyPickup.isPending || verifyDelivery.isPending;

  return (
    <article className="bg-white rounded-[14px] p-5 mb-3" style={{ border: "1px solid #E5E5E7" }}>
      {/* Header row */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-[280px] flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span
              className="text-[11px] font-semibold px-2.5 py-1 rounded-full"
              style={{ background: tone.bg, color: tone.fg }}
            >
              {STATUS_LABELS[transfer.status] ?? transfer.status}
            </span>
            <span
              className="text-[11px] font-semibold px-2.5 py-1 rounded-full"
              style={{ background: "#F0F0F2", color: "#6E6E73" }}
            >
              {isSender ? "You are sending" : "You are receiving"}
            </span>
            <span className="text-[11px] font-mono text-[#AEAEB2]">{transfer.id}</span>
          </div>

          <p className="text-[16px] font-semibold text-[#1D1D1F] mt-2.5 leading-snug">
            {transfer.units} {transfer.component_type} unit{transfer.units === 1 ? "" : "s"}{" "}
            {isSender ? "to" : "from"} {counterparty.short_name}
          </p>

          {transfer.reason && (
            <p className="text-[13px] text-[#6E6E73] mt-1 leading-relaxed">{transfer.reason}</p>
          )}
          {transfer.decline_reason && (
            <p className="text-[13px] mt-1 leading-relaxed" style={{ color: "#B3261E" }}>
              {transfer.decline_reason}
            </p>
          )}
        </div>

        <div className="flex flex-wrap gap-2">
          {transfer.status === "TRANSFER_COMPLETED" && <CertificateButton transferId={transfer.id} />}
          {transfer.route_geometry && (
            <button
              onClick={() => onViewTracking?.(transfer.id)}
              className="text-[13px] font-medium px-3.5 py-2 rounded-full cursor-pointer transition-colors"
              style={{ background: "#F0F0F2", color: "#1D1D1F" }}
            >
              Track shipment
            </button>
          )}
        </div>
      </div>

      {/* Logistics facts */}
      <div className="flex flex-wrap gap-x-8 gap-y-2 mt-4 pt-4" style={{ borderTop: "1px solid #F0F0F0" }}>
        <Fact label="Distance" value={transfer.distance_km ? `${transfer.distance_km.toFixed(2)} km` : "—"} />
        <Fact
          label={transfer.status === "IN_TRANSIT" ? "ETA remaining" : "Routed ETA"}
          value={
            transfer.status === "IN_TRANSIT" && transfer.eta_remaining_minutes !== null
              ? `${transfer.eta_remaining_minutes} min`
              : transfer.eta_minutes !== null
                ? `${transfer.eta_minutes} min`
                : "—"
          }
        />
        <Fact label="Courier" value={transfer.courier_name ?? "Not booked yet"} />
        <Fact label="AWB" value={transfer.awb_code ?? "—"} mono />
        {transfer.reserved_units > 0 && <Fact label="Reserved" value={`${transfer.reserved_units} units`} />}
      </div>

      {actionError && (
        <p className="text-[13px] mt-3 leading-relaxed" style={{ color: "#B3261E" }}>{actionError}</p>
      )}

      {/* ── What this console can do right now ────────────────────────────── */}

      {/* Sender: decide on an open request */}
      {isSender && transfer.status === "REQUESTED" && (
        <div className="mt-4 pt-4 flex flex-wrap gap-2.5" style={{ borderTop: "1px solid #F0F0F0" }}>
          <button
            onClick={() => accept.mutate()}
            disabled={busy}
            className="px-4 py-2.5 text-white text-[14px] font-medium rounded-full cursor-pointer disabled:opacity-50"
            style={{ background: "#1A7431" }}
          >
            {accept.isPending ? "Authorising…" : "Authorise and book courier"}
          </button>
          <button
            onClick={() => decline.mutate("Below our own safety stock")}
            disabled={busy}
            className="px-4 py-2.5 text-[14px] font-medium rounded-full cursor-pointer disabled:opacity-50"
            style={{ background: "#F0F0F2", color: "#1D1D1F" }}
          >
            Decline
          </button>
          <p className="text-[12px] text-[#6E6E73] self-center leading-relaxed max-w-[340px]">
            Authorising reserves the units, books the courier, and issues your pickup code.
          </p>
        </div>
      )}

      {/* Receiver: waiting on the other side */}
      {!isSender && transfer.status === "REQUESTED" && (
        <div className="mt-4 pt-4 flex flex-wrap items-center gap-3" style={{ borderTop: "1px solid #F0F0F0" }}>
          <p className="text-[13px] text-[#6E6E73] flex-1 min-w-[260px] leading-relaxed">
            Waiting for {transfer.source.short_name} to authorise. This page updates on its own.
          </p>
          {transfer.opened_by_bank_id === transfer.destination.id && (
            <button
              onClick={() => cancel.mutate("Withdrawn by the requesting facility")}
              disabled={busy}
              className="px-4 py-2 text-[13px] font-medium rounded-full cursor-pointer disabled:opacity-50"
              style={{ background: "#F0F0F2", color: "#1D1D1F" }}
            >
              Withdraw request
            </button>
          )}
        </div>
      )}

      {/* Sender: hold the pickup code, confirm when the rider collects */}
      {isSender && ["PICKUP_OTP_REQUIRED", "SHIPMENT_CREATED", "AWB_ASSIGNED", "UNITS_RESERVED"].includes(transfer.status) && (
        <div className="mt-4 pt-4 grid gap-4 md:grid-cols-2" style={{ borderTop: "1px solid #F0F0F0" }}>
          {pickupCode ? (
            <IssuedCodeCard
              code={pickupCode.code}
              title="Your pickup code"
              instruction="Give this to the rider when they collect the box, then confirm it here to release custody."
              expiresAt={pickupCode.expiresAt}
            />
          ) : (
            <div className="rounded-[14px] p-5" style={{ background: "#F5F5F7", border: "1px solid #E5E5E7" }}>
              <p className="text-[13px] text-[#6E6E73] leading-relaxed">
                A pickup code was issued to this facility. It is shown only once — if you no longer
                have it, issue a new one.
              </p>
              <button
                onClick={() => reissue.mutate()}
                disabled={busy}
                className="mt-3 px-4 py-2 text-[13px] font-medium rounded-full cursor-pointer disabled:opacity-50"
                style={{ background: "#1D1D1F", color: "white" }}
              >
                {reissue.isPending ? "Issuing…" : "Issue a new pickup code"}
              </button>
            </div>
          )}

          <div>
            <p className="text-[13px] font-medium text-[#1D1D1F] mb-2.5">
              Confirm pickup to dispatch
            </p>
            <CodeEntry
              value={entry}
              onChange={setEntry}
              onComplete={(code) => verifyPickup.mutate(code)}
              disabled={busy}
            />
            <button
              onClick={() => verifyPickup.mutate(entry)}
              disabled={busy || entry.length < 6}
              className="mt-3 px-5 py-2.5 text-white text-[14px] font-medium rounded-full cursor-pointer disabled:opacity-50"
              style={{ background: "#0071E3" }}
            >
              {verifyPickup.isPending ? "Verifying…" : "Confirm pickup"}
            </button>
          </div>
        </div>
      )}

      {/* Sender in transit: carry the receipt code to the receiver */}
      {isSender && transfer.status === "IN_TRANSIT" && (
        <div className="mt-4 pt-4" style={{ borderTop: "1px solid #F0F0F0" }}>
          {receiptCode ? (
            <div className="md:max-w-[420px]">
              <IssuedCodeCard
                code={receiptCode.code}
                title={`Receipt code for ${transfer.destination.short_name}`}
                instruction={`Pass this to ${transfer.destination.short_name} through your usual channel. Only they can redeem it, and redeeming it is what moves the units onto their ledger.`}
                expiresAt={receiptCode.expiresAt}
              />
            </div>
          ) : (
            <div className="rounded-[14px] p-5 md:max-w-[460px]" style={{ background: "#F5F5F7", border: "1px solid #E5E5E7" }}>
              <p className="text-[13px] text-[#6E6E73] leading-relaxed">
                In transit. The receipt code was issued at dispatch and shown once.{" "}
                {transfer.destination.short_name} needs it to close the transfer — if you no longer
                have it, issue a new one. The old code stops working immediately.
              </p>
              <button
                onClick={() => reissueReceipt.mutate()}
                disabled={busy}
                className="mt-3 px-4 py-2 text-[13px] font-medium rounded-full cursor-pointer disabled:opacity-50"
                style={{ background: "#1D1D1F", color: "white" }}
              >
                {reissueReceipt.isPending ? "Issuing…" : "Issue a new receipt code"}
              </button>
            </div>
          )}
        </div>
      )}

      {/* Receiver: redeem the sender's code */}
      {!isSender && ["IN_TRANSIT", "ARRIVED", "DELIVERY_OTP_REQUIRED"].includes(transfer.status) && (
        <div className="mt-4 pt-4" style={{ borderTop: "1px solid #F0F0F0" }}>
          <p className="text-[13px] font-medium text-[#1D1D1F]">
            Enter the receipt code from {transfer.source.short_name}
          </p>
          <p className="text-[12px] text-[#6E6E73] mt-1 mb-3 leading-relaxed max-w-[460px]">
            {transfer.delivery_otp.issued
              ? "The sending facility holds this code. Check the box contents and the cold-chain seal before entering it — this is what adds the units to your inventory."
              : "The sending facility has not confirmed pickup yet, so no receipt code exists."}
          </p>
          <CodeEntry
            value={entry}
            onChange={setEntry}
            onComplete={(code) => verifyDelivery.mutate(code)}
            disabled={busy || !transfer.delivery_otp.issued}
          />
          <button
            onClick={() => verifyDelivery.mutate(entry)}
            disabled={busy || entry.length < 6 || !transfer.delivery_otp.issued}
            className="mt-3 px-5 py-2.5 text-white text-[14px] font-medium rounded-full cursor-pointer disabled:opacity-50"
            style={{ background: "#1A7431" }}
          >
            {verifyDelivery.isPending ? "Verifying…" : "Confirm receipt"}
          </button>
        </div>
      )}

      {transfer.status === "TRANSFER_COMPLETED" && (
        <p className="text-[13px] mt-4 pt-4 leading-relaxed" style={{ borderTop: "1px solid #F0F0F0", color: "#1A7431" }}>
          Settled. {transfer.units} {transfer.component_type} unit(s) moved from{" "}
          {transfer.source.short_name} to {transfer.destination.short_name}.
        </p>
      )}
    </article>
  );
}

// ── Composer ─────────────────────────────────────────────────────────────────

function ComposerDrawer({
  direction,
  onClose,
  onCreated,
}: {
  direction: "SHORTAGE_PULL" | "WASTAGE_PUSH";
  onClose: () => void;
  onCreated: (transfer: Transfer) => void;
}) {
  const isPull = direction === "SHORTAGE_PULL";

  const [componentType, setComponentType] = useState("SDP");
  const [counterpartyId, setCounterpartyId] = useState<string | null>(null);
  const [units, setUnits] = useState(12);
  const [priority, setPriority] = useState("URGENT");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);

  // Candidates carry the other facility's live stock, so a pull can be aimed
  // at somebody who actually has the units.
  const { data: candidates, isLoading } = useQuery({
    queryKey: ["counterparties", componentType],
    queryFn: () => facilityApi.counterparties(componentType),
  });

  const sorted = useMemo(() => {
    if (!candidates) return [];
    if (isPull) {
      // Most usable stock first — that is who can actually help.
      return [...candidates].sort((a, b) => b.available_units - a.available_units);
    }
    // Pushing near-expiry units: nearest first, since shelf life is the constraint.
    return [...candidates].sort(
      (a, b) => (a.straight_line_km ?? Infinity) - (b.straight_line_km ?? Infinity),
    );
  }, [candidates, isPull]);

  const create = useMutation({
    mutationFn: () =>
      transferApi.create({
        counterparty_bank_id: counterpartyId!,
        direction,
        units,
        component_type: componentType,
        priority,
        reason: reason.trim() || null,
      }),
    onSuccess: onCreated,
    onError: (err) => setError(err instanceof Error ? err.message : "Could not open the transfer."),
  });

  const chosen = sorted.find((candidate) => candidate.id === counterpartyId) ?? null;

  return (
    <div className="fixed inset-0 z-50 flex justify-end" style={{ background: "rgba(0,0,0,0.35)" }} onClick={onClose}>
      <div
        className="w-full max-w-[520px] bg-white h-full overflow-y-auto"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="px-7 py-6 flex items-start justify-between" style={{ borderBottom: "1px solid #E5E5E7" }}>
          <div>
            <h2 className="text-[20px] font-semibold text-[#1D1D1F] tracking-tight">
              {isPull ? "Request units" : "Offer expiring units"}
            </h2>
            <p className="text-[13px] text-[#6E6E73] mt-1 leading-relaxed max-w-[380px]">
              {isPull
                ? "Ask another facility to release units to you. They decide, and they control the handover."
                : "Offer units you are unlikely to use before they expire. You remain the sender and control the handover."}
            </p>
          </div>
          <button onClick={onClose} className="text-[22px] text-[#AEAEB2] leading-none cursor-pointer">×</button>
        </div>

        <div className="px-7 py-6 space-y-6">
          <Field label="Component">
            <div className="flex gap-2">
              {["SDP", "RDP"].map((option) => (
                <button
                  key={option}
                  onClick={() => { setComponentType(option); setCounterpartyId(null); }}
                  className="px-4 py-2 text-[14px] font-medium rounded-full cursor-pointer"
                  style={
                    componentType === option
                      ? { background: "#1D1D1F", color: "white" }
                      : { background: "#F0F0F2", color: "#6E6E73" }
                  }
                >
                  {option}
                </button>
              ))}
            </div>
          </Field>

          <Field label={isPull ? "Request from" : "Offer to"}>
            {isLoading && <p className="text-[13px] text-[#6E6E73]">Loading facilities…</p>}
            <div className="space-y-2 max-h-[300px] overflow-y-auto pr-1">
              {sorted.map((candidate) => (
                <CandidateRow
                  key={candidate.id}
                  candidate={candidate}
                  isPull={isPull}
                  selected={candidate.id === counterpartyId}
                  onSelect={() => setCounterpartyId(candidate.id)}
                />
              ))}
            </div>
          </Field>

          <Field label="Units">
            <input
              type="number"
              min={1}
              max={200}
              value={units}
              onChange={(event) => setUnits(Math.max(1, Number(event.target.value) || 1))}
              className="w-32 bg-[#F5F5F7] border border-[#E5E5E7] text-[#1D1D1F] text-[14px] rounded-[10px] px-3 py-2.5 focus:outline-none focus:border-[#0071E3]"
            />
            {isPull && chosen && units > chosen.available_units && (
              <p className="text-[12px] mt-2 leading-relaxed" style={{ color: "#B26A00" }}>
                {chosen.short_name} currently shows {chosen.available_units} usable {componentType} unit(s).
                They will not be able to authorise more than that.
              </p>
            )}
          </Field>

          <Field label="Priority">
            <div className="flex gap-2">
              {["ROUTINE", "URGENT", "EMERGENCY"].map((option) => (
                <button
                  key={option}
                  onClick={() => setPriority(option)}
                  className="px-3.5 py-2 text-[13px] font-medium rounded-full cursor-pointer"
                  style={
                    priority === option
                      ? { background: "#1D1D1F", color: "white" }
                      : { background: "#F0F0F2", color: "#6E6E73" }
                  }
                >
                  {option.charAt(0) + option.slice(1).toLowerCase()}
                </button>
              ))}
            </div>
          </Field>

          <Field label="Clinical justification">
            <textarea
              rows={3}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder={
                isPull
                  ? "e.g. 4 dengue patients below 10,000 platelet count, no SDP on hand"
                  : "e.g. 6 SDP units expire in 14 hours, no scheduled demand"
              }
              className="w-full bg-[#F5F5F7] border border-[#E5E5E7] text-[#1D1D1F] text-[14px] rounded-[10px] px-3 py-2.5 focus:outline-none focus:border-[#0071E3] resize-none"
            />
            <p className="text-[12px] text-[#AEAEB2] mt-1.5 leading-relaxed">
              This is what the other facility sees when deciding.
            </p>
          </Field>

          {error && (
            <div className="rounded-[10px] px-4 py-3" style={{ background: "#FDECEC", border: "1px solid #F5C6C6" }}>
              <p className="text-[13px] leading-relaxed" style={{ color: "#B3261E" }}>{error}</p>
            </div>
          )}
        </div>

        <div className="px-7 py-5 sticky bottom-0 bg-white flex gap-3" style={{ borderTop: "1px solid #E5E5E7" }}>
          <button
            onClick={() => create.mutate()}
            disabled={!counterpartyId || create.isPending}
            className="flex-1 py-3 text-white text-[15px] font-medium rounded-full cursor-pointer disabled:opacity-50"
            style={{ background: isPull ? "#0071E3" : "#B26A00" }}
          >
            {create.isPending ? "Sending…" : isPull ? "Send request" : "Send offer"}
          </button>
          <button
            onClick={onClose}
            className="px-5 py-3 text-[15px] font-medium rounded-full cursor-pointer"
            style={{ background: "#F0F0F2", color: "#1D1D1F" }}
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

function CandidateRow({
  candidate,
  isPull,
  selected,
  onSelect,
}: {
  candidate: Counterparty;
  isPull: boolean;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className="w-full text-left rounded-[11px] px-4 py-3 cursor-pointer transition-all"
      style={{
        border: selected ? "2px solid #0071E3" : "1px solid #E5E5E7",
        background: selected ? "#F5F9FF" : "white",
      }}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[14px] font-medium text-[#1D1D1F] truncate">{candidate.short_name}</p>
          <p className="text-[12px] text-[#6E6E73] mt-0.5">
            {candidate.straight_line_km !== null ? `${candidate.straight_line_km.toFixed(1)} km away` : "Distance unknown"}
            {" · "}{candidate.tier}
          </p>
        </div>
        <div className="text-right flex-shrink-0">
          <p className="text-[15px] font-semibold text-[#1D1D1F] leading-tight">{candidate.available_units}</p>
          <p className="text-[11px] text-[#AEAEB2]">usable {candidate.component_type}</p>
          {isPull && candidate.expiring_24h > 0 && (
            <p className="text-[11px] mt-0.5" style={{ color: "#C77700" }}>
              {candidate.expiring_24h} expire soon
            </p>
          )}
        </div>
      </div>
    </button>
  );
}

// ── Small pieces ─────────────────────────────────────────────────────────────

function Section({
  title,
  count,
  empty,
  show,
  children,
}: {
  title: string;
  count: number;
  empty: string;
  show: boolean;
  children: React.ReactNode;
}) {
  if (!show) return null;
  return (
    <section className="mb-9">
      <div className="flex items-baseline gap-2.5 mb-3">
        <h2 className="text-[11px] font-semibold text-[#AEAEB2] uppercase tracking-widest">{title}</h2>
        {count > 0 && <span className="text-[11px] text-[#AEAEB2]">{count}</span>}
      </div>
      {count === 0 ? <p className="text-[13px] text-[#AEAEB2]">{empty}</p> : children}
    </section>
  );
}

function Fact({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <p className="text-[10px] font-semibold text-[#AEAEB2] uppercase tracking-wider">{label}</p>
      <p className={`text-[13px] text-[#1D1D1F] mt-0.5 ${mono ? "font-mono" : "font-medium"}`}>{value}</p>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="text-[11px] font-semibold text-[#1D1D1F] uppercase tracking-wider mb-2">{label}</p>
      {children}
    </div>
  );
}

// ── Predicates ───────────────────────────────────────────────────────────────

function isFinished(transfer: Transfer): boolean {
  return ["TRANSFER_COMPLETED", "DECLINED", "CANCELLED", "FAILED"].includes(transfer.status);
}

/** True when the ball is in this console's court. */
/**
 * Opens the chain-of-custody certificate: an archived copy from S3 / Supabase
 * Storage when one exists, otherwise a PDF the API renders on the spot.
 */
function CertificateButton({ transferId }: { transferId: string }) {
  const [state, setState] = useState<"idle" | "loading" | "error">("idle");
  async function open() {
    // Open the tab synchronously so pop-up blockers allow it, then point it at the PDF.
    const tab = window.open("about:blank", "_blank");
    setState("loading");
    try {
      const url = await transferApi.certificate(transferId);
      if (tab) tab.location.href = url;
      else window.location.href = url;
      setState("idle");
    } catch {
      tab?.close();
      setState("error");
    }
  }
  return (
    <button
      onClick={open}
      disabled={state === "loading"}
      className="text-[13px] font-medium px-3.5 py-2 rounded-full cursor-pointer transition-colors disabled:opacity-50"
      style={{ background: "#E7F5EA", color: "#1A7431" }}
      title="Chain-of-custody certificate (PDF)"
    >
      {state === "loading" ? "Preparing…" : state === "error" ? "Certificate unavailable" : "Custody certificate"}
    </button>
  );
}

function needsMyAction(transfer: Transfer): boolean {
  if (isFinished(transfer)) return false;
  const isSender = transfer.viewer_role === "SENDER";

  if (isSender) {
    return ["REQUESTED", "UNITS_RESERVED", "SHIPMENT_CREATED", "AWB_ASSIGNED", "PICKUP_OTP_REQUIRED"]
      .includes(transfer.status);
  }
  return (
    ["IN_TRANSIT", "ARRIVED", "DELIVERY_OTP_REQUIRED"].includes(transfer.status) &&
    transfer.delivery_otp.issued
  );
}
