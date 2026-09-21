# Seeds the operational schema (spec §4) from the real
# "Mediportal Information FINAL.xlsx" spreadsheet, scoped to Long Island
# Bone and Joint (O&C) (spec §8.1), plus synthetic availability, patients,
# calls, and lookup tables (spec §8.2-§8.4). Wipes and reloads everything
# except the rolling availability window, which is wiped and regenerated
# every run (see app/seed/availability.py).
#
# Run manually, not on every deploy. Lives at the repo root (not backend/)
# alongside the spreadsheet, so it needs backend/ on sys.path -- see the
# sys.path insert below.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))

from app import create_app
from app.extensions import db
from app.models import (
    Appointment,
    AppointmentSlot,
    Call,
    DirectoryEntry,
    DirectoryRedirectRule,
    Doctor,
    DoctorPractice,
    ImagingAppointment,
    ImagingSlot,
    Patient,
    PatientPrerequisite,
    Practice,
    Term,
    TermEligibility,
    User,
    ZipCentroid,
)
from app.seed.availability import generate_availability, generate_imaging_availability
from app.seed.geography import ZIP_CENTROIDS
from app.seed.spreadsheet import (
    load_workbook,
    seed_directory,
    seed_doctors,
    seed_practices_and_doctor_practices,
    seed_terms_and_eligibility,
    _load_rows,
)
from app.seed.synthetic import (
    apply_patient_phrasing,
    seed_calls,
    seed_directory_redirect_rules,
    seed_patient_prerequisites,
    seed_patients,
    seed_users,
)

XLSX_PATH = Path(__file__).resolve().parent / "data" / "Mediportal Information FINAL.xlsx"

# Expected counts per spec §8.1 -- printed and checked against the actual
# seed result so a spreadsheet or parsing change that silently breaks the
# routing data is caught immediately rather than discovered during a demo.
EXPECTED_DOCTOR_COUNT = 10
EXPECTED_ELIGIBLE_DOCTOR_COUNT = 7
EXPECTED_COVERED_TERM_COUNT = 247
EXPECTED_UNCOVERED_TERM_COUNT = 49


def _seed_zip_centroids():
    for zip_code, (lat, lon, label) in ZIP_CENTROIDS.items():
        db.session.add(ZipCentroid(zip=zip_code, latitude=lat, longitude=lon, label=label))
    db.session.flush()


def _wipe_all():
    # calls <-> appointments and calls <-> imaging_appointments are both
    # genuine FK cycles (spec §4.3) -- null out every cross-referencing
    # column before deleting any of these tables.
    db.session.query(Call).update({Call.appointment_id: None, Call.imaging_appointment_id: None})
    db.session.query(Appointment).update({Appointment.call_id: None})
    db.session.query(ImagingAppointment).update({ImagingAppointment.call_id: None})
    db.session.flush()

    # Deleted in FK-dependency order (children before parents).
    for model in (
        Call,
        Appointment,
        AppointmentSlot,
        ImagingAppointment,
        ImagingSlot,
        PatientPrerequisite,
        DoctorPractice,
        TermEligibility,
        DirectoryRedirectRule,
        Term,
        Practice,
        Doctor,
        DirectoryEntry,
        Patient,
        User,
        ZipCentroid,
    ):
        db.session.query(model).delete()
    db.session.commit()


def _verify_counts(doctors_by_name, terms_by_name):
    eligibility_rows = TermEligibility.query.all()
    eligible_doctor_ids = {row.doctor_id for row in eligibility_rows}
    covered_term_ids = {row.term_id for row in eligibility_rows}

    checks = [
        ("Doctors seeded (LIBJ roster)", len(doctors_by_name), EXPECTED_DOCTOR_COUNT),
        ("Doctors with term_eligibility rows", len(eligible_doctor_ids), EXPECTED_ELIGIBLE_DOCTOR_COUNT),
        ("Distinct terms covered", len(covered_term_ids), EXPECTED_COVERED_TERM_COUNT),
        (
            "Terms with zero eligible doctors",
            len(terms_by_name) - len(covered_term_ids),
            EXPECTED_UNCOVERED_TERM_COUNT,
        ),
    ]
    for label, actual, expected in checks:
        print(f"{label}: {actual} (expected {expected})")

    mismatches = [label for label, actual, expected in checks if actual != expected]
    if mismatches:
        print(f"\nMISMATCH vs spec §8.1 targets: {', '.join(mismatches)}")
    else:
        print("\nAll counts match spec §8.1 targets.")


def seed_mediportal():
    app = create_app()
    with app.app_context():
        _wipe_all()

        wb = load_workbook(XLSX_PATH)
        provider_rows = _load_rows(wb, "Provider Info")
        practice_rows = _load_rows(wb, "Practice Information")
        terms_rows = _load_rows(wb, "Terms Updated")
        directory_rows = _load_rows(wb, "Directory")

        warnings = []
        doctors_by_name = seed_doctors(provider_rows, warnings)
        practices_by_key = seed_practices_and_doctor_practices(
            provider_rows, practice_rows, doctors_by_name, warnings
        )
        terms_by_name = seed_terms_and_eligibility(terms_rows, doctors_by_name, warnings)
        seed_directory(directory_rows, warnings)

        apply_patient_phrasing(terms_by_name, warnings)
        seed_directory_redirect_rules()
        _seed_zip_centroids()
        patients = seed_patients()

        db.session.flush()
        generate_availability(doctors_by_name, practices_by_key, patients)
        generate_imaging_availability(practices_by_key, patients)

        # After availability so the "scheduled" demo call can reference a
        # real pre-booked appointment.
        seed_calls(patients, terms_by_name)
        seed_patient_prerequisites(patients, terms_by_name)
        seed_users()

        db.session.commit()

        print(f"Practices: {len(practices_by_key)}")
        print(f"Patients: {len(patients)}")
        print(f"Appointment slots: {AppointmentSlot.query.count()}")
        print(f"Appointments (pre-booked + demo): {Appointment.query.count()}")
        print(f"MRI-capable practices: {sum(1 for p in practices_by_key.values() if p.has_mri)}")
        print(f"Imaging slots: {ImagingSlot.query.count()}")
        print(f"Patient prerequisites: {PatientPrerequisite.query.count()}")
        print(f"Calls: {Call.query.count()}")
        print(f"Directory entries: {DirectoryEntry.query.count()}")
        print()
        _verify_counts(doctors_by_name, terms_by_name)

        if warnings:
            print(f"\n{len(warnings)} warning(s):")
            for w in warnings:
                print(f"  - {w}")
        print("\nSeed complete.")


if __name__ == "__main__":
    seed_mediportal()
