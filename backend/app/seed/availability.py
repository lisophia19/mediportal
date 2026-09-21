# Synthetic availability generator (spec §8.2). We have no real physician
# schedule data -- this is an explicit baseline assumption, not an
# oversight. Re-runnable: wipes and regenerates only the rolling window
# each run, so the demo never goes stale and running twice doesn't
# duplicate rows.
import random
from datetime import date, datetime, time, timedelta, timezone

from ..extensions import db
from ..models import Appointment, AppointmentSlot, Call, ImagingAppointment, ImagingSlot, Term, TermEligibility

WINDOW_DAYS = 14
SLOT_GRID_MINUTES = 20
DAY_START = time(9, 0)
DAY_END = time(17, 0)
LUNCH_START = time(12, 0)
LUNCH_END = time(13, 0)

PRE_BOOKED_FRACTION = 0.6
NEAR_FULL_FRACTION = 0.97
NEAR_FULL_DOCTOR_LAST_NAME = "McGinley"  # smallest roster (one office) -- easiest to fill

# Weekly templates: which weekday(s) (Mon=0) a doctor is at which of their
# real seeded practices. Yu and Rana's templates match the illustrative
# examples in spec §8.2 exactly; the rest are reasonable choices within each
# doctor's real doctor_practices set so "this doctor is booked, try the next
# one" is reachable across different offices.
WEEKLY_TEMPLATES = {
    "Densen": {0: "Southampton", 1: "Riverhead", 3: "Smithtown", 4: "Southampton"},
    "Fracchia": {0: "Port Jefferson", 1: "Riverhead", 2: "Melville", 4: "Port Jefferson"},
    "Halsey": {0: "Southampton", 1: "Riverhead", 2: "Southampton", 3: "Riverhead", 4: "Southampton"},
    "Hubbell": {0: "Southampton", 1: "Southampton", 2: "Southampton", 3: "Southampton", 4: "Southampton"},
    "McGinley": {0: "Port Jefferson", 1: "Port Jefferson", 2: "Port Jefferson", 3: "Port Jefferson", 4: "Port Jefferson"},
    "Rana": {0: "Port Jefferson", 1: "Port Jefferson", 2: "Riverhead", 3: "Port Jefferson", 4: "Riverhead"},
    "Yu": {0: "Port Jefferson", 1: "Riverhead", 2: "Port Jefferson", 3: "Riverhead", 4: "Port Jefferson"},
}


def _grid_times(day):
    current = datetime.combine(day, DAY_START, tzinfo=timezone.utc)
    end = datetime.combine(day, DAY_END, tzinfo=timezone.utc)
    lunch_start = datetime.combine(day, LUNCH_START, tzinfo=timezone.utc)
    lunch_end = datetime.combine(day, LUNCH_END, tzinfo=timezone.utc)
    step = timedelta(minutes=SLOT_GRID_MINUTES)
    while current + step <= end:
        if not (lunch_start <= current < lunch_end):
            yield current, current + step
        current += step


def _wipe_rolling_window(today, slot_model, appointment_model, call_fk_column):
    """Deletes slots (and their dependent appointments) from today through
    the end of the rolling window, so re-running regenerates a clean window
    without touching historical bookings. Shared by the doctor-visit and
    imaging generators, which differ only in which models/FK column they
    touch -- both wipe otherwise-identical rolling windows."""
    window_end = today + timedelta(days=WINDOW_DAYS)
    stale_slot_ids = [
        row.id
        for row in slot_model.query.filter(
            slot_model.start_time >= today,
            slot_model.start_time < window_end,
        ).all()
    ]
    if stale_slot_ids:
        stale_appointment_ids = [
            row.id
            for row in appointment_model.query.filter(appointment_model.slot_id.in_(stale_slot_ids)).all()
        ]
        # A call may reference one of these appointments (the demo
        # "scheduled" call, spec §8.4) -- null that reference before
        # deleting so a standalone re-run of just this generator doesn't
        # hit the appointments<->calls FK cycle.
        if stale_appointment_ids:
            Call.query.filter(call_fk_column.in_(stale_appointment_ids)).update(
                {call_fk_column: None}, synchronize_session=False
            )
        appointment_model.query.filter(appointment_model.slot_id.in_(stale_slot_ids)).delete(
            synchronize_session=False
        )
        slot_model.query.filter(slot_model.id.in_(stale_slot_ids)).delete(synchronize_session=False)
        db.session.flush()


def generate_availability(doctors_by_name, practices_by_key, patients, rng=None):
    """Expands each routing-eligible doctor's weekly template over the
    rolling 14-day window, then pre-books a realistic fraction of the
    result. `doctors_by_name` keys are "Last, First" (spreadsheet.py's
    convention); only doctors present in WEEKLY_TEMPLATES get slots, which
    is exactly the 7 routing-eligible doctors (spec §8.1)."""
    rng = rng or random.Random(2026)
    today = date.today()
    _wipe_rolling_window(today, AppointmentSlot, Appointment, Call.appointment_id)

    practice_by_name = {name: practice for (name, _address), practice in practices_by_key.items()}

    for doctor_key, doctor in doctors_by_name.items():
        last_name = doctor_key.split(",")[0]
        template = WEEKLY_TEMPLATES.get(last_name)
        if not template:
            continue  # Arena/Marano/Nissen -- not routing-eligible, no schedule.

        # One query per doctor instead of one per booked slot: (term_id ->
        # default_appointment_type) for every term this doctor is eligible
        # for, reused across every booking decision below.
        appointment_type_by_term_id = dict(
            db.session.query(Term.id, Term.default_appointment_type)
            .join(TermEligibility, TermEligibility.term_id == Term.id)
            .filter(TermEligibility.doctor_id == doctor.id)
            .all()
        )
        eligible_term_ids = list(appointment_type_by_term_id)
        near_full = last_name == NEAR_FULL_DOCTOR_LAST_NAME

        for offset in range(WINDOW_DAYS):
            day = today + timedelta(days=offset)
            if day.weekday() >= 5 or day.weekday() not in template:
                continue
            practice = practice_by_name.get(template[day.weekday()])
            if not practice:
                continue

            # Add the whole day's slots, then flush once so every slot.id is
            # assigned in a single round trip instead of one flush per slot.
            day_slots = [
                AppointmentSlot(
                    doctor_id=doctor.id,
                    practice_id=practice.id,
                    start_time=start,
                    end_time=end,
                    status="open",
                )
                for start, end in _grid_times(day)
            ]
            db.session.add_all(day_slots)
            db.session.flush()

            book_fraction = NEAR_FULL_FRACTION if (near_full and offset < 7) else PRE_BOOKED_FRACTION
            for slot in day_slots:
                if eligible_term_ids and rng.random() < book_fraction:
                    slot.status = "booked"
                    term_id = rng.choice(eligible_term_ids)
                    patient = rng.choice(patients)
                    db.session.add(
                        Appointment(
                            patient_id=patient.id,
                            slot_id=slot.id,
                            term_id=term_id,
                            appointment_type=appointment_type_by_term_id[term_id],
                            status="scheduled",
                        )
                    )
    db.session.flush()


IMAGING_MODALITY = "MRI"
IMAGING_BOOK_FRACTION = 0.3  # lighter than doctor schedules -- imaging has more headroom in practice


def generate_imaging_availability(practices_by_key, patients, rng=None):
    """MRI slots for every MRI-capable practice (spec: onsite-service
    matching) over the same rolling window as doctor availability. Not tied
    to any doctor -- imaging is booked against the machine, not a
    physician's calendar (see models.py's ImagingSlot docstring). Weekdays
    only, same grid/hours as doctor schedules for simplicity; a lighter
    pre-booked fraction since there's no real basis for how busy a
    practice's MRI machine actually is."""
    rng = rng or random.Random(2026)
    today = date.today()
    _wipe_rolling_window(today, ImagingSlot, ImagingAppointment, Call.imaging_appointment_id)

    mri_practices = [p for p in practices_by_key.values() if p.has_mri]
    if not mri_practices or not patients:
        return

    for practice in mri_practices:
        for offset in range(WINDOW_DAYS):
            day = today + timedelta(days=offset)
            if day.weekday() >= 5:
                continue
            day_slots = [
                ImagingSlot(
                    practice_id=practice.id,
                    modality=IMAGING_MODALITY,
                    start_time=start,
                    end_time=end,
                    status="open",
                )
                for start, end in _grid_times(day)
            ]
            db.session.add_all(day_slots)
            db.session.flush()

            for slot in day_slots:
                if rng.random() < IMAGING_BOOK_FRACTION:
                    slot.status = "booked"
                    patient = rng.choice(patients)
                    db.session.add(
                        ImagingAppointment(patient_id=patient.id, slot_id=slot.id, status="scheduled")
                    )
    db.session.flush()
