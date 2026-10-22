import datetime
import json
from sqlalchemy import Column, String, DateTime, Integer, Float, ForeignKey, Text
from database import Base

# ── Transfer state machine ───────────────────────────────────────────────────
# A transfer always has exactly one SENDER (the facility giving units) and one
# RECEIVER (the facility getting them). Which side opened the request depends
# on the direction:
#
#   SHORTAGE_PULL  — the receiver asks the sender for units it is short of.
#   WASTAGE_PUSH   — the sender offers units that would otherwise expire.
#
# Either way the sender authorises, the sender's OTP releases the shipment, and
# the receiver's OTP closes it.
TRANSFER_STATES = [
    "REQUESTED",             # opened, waiting on the sender's decision
    "ACCEPTED",              # sender authorised
    "UNITS_RESERVED",        # units locked out of the sender's usable pool
    "SHIPMENT_CREATED",      # courier order placed
    "AWB_ASSIGNED",          # courier returned an airway bill
    "PICKUP_OTP_REQUIRED",   # sender holds a pickup OTP for the rider
    "IN_TRANSIT",            # pickup verified, custody with the courier
    "ARRIVED",               # courier reached the receiver
    "DELIVERY_OTP_REQUIRED", # receiver must enter the sender's OTP
    "TRANSFER_COMPLETED",    # settled on both ledgers
    "DECLINED",              # sender refused
    "CANCELLED",             # withdrawn by the opener
    "FAILED",                # courier dispatch failed, units released
]

TERMINAL_STATES = {"TRANSFER_COMPLETED", "DECLINED", "CANCELLED", "FAILED"}


class Transfer(Base):
    """A unit movement between two facilities, persisted end to end."""

    __tablename__ = "transfers"

    id = Column(String, primary_key=True, index=True)
    direction = Column(String, nullable=False, default="SHORTAGE_PULL")  # SHORTAGE_PULL | WASTAGE_PUSH

    source_bank_id = Column(String, ForeignKey("banks.id"), nullable=False, index=True)
    destination_bank_id = Column(String, ForeignKey("banks.id"), nullable=False, index=True)
    opened_by_bank_id = Column(String, nullable=False)
    opened_by_user_id = Column(String, nullable=True)

    component_type = Column(String, default="SDP")  # SDP | RDP
    blood_group = Column(String, nullable=True)
    units = Column(Integer, nullable=False, default=1)
    priority = Column(String, default="URGENT")  # ROUTINE | URGENT | EMERGENCY
    reason = Column(Text, nullable=True)

    status = Column(String, nullable=False, default="REQUESTED", index=True)
    decline_reason = Column(Text, nullable=True)

    # Routing, resolved from the two facilities' coordinates
    distance_km = Column(Float, nullable=True)
    eta_minutes = Column(Float, nullable=True)
    route_geometry_json = Column(Text, nullable=True)  # GeoJSON LineString
    route_provider = Column(String, nullable=True)

    # Courier / Shiprocket
    transport_provider = Column(String, nullable=True)   # canonical adapter key
    provider_label = Column(String, nullable=True)       # what the adapter called itself
    provider_order_id = Column(String, nullable=True, index=True)
    provider_shipment_id = Column(String, nullable=True)
    awb_code = Column(String, nullable=True, index=True)
    courier_name = Column(String, nullable=True)
    rider_name = Column(String, nullable=True)
    rider_mobile = Column(String, nullable=True)
    vehicle_number = Column(String, nullable=True)

    # Last known courier position, written by webhook or the tracking poller
    last_lat = Column(Float, nullable=True)
    last_lng = Column(Float, nullable=True)
    last_location_at = Column(DateTime, nullable=True)
    last_location_source = Column(String, nullable=True)  # shiprocket | simulated

    # Which inventory units were locked for this transfer
    reserved_unit_ids_json = Column(Text, nullable=True)

    # Monotonic counter — clients poll this to detect changes cheaply
    revision = Column(Integer, nullable=False, default=1)

    # Chain-of-custody certificate, written when the transfer settles
    certificate_store = Column(String, nullable=True)  # s3 | supabase
    certificate_key = Column(String, nullable=True)
    certificate_sha256 = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    accepted_at = Column(DateTime, nullable=True)
    dispatched_at = Column(DateTime, nullable=True)
    arrived_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)

    @property
    def route_geometry(self):
        if not self.route_geometry_json:
            return None
        try:
            return json.loads(self.route_geometry_json)
        except (ValueError, TypeError):
            return None

    @property
    def reserved_unit_ids(self) -> list:
        if not self.reserved_unit_ids_json:
            return []
        try:
            return json.loads(self.reserved_unit_ids_json)
        except (ValueError, TypeError):
            return []

    def touch(self, status: str = None) -> None:
        """Advance the revision so pollers pick the change up."""
        if status:
            self.status = status
        self.revision = (self.revision or 0) + 1
        self.updated_at = datetime.datetime.utcnow()


class TransferEvent(Base):
    """Append-only timeline behind the Flipkart-style tracking view."""

    __tablename__ = "transfer_events"

    id = Column(String, primary_key=True)
    transfer_id = Column(String, ForeignKey("transfers.id"), nullable=False, index=True)
    status = Column(String, nullable=False)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    actor_bank_id = Column(String, nullable=True)
    actor_user_id = Column(String, nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    location_name = Column(String, nullable=True)
    source = Column(String, default="system")  # system | shiprocket | operator
    occurred_at = Column(DateTime, default=datetime.datetime.utcnow)


# ── Legacy advisory tables, still used by the recommendation surface ─────────

class TransferOpportunity(Base):
    __tablename__ = "transfer_opportunities"

    id = Column(String, primary_key=True)
    source_bank_id = Column(String, nullable=False)
    source_bank_name = Column(String, nullable=True)
    destination_bank_id = Column(String, ForeignKey("banks.id"), nullable=False)
    component_type = Column(String, default="SDP")
    potential_quantity = Column(Integer, nullable=False)
    source_freshness_hours = Column(Float, default=12.0)
    destination_risk_score = Column(Float, default=0.5)
    expiry_window_hours = Column(Float, default=72.0)
    status = Column(String, default="OPEN")  # OPEN | REVIEWED | OFFERED | EXPIRED | CLOSED
    reason_summary = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class TransferOffer(Base):
    __tablename__ = "transfer_offers"

    id = Column(String, primary_key=True)
    opportunity_id = Column(String, ForeignKey("transfer_opportunities.id"), nullable=False)
    source_bank_id = Column(String, nullable=False)
    destination_bank_id = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False)
    status = Column(String, default="PENDING")  # PENDING | ACCEPTED | DECLINED | COMPLETED | CANCELLED
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
