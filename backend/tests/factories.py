# Minimal fixture-building helpers shared by this track's tests -- keeps
# each test focused on the behavior under test instead of re-deriving
# doctor/practice/term/slot setup every time.
import itertools
from datetime import date, datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

_counter = itertools.count(1)

from app.extensions import db
from app.models import (
    Appointment,
    AppointmentSlot,
    Doctor,
    Patient,
    Practice,
    Term,
    TermEligibility,
    User,
)


def make_doctor(session=None, **overrides):
    session = session or db.session
    doctor = Doctor(
        first_name=overrides.get("first_name", "Test"),
        last_name=overrides.get("last_name", "Doctor"),
        degree="MD",
        specialty="Sports Medicine",
        active=True,
    )
    session.add(doctor)
    session.flush()
    return doctor


def make_practice(session=None, **overrides):
    session = session or db.session
    n = next(_counter)
    practice = Practice(
        name=overrides.get("name", f"Test Practice {n}"),
        address=overrides.get("address", f"{n} Test Way"),
        zip=overrides.get("zip", "11566"),
    )
    session.add(practice)
    session.flush()
    return practice


def make_term(session=None, **overrides):
    session = session or db.session
    term = Term(
        term=overrides.get("term", f"Test-Term-{next(_counter)}"),
        body_part="Hand/Wrist",
        category="Fracture",
        urgency=overrides.get("urgency"),
        patient_phrasing=[],
        default_appointment_type=overrides.get("default_appointment_type", "new_patient_consult"),
    )
    session.add(term)
    session.flush()
    return term


def make_eligibility(term, doctor, session=None, min_age=1, max_age=100):
    session = session or db.session
    row = TermEligibility(term_id=term.id, doctor_id=doctor.id, min_age=min_age, max_age=max_age)
    session.add(row)
    session.flush()
    return row


def make_patient(session=None, **overrides):
    session = session or db.session
    patient = Patient(
        first_name=overrides.get("first_name", "Jane"),
        last_name=overrides.get("last_name", "Doe"),
        date_of_birth=overrides.get("date_of_birth", date(1990, 1, 1)),
        phone=overrides.get("phone", "555-0100"),
        home_zip=overrides.get("home_zip", "11566"),
    )
    session.add(patient)
    session.flush()
    return patient


def make_slot(doctor, practice, session=None, start_time=None, status="open"):
    session = session or db.session
    start_time = start_time or (datetime.now(timezone.utc) + timedelta(days=1))
    slot = AppointmentSlot(
        doctor_id=doctor.id,
        practice_id=practice.id,
        start_time=start_time,
        end_time=start_time + timedelta(minutes=20),
        status=status,
    )
    session.add(slot)
    session.flush()
    return slot


def make_user(session=None, email="dashboard@test.example", password="test-password"):
    session = session or db.session
    user = User(email=email, password_hash=generate_password_hash(password))
    session.add(user)
    session.flush()
    return user


def make_appointment(patient, slot, term, session=None, call_id=None):
    session = session or db.session
    appointment = Appointment(
        patient_id=patient.id,
        slot_id=slot.id,
        term_id=term.id,
        appointment_type=term.default_appointment_type,
        call_id=call_id,
    )
    session.add(appointment)
    session.flush()
    return appointment
