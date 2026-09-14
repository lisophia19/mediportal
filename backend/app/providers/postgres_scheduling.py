# Baseline SchedulingProvider (spec §5.5a) -- queries/writes the real seeded
# appointment_slots/appointments tables directly through SQLAlchemy. Not a
# mock in the sense of being fake: a fully working booking flow against a
# real Postgres database, just not a real EHR yet. A future
# RealEHRSchedulingProvider drops in behind the same interface with zero
# changes to the routes that call it.
from datetime import date, datetime, time, timedelta, timezone

from ..extensions import db
from ..models import AppointmentSlot, Appointment, Practice, Term
from .scheduling import (
    DEFAULT_SLOT_LIMIT,
    NORMAL_WINDOW_DAYS,
    BookingResult,
    SchedulingProvider,
    SlotResult,
    SlotUnavailableError,
)

# Slot duration by appointment type (spec §8.2) -- not specified by the
# practice, our assumption, documented here and in seed/availability.py.
DURATION_BY_APPOINTMENT_TYPE = {
    "new_patient_consult": 40,
    "follow_up": 20,
    "urgent": 30,
}
DEFAULT_DURATION_MINUTES = 20

# Slots are generated on a uniform 20-minute grid (§8.2), which doesn't
# divide evenly into "urgent"'s 30 minutes -- rounding up means an urgent
# booking actually reserves 40 minutes, a deliberate over-reservation rather
# than double-booking the trailing 10 minutes. §8.2's own example ("a
# 40-minute appointment consumes two adjacent grid slots") is the basis for
# this: one row per appointment type is not enough, the grid must be
# reserved in contiguous multiples.
_SLOT_GRID_MINUTES = 20
SLOTS_NEEDED_BY_APPOINTMENT_TYPE = {
    appointment_type: -(-minutes // _SLOT_GRID_MINUTES)  # ceiling division
    for appointment_type, minutes in DURATION_BY_APPOINTMENT_TYPE.items()
}


def _slots_needed(appointment_type):
    return SLOTS_NEEDED_BY_APPOINTMENT_TYPE.get(appointment_type, 1)


class PostgresSchedulingProvider(SchedulingProvider):
    def get_available_slots(
        self, doctor_id, practice_id, appointment_type, date_from, date_to, limit
    ):
        window_start = datetime.combine(date_from, time.min, tzinfo=timezone.utc)
        window_end = datetime.combine(date_to, time.max, tzinfo=timezone.utc)

        query = (
            db.session.query(AppointmentSlot, Practice.name)
            .join(Practice, AppointmentSlot.practice_id == Practice.id)
            .filter(
                AppointmentSlot.doctor_id == doctor_id,
                AppointmentSlot.status == "open",
                AppointmentSlot.start_time >= window_start,
                AppointmentSlot.start_time <= window_end,
            )
        )
        if practice_id is not None:
            query = query.filter(AppointmentSlot.practice_id == practice_id)

        # No DB-side limit: a slot only qualifies as an offer if it's the
        # start of a long-enough contiguous run of open grid cells, so we
        # need to see the full open set for this doctor/window to find
        # `limit` valid starts, not just the first `limit` rows.
        rows = query.order_by(AppointmentSlot.start_time.asc()).all()

        duration_minutes = (
            DURATION_BY_APPOINTMENT_TYPE.get(appointment_type, DEFAULT_DURATION_MINUTES)
            if appointment_type
            else None
        )
        slots_needed = _slots_needed(appointment_type) if appointment_type else 1

        # (practice_id, start_time) -> (slot, practice_name), for O(1)
        # "is the next grid cell open" lookups while scanning for runs.
        by_key = {(slot.practice_id, slot.start_time): (slot, name) for slot, name in rows}

        def _run_end_time(slot):
            """Walks `slots_needed` contiguous grid cells from `slot`;
            returns the final cell's end_time, or None if the chain breaks
            (a gap, or the next cell isn't open)."""
            current = slot
            for _ in range(slots_needed - 1):
                nxt = by_key.get((current.practice_id, current.end_time))
                if nxt is None:
                    return None
                current = nxt[0]
            return current.end_time

        results = []
        for slot, practice_name in rows:
            if len(results) >= limit:
                break
            run_end = _run_end_time(slot)
            if run_end is None:
                continue
            actual_minutes = int((run_end - slot.start_time).total_seconds() // 60)
            results.append(
                SlotResult(
                    slot_id=slot.id,
                    doctor_id=slot.doctor_id,
                    practice_id=slot.practice_id,
                    practice_name=practice_name,
                    start_time=slot.start_time,
                    end_time=run_end,
                    duration_minutes=duration_minutes or actual_minutes,
                )
            )
        return results

    def _lock_contiguous_run(self, start_slot, needed):
        """Locks `needed` contiguous grid cells starting at start_slot (which
        the caller has already locked), re-verifying under FOR UPDATE that
        each is still open -- the read-only scan in get_available_slots can
        be stale by the time a booking actually lands. Returns the list of
        locked slots, or None if the chain is broken or any cell is no
        longer open."""
        run = [start_slot]
        current = start_slot
        for _ in range(needed - 1):
            next_slot = (
                AppointmentSlot.query.filter_by(
                    doctor_id=current.doctor_id,
                    practice_id=current.practice_id,
                    start_time=current.end_time,
                )
                .with_for_update()
                .first()
            )
            if next_slot is None or next_slot.status != "open":
                return None
            run.append(next_slot)
            current = next_slot
        return run

    def _alternates_for(self, doctor_id, practice_id, appointment_type):
        return self.get_available_slots(
            doctor_id=doctor_id,
            practice_id=practice_id,
            appointment_type=appointment_type,
            date_from=date.today(),
            date_to=date.today() + timedelta(days=NORMAL_WINDOW_DAYS),
            limit=DEFAULT_SLOT_LIMIT,
        )

    def book_slot(self, slot_id, patient_id, term_id, call_id):
        term = db.session.get(Term, term_id)
        if term is None:
            raise ValueError(f"unknown term_id {term_id}")
        appointment_type = term.default_appointment_type
        slots_needed = _slots_needed(appointment_type)

        # Lock the row so a concurrent booking attempt for the same slot
        # blocks here rather than racing past the status check below.
        slot = AppointmentSlot.query.filter_by(id=slot_id).with_for_update().first()
        if slot is None or slot.status != "open":
            alternates = []
            if slot is not None:
                alternates = self._alternates_for(slot.doctor_id, slot.practice_id, appointment_type)
            raise SlotUnavailableError(alternates=alternates)

        run = self._lock_contiguous_run(slot, slots_needed)
        if run is None:
            # Long enough when offered, but no longer -- another booking
            # landed on a downstream grid cell in between. Same 409 shape as
            # the single-slot race above.
            alternates = self._alternates_for(slot.doctor_id, slot.practice_id, appointment_type)
            raise SlotUnavailableError(alternates=alternates)

        appointment = Appointment(
            patient_id=patient_id,
            slot_id=slot.id,
            term_id=term_id,
            appointment_type=appointment_type,
            call_id=call_id,
            status="scheduled",
        )
        for reserved in run:
            reserved.status = "booked"
        db.session.add(appointment)
        db.session.commit()

        return BookingResult(
            appointment_id=appointment.id,
            slot_id=slot.id,
            patient_id=patient_id,
            term_id=term_id,
            doctor_id=slot.doctor_id,
            practice_id=slot.practice_id,
            start_time=slot.start_time,
            end_time=run[-1].end_time,
        )
