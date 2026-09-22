# Operational schema -- spec §4 (docs/design/spec-ortho-baseline-demo.md).
# Table order below matches the spec's own dependency order: routing tables
# (§4.1), patient/scheduling tables (§4.2), call capture tables (§4.3), plus
# two small lookup tables (§8.3 ZIP centroids, §5.9 directory redirect rules)
# that the spec describes but doesn't formally tabulate.
#
# Status columns use plain text + CHECK constraints rather than native
# Postgres ENUM types -- adding a status value later is a one-line constraint
# migration instead of an ALTER TYPE, and this is a small demo app, not a
# system where enum-level type safety earns its keep.
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from .extensions import db


def _utcnow():
    return datetime.now(timezone.utc)


# --- §4.1 Routing tables ----------------------------------------------------


class Doctor(db.Model):
    __tablename__ = "doctors"

    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.Text, nullable=False)
    last_name = db.Column(db.Text, nullable=False)
    degree = db.Column(db.Text)
    # Display only -- never a routing key. See spec §4.1 / §3.2.
    specialty = db.Column(db.Text)
    npi = db.Column(db.Text)
    active = db.Column(db.Boolean, nullable=False, default=True)
    # Inferred from first name at seed time (gender-guesser), nullable --
    # a caller's stated gender preference is a real, legitimate routing
    # filter, but an unrecognized/ambiguous name means "unknown", not a
    # guess forced into one bucket.
    gender = db.Column(db.Text)

    doctor_practices = db.relationship("DoctorPractice", back_populates="doctor")
    term_eligibility = db.relationship("TermEligibility", back_populates="doctor")

    __table_args__ = (
        CheckConstraint("gender IN ('male', 'female')", name="ck_doctor_gender"),
    )


class Practice(db.Model):
    __tablename__ = "practices"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.Text, nullable=False)
    address = db.Column(db.Text, nullable=False)
    suite = db.Column(db.Text)
    zip = db.Column(db.Text, nullable=False)
    region = db.Column(db.Text)
    # Seeded from the ZIP centroid table (§8.3), not geocoded live.
    latitude = db.Column(db.Numeric)
    longitude = db.Column(db.Numeric)
    main_phone = db.Column(db.Text)
    # From the "MRI Onsite" column (spec: onsite-service matching). Only MRI is modeled 
    # -- Xray/PT/OT. onsite flags exist in the same sheet but are unused so far.
    has_mri = db.Column(db.Boolean, nullable=False, default=False)

    __table_args__ = (UniqueConstraint("name", "address", name="uq_practice_name_address"),)


class DoctorPractice(db.Model):
    __tablename__ = "doctor_practices"

    id = db.Column(db.Integer, primary_key=True)
    doctor_id = db.Column(db.Integer, db.ForeignKey("doctors.id"), nullable=False)
    practice_id = db.Column(db.Integer, db.ForeignKey("practices.id"), nullable=False)
    is_primary = db.Column(db.Boolean, nullable=False, default=False)

    doctor = db.relationship("Doctor", back_populates="doctor_practices")
    practice = db.relationship("Practice")

    __table_args__ = (
        UniqueConstraint("doctor_id", "practice_id", name="uq_doctor_practice"),
    )


class Term(db.Model):
    __tablename__ = "terms"

    id = db.Column(db.Integer, primary_key=True)
    term = db.Column(db.Text, nullable=False, unique=True)
    body_part = db.Column(db.Text)
    category = db.Column(db.Text)
    urgency = db.Column(db.Text)
    # Hand-written lay synonyms (spec §8.1) -- populated for a handful of
    # demo terms only, empty array for the rest.
    patient_phrasing = db.Column(ARRAY(db.Text), nullable=False, default=list)
    default_appointment_type = db.Column(db.Text, nullable=False)

    term_eligibility = db.relationship("TermEligibility", back_populates="term")


class TermEligibility(db.Model):
    __tablename__ = "term_eligibility"

    id = db.Column(db.Integer, primary_key=True)
    term_id = db.Column(db.Integer, db.ForeignKey("terms.id"), nullable=False)
    doctor_id = db.Column(db.Integer, db.ForeignKey("doctors.id"), nullable=False)
    # From the sheet cell: Y/None -> (1, 100), "N+" -> (N, 100), "N-M" -> (N, M).
    min_age = db.Column(db.Integer, nullable=False)
    max_age = db.Column(db.Integer, nullable=False)

    term = db.relationship("Term", back_populates="term_eligibility")
    doctor = db.relationship("Doctor", back_populates="term_eligibility")

    __table_args__ = (
        UniqueConstraint("term_id", "doctor_id", name="uq_term_eligibility_term_doctor"),
    )


class DirectoryEntry(db.Model):
    """The practice's internal call-routing directory (§4.1) -- seeded from
    the full `Directory` sheet, not scoped to LIBJ, because a dead-end
    redirect target is often an unrelated department (spec §5.9)."""

    __tablename__ = "directory_entries"

    id = db.Column(db.Integer, primary_key=True)
    contact = db.Column(db.Text, nullable=False)
    location = db.Column(db.Text, nullable=False)
    group_name = db.Column(db.Text, nullable=False)
    desk_number = db.Column(db.Text)
    email = db.Column(db.Text)


class DirectoryRedirectRule(db.Model):
    """Hand-curated category/body_part -> directory_entries.contact mapping
    (spec §5.9). Deliberately small and hand-maintained, not derived: the
    Directory sheet's department names share no vocabulary with `terms` to
    match on automatically. At least one of category/body_part is set;
    a row matches when its non-null fields all match the term being
    redirected."""

    __tablename__ = "directory_redirect_rules"

    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.Text)
    body_part = db.Column(db.Text)
    contact = db.Column(db.Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "category IS NOT NULL OR body_part IS NOT NULL",
            name="ck_redirect_rule_has_match_field",
        ),
    )


class ZipCentroid(db.Model):
    """Straight-line-distance ZIP lookup (§8.3) -- no external geocoding API.
    Covers the 5 real practice ZIPs plus a spread of Long Island caller ZIPs
    for realistic proximity-ranking demos."""

    __tablename__ = "zip_centroids"

    zip = db.Column(db.Text, primary_key=True)
    latitude = db.Column(db.Numeric, nullable=False)
    longitude = db.Column(db.Numeric, nullable=False)
    label = db.Column(db.Text)


# --- §4.2 Patient and scheduling tables -------------------------------------


class Patient(db.Model):
    __tablename__ = "patients"

    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.Text, nullable=False)
    last_name = db.Column(db.Text, nullable=False)
    date_of_birth = db.Column(db.Date, nullable=False)
    phone = db.Column(db.Text)
    home_zip = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        # Backs §5.3's lookup: case-insensitive last name + exact DOB.
        Index(
            "ix_patients_last_name_lower_dob",
            db.text("lower(last_name)"),
            "date_of_birth",
        ),
    )


class AppointmentSlot(db.Model):
    __tablename__ = "appointment_slots"

    id = db.Column(db.Integer, primary_key=True)
    doctor_id = db.Column(db.Integer, db.ForeignKey("doctors.id"), nullable=False)
    practice_id = db.Column(db.Integer, db.ForeignKey("practices.id"), nullable=False)
    start_time = db.Column(db.DateTime(timezone=True), nullable=False)
    end_time = db.Column(db.DateTime(timezone=True), nullable=False)
    status = db.Column(db.Text, nullable=False, default="open")
    hold_expires_at = db.Column(db.DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'held', 'booked')", name="ck_appointment_slot_status"
        ),
        Index("ix_appointment_slots_doctor_status_start", "doctor_id", "status", "start_time"),
        # A doctor can't be booked twice at the same start time.
        Index(
            "uq_appointment_slots_doctor_start_booked",
            "doctor_id",
            "start_time",
            unique=True,
            postgresql_where=db.text("status = 'booked'"),
        ),
    )


class Appointment(db.Model):
    __tablename__ = "appointments"

    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patients.id"), nullable=False)
    slot_id = db.Column(
        db.Integer, db.ForeignKey("appointment_slots.id"), nullable=False, unique=True
    )
    term_id = db.Column(db.Integer, db.ForeignKey("terms.id"), nullable=False)
    # Copied from terms.default_appointment_type at booking time.
    appointment_type = db.Column(db.Text, nullable=False)
    status = db.Column(db.Text, nullable=False, default="scheduled")
    # use_alter: appointments <-> calls is a genuine FK cycle (an appointment
    # references its call, a call references its resulting appointment), so
    # this FK is added via a deferred ALTER TABLE after both tables exist.
    call_id = db.Column(
        db.Integer, db.ForeignKey("calls.id", use_alter=True, name="fk_appointments_call_id")
    )
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        CheckConstraint("status IN ('scheduled', 'cancelled')", name="ck_appointment_status"),
    )


# --- Imaging (spec: onsite-service matching / prerequisite follow-ups) -------
#
# Imaging is booked against a practice's MACHINE, not a physician's calendar
# -- AppointmentSlot.doctor_id is nullable=False specifically because a
# doctor visit always has one, which an MRI never does. Modeled as separate
# slot/appointment tables rather than making doctor_id nullable on the
# existing ones, so a doctor-visit query can never accidentally match an
# imaging row (and vice versa) without an explicit join.


class ImagingSlot(db.Model):
    __tablename__ = "imaging_slots"

    id = db.Column(db.Integer, primary_key=True)
    practice_id = db.Column(db.Integer, db.ForeignKey("practices.id"), nullable=False)
    modality = db.Column(db.Text, nullable=False)  # "MRI" today; Xray onsite exists in the source data, unused so far
    start_time = db.Column(db.DateTime(timezone=True), nullable=False)
    end_time = db.Column(db.DateTime(timezone=True), nullable=False)
    status = db.Column(db.Text, nullable=False, default="open")

    __table_args__ = (
        CheckConstraint("status IN ('open', 'held', 'booked')", name="ck_imaging_slot_status"),
        Index("ix_imaging_slots_practice_modality_status_start", "practice_id", "modality", "status", "start_time"),
        # Mirrors appointment_slots' uq_appointment_slots_doctor_start_booked:
        # a practice's machine (one per modality, per the seed data) can't be
        # double-booked across two different slot rows for the same instant.
        Index(
            "uq_imaging_slots_practice_modality_start_booked",
            "practice_id",
            "modality",
            "start_time",
            unique=True,
            postgresql_where=db.text("status = 'booked'"),
        ),
    )


class ImagingAppointment(db.Model):
    __tablename__ = "imaging_appointments"

    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patients.id"), nullable=False)
    slot_id = db.Column(db.Integer, db.ForeignKey("imaging_slots.id"), nullable=False, unique=True)
    call_id = db.Column(
        db.Integer, db.ForeignKey("calls.id", use_alter=True, name="fk_imaging_appointments_call_id")
    )
    status = db.Column(db.Text, nullable=False, default="scheduled")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        CheckConstraint("status IN ('scheduled', 'cancelled')", name="ck_imaging_appointment_status"),
    )


class PatientPrerequisite(db.Model):
    """A clinical prerequisite gating a returning patient's next visit (e.g.
    "needs an MRI before their follow-up can be booked"). Synthetic/demo
    data -- there is no real system of record for this yet (spec §4 notes),
    seeded onto one demo patient the same way other synthetic call/booking
    history is seeded."""

    __tablename__ = "patient_prerequisites"

    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patients.id"), nullable=False)
    # The follow-up term this prerequisite blocks -- not enforced against
    # what the caller actually says on the call, just what's on file.
    term_id = db.Column(db.Integer, db.ForeignKey("terms.id"), nullable=False)
    requirement = db.Column(db.Text, nullable=False)  # "MRI" today; a plain label, not an enum -- only one kind exists yet
    satisfied = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("patient_id", "term_id", "requirement", name="uq_patient_prerequisite"),
    )


# --- §4.3 Call capture tables ------------------------------------------------


class Call(db.Model):
    __tablename__ = "calls"

    id = db.Column(db.Integer, primary_key=True)
    vogent_call_id = db.Column(db.Text, nullable=False, unique=True)
    # Confirmed ANI from Vogent's {{toNumber}} -- distinct from patients.phone,
    # which is the number on file and may differ (spec §4.3 / §5.3).
    caller_phone = db.Column(db.Text)
    started_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    ended_at = db.Column(db.DateTime(timezone=True))
    status = db.Column(db.Text, nullable=False, default="in_progress")
    patient_id = db.Column(db.Integer, db.ForeignKey("patients.id"))
    appointment_id = db.Column(db.Integer, db.ForeignKey("appointments.id"))
    imaging_appointment_id = db.Column(db.Integer, db.ForeignKey("imaging_appointments.id"))
    transcript = db.Column(JSONB, nullable=False, default=list)
    matched_term_id = db.Column(db.Integer, db.ForeignKey("terms.id"))
    raw_complaint = db.Column(db.Text)

    __table_args__ = (
        CheckConstraint(
            "status IN ('in_progress', 'scheduled', 'no_match', 'no_slots', "
            "'abandoned', 'failed')",
            name="ck_call_status",
        ),
    )


class User(db.Model):
    """Dashboard auth only -- no signup, users are seeded (spec §9)."""

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.Text, nullable=False, unique=True)
    password_hash = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
