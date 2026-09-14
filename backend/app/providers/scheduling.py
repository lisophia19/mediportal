# The swap point for a real EHR later (spec §5.5a). §5.5/§5.6 routes are
# thin wrappers over this interface, never direct database access -- so
# swapping in a real EHR later touches only this module and a
# SCHEDULING_PROVIDER-selected implementation, never the Flask routes or the
# Vogent flow.
#
# Phase 0: interface + dataclasses only. PostgresSchedulingProvider (the
# baseline implementation, querying/writing the real seeded
# appointment_slots/appointments tables) is Phase 1.
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime


@dataclass
class SlotResult:
    slot_id: int
    doctor_id: int
    practice_id: int
    practice_name: str
    start_time: datetime
    end_time: datetime
    duration_minutes: int


@dataclass
class BookingResult:
    appointment_id: int
    slot_id: int
    patient_id: int
    term_id: int
    doctor_id: int
    practice_id: int
    start_time: datetime
    end_time: datetime


class SlotUnavailableError(Exception):
    """Raised by book_slot when the slot is no longer open -- the route
    layer catches this and returns 409 with alternates (spec §5.6)."""

    def __init__(self, alternates: list[SlotResult] | None = None):
        self.alternates = alternates or []
        super().__init__(f"slot unavailable, {len(self.alternates)} alternate(s) offered")


class SchedulingProvider(ABC):
    @abstractmethod
    def get_available_slots(
        self,
        doctor_id: int,
        practice_id: int | None,
        appointment_type: str | None,
        date_from: date,
        date_to: date,
        limit: int,
    ) -> list[SlotResult]:
        """Open slots for a doctor within [date_from, date_to], optionally
        narrowed to one practice and/or a duration implied by
        appointment_type. Ordered soonest-first, capped at limit."""
        raise NotImplementedError

    @abstractmethod
    def book_slot(
        self, slot_id: int, patient_id: int, term_id: int, call_id: int | None
    ) -> BookingResult:
        """Books the slot for the patient, atomically. Raises
        SlotUnavailableError(alternates=[...]) if the slot is no longer
        open by the time this runs."""
        raise NotImplementedError
