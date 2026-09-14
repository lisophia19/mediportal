# Tests for PostgresSchedulingProvider (spec §5.5a).
from datetime import date, timedelta

import pytest

from app.providers.postgres_scheduling import PostgresSchedulingProvider
from app.providers.scheduling import SlotUnavailableError

from .factories import make_doctor, make_patient, make_practice, make_slot, make_term


@pytest.fixture
def provider():
    return PostgresSchedulingProvider()


def test_get_available_slots_orders_and_caps(db, provider):
    doctor = make_doctor(db)
    practice = make_practice(db)
    from datetime import datetime, timezone

    later = make_slot(doctor, practice, db, start_time=datetime.now(timezone.utc) + timedelta(days=2))
    sooner = make_slot(doctor, practice, db, start_time=datetime.now(timezone.utc) + timedelta(days=1))
    make_slot(doctor, practice, db, start_time=datetime.now(timezone.utc) + timedelta(days=3))

    results = provider.get_available_slots(
        doctor_id=doctor.id,
        practice_id=None,
        appointment_type=None,
        date_from=date.today(),
        date_to=date.today() + timedelta(days=14),
        limit=2,
    )

    assert len(results) == 2
    assert results[0].slot_id == sooner.id
    assert results[1].slot_id == later.id


def test_get_available_slots_excludes_booked(db, provider):
    doctor = make_doctor(db)
    practice = make_practice(db)
    make_slot(doctor, practice, db, status="booked")

    results = provider.get_available_slots(
        doctor_id=doctor.id,
        practice_id=None,
        appointment_type=None,
        date_from=date.today(),
        date_to=date.today() + timedelta(days=14),
        limit=5,
    )

    assert results == []


def test_get_available_slots_skips_starts_without_enough_contiguous_capacity(db, provider):
    doctor = make_doctor(db)
    practice = make_practice(db)
    # A lone 20-min slot, no contiguous follow-on -- not offerable for a
    # 40-min appointment type.
    lonely = make_slot(doctor, practice, db)
    # A real contiguous pair, further out -- offerable.
    pair_start = make_slot(
        doctor, practice, db, start_time=lonely.start_time + timedelta(days=1)
    )
    make_slot(doctor, practice, db, start_time=pair_start.end_time)

    results = provider.get_available_slots(
        doctor_id=doctor.id,
        practice_id=None,
        appointment_type="new_patient_consult",
        date_from=date.today(),
        date_to=date.today() + timedelta(days=14),
        limit=5,
    )

    assert [r.slot_id for r in results] == [pair_start.id]
    assert results[0].duration_minutes == 40


def test_book_slot_success(db, provider):
    doctor = make_doctor(db)
    practice = make_practice(db)
    # follow_up is a single 20-min grid slot -- duration isn't what this
    # test is about, so keep the fixture to one slot per offer.
    term = make_term(db, default_appointment_type="follow_up")
    patient = make_patient(db)
    slot = make_slot(doctor, practice, db)

    booking = provider.book_slot(slot.id, patient.id, term.id, call_id=None)

    assert booking.slot_id == slot.id
    assert booking.patient_id == patient.id
    assert booking.doctor_id == doctor.id

    db.refresh(slot)
    assert slot.status == "booked"


def test_book_slot_reserves_all_contiguous_grid_cells_for_a_multi_slot_type(db, provider):
    """Regression test for the double-booking bug: a 40-minute
    new_patient_consult must reserve BOTH contiguous 20-minute grid slots
    (spec §8.2), not just the one the caller named -- otherwise the second
    slot stays "open" and can be double-booked to someone else."""
    doctor = make_doctor(db)
    practice = make_practice(db)
    term = make_term(db, default_appointment_type="new_patient_consult")
    patient = make_patient(db)
    first_slot = make_slot(doctor, practice, db)
    second_slot = make_slot(doctor, practice, db, start_time=first_slot.end_time)

    booking = provider.book_slot(first_slot.id, patient.id, term.id, call_id=None)

    assert booking.end_time == second_slot.end_time
    db.refresh(first_slot)
    db.refresh(second_slot)
    assert first_slot.status == "booked"
    assert second_slot.status == "booked"  # the bug: this used to stay "open"

    # And now the second slot can't be double-booked out from under it.
    other_patient = make_patient(db, first_name="Someone", last_name="Else")
    with pytest.raises(SlotUnavailableError):
        provider.book_slot(second_slot.id, other_patient.id, term.id, call_id=None)


def test_book_slot_raises_when_contiguous_slot_missing(db, provider):
    """A multi-slot appointment type with no open follow-on grid cell must
    be refused, not silently booked into just the first 20 minutes."""
    doctor = make_doctor(db)
    practice = make_practice(db)
    term = make_term(db, default_appointment_type="new_patient_consult")
    patient = make_patient(db)
    # No second contiguous slot exists -- only a lone 20-min slot.
    slot = make_slot(doctor, practice, db)

    with pytest.raises(SlotUnavailableError):
        provider.book_slot(slot.id, patient.id, term.id, call_id=None)

    db.refresh(slot)
    assert slot.status == "open"  # never partially booked


def test_book_slot_raises_on_race(db, provider):
    doctor = make_doctor(db)
    practice = make_practice(db)
    term = make_term(db, default_appointment_type="follow_up")
    patient = make_patient(db)
    slot = make_slot(doctor, practice, db)
    # Simulate another caller winning the race between offer and booking.
    slot.status = "booked"
    db.flush()

    other_open = make_slot(doctor, practice, db, start_time=slot.start_time + timedelta(days=1))

    with pytest.raises(SlotUnavailableError) as exc_info:
        provider.book_slot(slot.id, patient.id, term.id, call_id=None)

    alternates = exc_info.value.alternates
    assert any(a.slot_id == other_open.id for a in alternates)


def test_book_slot_missing_slot_raises(db, provider):
    term = make_term(db)
    patient = make_patient(db)

    with pytest.raises(SlotUnavailableError) as exc_info:
        provider.book_slot(999999, patient.id, term.id, call_id=None)

    assert exc_info.value.alternates == []
