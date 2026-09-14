# Synthetic patients, calls, dashboard user, directory-redirect rules, and
# hand-written patient_phrasing synonyms (spec §8.4, §5.9, §8.1). None of
# this comes from the spreadsheet -- it's created so the demo has realistic
# baseline data and dashboard content before the first live call.
from datetime import date, datetime, timezone

from werkzeug.security import generate_password_hash

from ..extensions import db
from ..models import Appointment, AppointmentSlot, Call, DirectoryRedirectRule, Patient, TermEligibility, User
from .geography import ZIP_CENTROIDS

_CALLER_ZIPS = [z for z in ZIP_CENTROIDS if z not in {"11968", "11901", "11787", "11777", "11747"}]

# (first, last, dob, phone, zip). The Smith/Smith pair sharing last name +
# DOB with different first names AND phone numbers is deliberate -- it
# exercises §5.3's layered disambiguation chain (Phase 1, not built here).
_PATIENTS = [
    ("Maria", "Rodriguez", date(1991, 4, 2), "516-555-0142", "11563"),
    ("James", "Whitfield", date(1978, 11, 19), "516-555-0198", "11566"),
    ("Linda", "Chen", date(2002, 6, 30), "631-555-0110", "11901"),
    ("Robert", "Guerrero", date(1965, 2, 14), "516-555-0176", "11530"),
    ("Patricia", "Nguyen", date(1988, 9, 5), "631-555-0133", "11743"),
    ("John", "Smith", date(1985, 5, 12), "516-555-0101", "11566"),
    ("Jonathan", "Smith", date(1985, 5, 12), "631-555-0177", "11758"),
    ("Barbara", "O'Connor", date(1972, 12, 24), "516-555-0154", "11801"),
    ("Michael", "Delgado", date(1995, 3, 8), "631-555-0121", "11772"),
    ("Susan", "Kowalski", date(1960, 7, 22), "516-555-0165", "11758"),
    ("David", "Petrov", date(2010, 1, 17), "631-555-0188", "11725"),
    ("Karen", "Ahmed", date(1983, 10, 3), "516-555-0143", "11706"),
    ("Christopher", "Russo", date(1992, 8, 27), "631-555-0199", "11751"),
    ("Nancy", "Fitzgerald", date(1955, 4, 11), "516-555-0187", "11563"),
    ("Daniel", "Park", date(1999, 2, 2), "631-555-0122", "11716"),
    ("Elizabeth", "Marsh", date(1980, 6, 15), "516-555-0134", "11801"),
    ("Anthony", "Ibrahim", date(1973, 9, 29), "631-555-0145", "11743"),
    ("Sandra", "Costa", date(1990, 12, 9), "516-555-0156", "11530"),
    ("Kevin", "Doyle", date(1968, 5, 20), "631-555-0167", "11772"),
    ("Michelle", "Levine", date(2005, 11, 30), "516-555-0178", "11566"),
]


def seed_patients():
    patients = []
    for first, last, dob, phone, zip_code in _PATIENTS:
        patient = Patient(
            first_name=first, last_name=last, date_of_birth=dob, phone=phone, home_zip=zip_code
        )
        db.session.add(patient)
        patients.append(patient)
    db.session.flush()
    return patients


def seed_users():
    user = User(
        email="frontdesk@libj-demo.example",
        password_hash=generate_password_hash("change-me-before-demo"),
    )
    db.session.add(user)
    db.session.flush()
    return user


def seed_directory_redirect_rules():
    """Hand-curated category/body_part -> directory contact mapping (spec
    §5.9) -- short and stable enough to hand-maintain, per the spec's own
    reasoning. Only a handful of rules exist; a term matching none of them
    falls back to the nearest practice's main line (§5.9), which is a
    legitimate outcome, not a gap to fill."""
    rules = [
        DirectoryRedirectRule(body_part="Back/Neck", contact="Spine"),
        DirectoryRedirectRule(category="Procedure", contact="Pain Management"),
        DirectoryRedirectRule(category="Lesion/Mass/Lump/Tumor", contact="Orthopedic Oncology"),
        DirectoryRedirectRule(category="Injury", body_part="General", contact="Concussion"),
    ]
    db.session.add_all(rules)
    db.session.flush()


# Hand-written for a handful of demo-likely terms (spec §8.1) -- not all 247.
_PATIENT_PHRASING = {
    "Fracture-Wrist": [
        "broke my wrist",
        "think my wrist is broken",
        "snapped my wrist",
        "fell on my wrist",
    ],
    "Sprain-Ankle": [
        "twisted my ankle",
        "rolled my ankle",
        "sprained ankle",
    ],
    "Fracture-Ankle": [
        "broke my ankle",
        "think my ankle is broken",
    ],
    "Pain-Back": [
        "my lower back is killing me",
        "lower back pain",
    ],
    "Herniated Disc": [
        "slipped disc",
        "bulging disc in my back",
    ],
    "Fracture-Finger": [
        "broke my finger",
        "jammed my finger and it's swollen",
    ],
    "Carpal Tunnel Syndrome": [
        "numbness and tingling in my hand",
        "my hand keeps falling asleep",
    ],
}


def apply_patient_phrasing(terms_by_name, warnings):
    for term_name, synonyms in _PATIENT_PHRASING.items():
        term = terms_by_name.get(term_name)
        if not term:
            warnings.append(f"patient_phrasing target term {term_name!r} not found in sheet")
            continue
        term.patient_phrasing = synonyms
    db.session.flush()


def _book_slot_for_demo(patient, term):
    """Finds an open slot for a doctor eligible for `term` and books it for
    `patient`, so the seeded "scheduled" demo call's transcript, patient, and
    matched term are actually consistent with each other -- not just
    whichever pre-booked appointment the availability generator's RNG
    happened to produce first."""
    eligible_doctor_ids = [
        row.doctor_id for row in TermEligibility.query.filter_by(term_id=term.id).all()
    ]
    slot = (
        AppointmentSlot.query.filter(
            AppointmentSlot.doctor_id.in_(eligible_doctor_ids),
            AppointmentSlot.status == "open",
        )
        .order_by(AppointmentSlot.start_time)
        .first()
    )
    if not slot:
        return None
    slot.status = "booked"
    appointment = Appointment(
        patient_id=patient.id,
        slot_id=slot.id,
        term_id=term.id,
        appointment_type=term.default_appointment_type,
        status="scheduled",
    )
    db.session.add(appointment)
    db.session.flush()
    return appointment


def seed_calls(patients, terms_by_name):
    """3-5 completed calls across different outcome statuses so the
    dashboard has content before the first live call (spec §8.4)."""
    patients_by_name = {(p.first_name, p.last_name): p for p in patients}

    calls = []

    scheduled_patient = patients_by_name.get(("Maria", "Rodriguez"))
    scheduled_term = terms_by_name.get("Fracture-Wrist")
    booked_appointment = (
        _book_slot_for_demo(scheduled_patient, scheduled_term)
        if scheduled_patient and scheduled_term
        else None
    )

    if booked_appointment:
        scheduled_call = Call(
                vogent_call_id="vg_demo_scheduled_001",
                caller_phone=scheduled_patient.phone,
                started_at=datetime(2026, 9, 10, 14, 5, tzinfo=timezone.utc),
                ended_at=datetime(2026, 9, 10, 14, 12, tzinfo=timezone.utc),
                status="scheduled",
                patient_id=scheduled_patient.id,
                appointment_id=booked_appointment.id,
                matched_term_id=booked_appointment.term_id,
                raw_complaint="my wrist has been killing me since I fell off my bike",
                transcript=[
                    {
                        "speaker": "agent",
                        "text": "Thanks for calling — can I get your first and last name?",
                        "timestamp": "2026-09-10T14:05:03Z",
                    },
                    {
                        "speaker": "caller",
                        "text": "Sure, it's Maria Rodriguez.",
                        "timestamp": "2026-09-10T14:05:09Z",
                    },
                    {
                        "speaker": "agent",
                        "text": "Great, I found your record. What's going on that brings you in?",
                        "timestamp": "2026-09-10T14:05:20Z",
                    },
                    {
                        "speaker": "caller",
                        "text": "My wrist has been killing me since I fell off my bike last week.",
                        "timestamp": "2026-09-10T14:05:30Z",
                    },
                ],
        )
        db.session.add(scheduled_call)
        db.session.flush()
        booked_appointment.call_id = scheduled_call.id
        calls.append(scheduled_call)

    no_match_patient = patients_by_name.get(("Barbara", "O'Connor"))
    calls.append(
        Call(
            vogent_call_id="vg_demo_no_match_001",
            caller_phone="516-555-0154",
            started_at=datetime(2026, 9, 11, 9, 40, tzinfo=timezone.utc),
            ended_at=datetime(2026, 9, 11, 9, 44, tzinfo=timezone.utc),
            status="no_match",
            patient_id=no_match_patient.id if no_match_patient else None,
            raw_complaint="I've had a bad migraine for two days",
            transcript=[
                {
                    "speaker": "agent",
                    "text": "What's going on that brings you in?",
                    "timestamp": "2026-09-11T09:40:15Z",
                },
                {
                    "speaker": "caller",
                    "text": "I've had a bad migraine for two days.",
                    "timestamp": "2026-09-11T09:40:22Z",
                },
                {
                    "speaker": "agent",
                    "text": "I'm sorry — we're an orthopedic practice, so that's not something our "
                    "doctors here treat. You'd want to start with your primary care doctor for that.",
                    "timestamp": "2026-09-11T09:40:30Z",
                },
            ],
        )
    )

    calls.append(
        Call(
            vogent_call_id="vg_demo_abandoned_001",
            caller_phone="631-555-0199",
            started_at=datetime(2026, 9, 11, 16, 2, tzinfo=timezone.utc),
            ended_at=None,
            status="abandoned",
            raw_complaint="my knee hurts when I",
            transcript=[
                {
                    "speaker": "agent",
                    "text": "Thanks for calling — can I get your first and last name?",
                    "timestamp": "2026-09-11T16:02:05Z",
                },
                {
                    "speaker": "caller",
                    "text": "Yeah it's Christoph—",
                    "timestamp": "2026-09-11T16:02:11Z",
                },
            ],
        )
    )

    # Second Smith/Smith-family call, using a caller_phone that matches
    # neither patient's phone on file -- exercises §5.3's first-name-score /
    # confirm-by-readback fallback rather than the silent caller-ID match.
    calls.append(
        Call(
            vogent_call_id="vg_demo_disambiguation_001",
            caller_phone="516-555-0190",
            started_at=datetime(2026, 9, 12, 11, 15, tzinfo=timezone.utc),
            ended_at=datetime(2026, 9, 12, 11, 20, tzinfo=timezone.utc),
            status="failed",
            raw_complaint="my shoulder has been bothering me for weeks",
            transcript=[
                {
                    "speaker": "agent",
                    "text": "Can I get your first and last name and date of birth?",
                    "timestamp": "2026-09-12T11:15:04Z",
                },
                {
                    "speaker": "caller",
                    "text": "Smith, born May twelfth, 1985.",
                    "timestamp": "2026-09-12T11:15:12Z",
                },
            ],
        )
    )

    db.session.add_all(calls)
    db.session.flush()
    return calls
