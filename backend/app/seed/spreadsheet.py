# Parses Mediportal Information FINAL.xlsx into the operational schema
# (spec §4). This adapts the original root-level seed_mediportal.py's
# parsing logic (name matching, age-value parsing, practice de-duplication)
# rather than rewriting it; the difference is the write target (operational
# tables, not mediportal_* staging tables) and the provider-group scope.
import re

import gender_guesser.detector as gender_detector
import openpyxl

from ..extensions import db
from ..models import Doctor, DoctorPractice, DirectoryEntry, Practice, Term, TermEligibility
from .geography import ZIP_CENTROIDS

# Orlin & Cohen carries some non-orthopedic specialties (Pain Management,
# Physiatry, Neurology) alongside its ortho roster -- excluded here. LIBJ doctors are never
# specialty-filtered (all 10 are ortho).
ORTHO_GROUPS = {"Long Island Bone and Joint (O&C)", "Orlin and Cohen"}
NON_ORTHO_SPECIALTIES = {"Pain Management", "Physiatrist", "Neurologist"}
TERMS_SHEET = "Terms Updated"

_GENDER_DETECTOR = gender_detector.Detector(case_sensitive=False)
_GENDER_MAP = {"male": "male", "mostly_male": "male", "female": "female", "mostly_female": "female"}
# Real names the detector doesn't recognize (not in its name database) --
# confirmed by hand rather than guessed. Add an entry here for any future
# name that falls through to "unknown"/"andy".
GENDER_OVERRIDES = {"Moiz": "male", "Rasel": "male", "Alpesh": "male"}


def _infer_gender(first_name):
    """First name -> "male"/"female"/None. None means genuinely unknown
    (androgynous or not in the detector's name database), not a coin-flip
    guess -- a caller's gender preference is a real routing filter, so a
    wrong guess is worse than admitting we don't know."""
    given = first_name.split()[0].split("-")[0]
    if given in GENDER_OVERRIDES:
        return GENDER_OVERRIDES[given]
    return _GENDER_MAP.get(_GENDER_DETECTOR.get_gender(given))


_AGE_PLUS_RE = re.compile(r"^(\d+)\+$")
_AGE_RANGE_RE = re.compile(r"^(\d+)-(\d+)$")


def _clean(value):
    """Trims whitespace and normalizes openpyxl's numeric cells (zips come
    back as int/float) to plain strings. None/blank collapses to None."""
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    return text or None


def _parse_age(cell_value):
    """Cell value -> (min_age, max_age), or None if unrecognized.
    Y/None -> no restriction, "N+" -> floor only, "N-M" -> explicit range.
    The expanded (LIBJ + Orlin & Cohen) roster has real floors/ceilings
    (e.g. "18+", "2-40"), unlike the original LIBJ-only 7."""
    if cell_value in ("Y", "None"):
        return 1, 100
    m = _AGE_PLUS_RE.match(cell_value)
    if m:
        return int(m.group(1)), 100
    m = _AGE_RANGE_RE.match(cell_value)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _load_rows(wb, sheet_name):
    ws = wb[sheet_name]
    return [tuple(_clean(c) for c in row) for row in ws.iter_rows(values_only=True)]


def load_workbook(xlsx_path):
    return openpyxl.load_workbook(xlsx_path, data_only=True)


def _provider_key(first, last):
    return f"{last}, {first}"


def seed_doctors(provider_rows, warnings):
    """Providers filtered to the ortho groups (LIBJ + Orlin & Cohen, minus
    O&C's non-ortho specialties -- spec §8.1). Returns {"Last, First":
    Doctor} -- the same exact-match key Terms Updated's doctor columns are
    matched against."""
    header = provider_rows[0]
    group_idx = header.index("Group")
    specialty_1_idx, specialty_2_idx = header.index("Specialty 1"), header.index("Specialty 2")
    npi_idx = header.index("NPI")

    by_name = {}
    for row in provider_rows[1:]:
        first, last, group = row[0], row[1], row[group_idx]
        if not first or not last or group not in ORTHO_GROUPS:
            continue
        if row[specialty_1_idx] in NON_ORTHO_SPECIALTIES:
            continue
        specialties = [s for s in (row[specialty_1_idx], row[specialty_2_idx]) if s]
        gender = _infer_gender(first)
        if gender is None:
            warnings.append(f"could not infer gender for {first} {last} -- left null")
        doctor = Doctor(
            first_name=first,
            last_name=last,
            degree=row[2],
            specialty=" / ".join(specialties) or None,
            npi=row[npi_idx],
            gender=gender,
        )
        db.session.add(doctor)
        by_name[_provider_key(first, last)] = doctor
    db.session.flush()
    return by_name


def seed_practices_and_doctor_practices(provider_rows, practice_rows, doctors_by_name, warnings):
    """Practices referenced by the seeded roster's Practice 1..6 columns
    only (not the full multi-practice directory). Same (name, address)
    de-dup as the original script. Only MRI Onsite is carried into the
    operational schema (imaging booking); the other onsite-service flags
    (Xray/PT/OT) are still dropped here, unused so far."""
    provider_header = provider_rows[0]
    practice_cols = [
        (
            provider_header.index(f"Practice {n} - Name"),
            provider_header.index(f"Practice {n} - Address"),
        )
        for n in range(1, 7)
    ]

    practice_header = practice_rows[0]
    main_phone_idx = practice_header.index("Main Phone")
    mri_idx = practice_header.index("MRI Onsite")
    practices_by_key = {}

    def _get_or_create_practice(name, address):
        key = (name, address)
        if key in practices_by_key:
            return practices_by_key[key]
        matching_rows = [row for row in practice_rows[1:] if row[0] == name and row[2] == address]
        if not matching_rows:
            warnings.append(f"no Practice Information match for {name!r} / {address!r}")
            return None
        row = matching_rows[0]
        # The real sheet has duplicate (name, address) rows for at least one
        # LIBJ office with a conflicting MRI Onsite value between them (a
        # real data-quality issue, not a parsing bug -- see
        # notes-final-product.md). Any 'Y' across the duplicates counts as
        # onsite MRI capability, rather than depending on which row happens
        # to be first.
        has_mri = any(r[mri_idx] == "Y" for r in matching_rows)
        zip_code = str(row[5]) if row[5] else None
        centroid = ZIP_CENTROIDS.get(zip_code)
        practice = Practice(
            name=name,
            address=address,
            suite=row[4],
            zip=zip_code,
            region=row[3],
            main_phone=row[main_phone_idx],
            latitude=centroid[0] if centroid else None,
            longitude=centroid[1] if centroid else None,
            has_mri=has_mri,
        )
        db.session.add(practice)
        db.session.flush()
        practices_by_key[key] = practice
        return practice

    for row in provider_rows[1:]:
        first, last = row[0], row[1]
        doctor = doctors_by_name.get(_provider_key(first, last)) if first and last else None
        if not doctor:
            continue
        for n, (name_idx, address_idx) in enumerate(practice_cols, start=1):
            name, address = row[name_idx], row[address_idx]
            if not name or not address:
                continue
            practice = _get_or_create_practice(name, address)
            if not practice:
                continue
            db.session.add(
                DoctorPractice(doctor_id=doctor.id, practice_id=practice.id, is_primary=(n == 1))
            )
    db.session.flush()
    return practices_by_key


def _default_appointment_type(urgency):
    """No appointment-type signal exists in the sheet, so this is a seed-time
    assumption (documented per CLAUDE.md's appointment-length decision):
    URGENT terms get the 'urgent' type; everything else defaults to
    'new_patient_consult'. A later phase could refine this per-term (e.g.
    'follow_up' for chronic/routine categories) once real usage data exists."""
    return "urgent" if urgency == "URGENT" else "new_patient_consult"


def seed_terms_and_eligibility(terms_rows, doctors_by_name, warnings):
    """Full term catalog (all rows in Terms Updated, §4.1) -- terms aren't
    scoped to the seeded roster, only the eligibility rows are (a doctor
    with no matching column, or a name mismatch between sheets, correctly
    ends up with zero eligibility rows rather than erroring)."""
    header = terms_rows[1]
    doctor_columns = [(i, name) for i, name in enumerate(header) if i >= 4 and name]

    terms_by_name = {}
    seen_terms = set()
    for row in terms_rows[2:]:
        term, body_part, category, urgency = row[0], row[1], row[2], row[3]
        if not term:
            continue
        if term in seen_terms:
            warnings.append(f"duplicate term {term!r} in {TERMS_SHEET} -- kept first occurrence")
            continue
        seen_terms.add(term)

        term_row = Term(
            term=term,
            body_part=body_part,
            category=category,
            urgency=urgency,
            patient_phrasing=[],
            default_appointment_type=_default_appointment_type(urgency),
        )
        db.session.add(term_row)
        db.session.flush()
        terms_by_name[term] = term_row

        for col_idx, doctor_name in doctor_columns:
            cell = row[col_idx] if col_idx < len(row) else None
            if not cell:
                continue
            doctor = doctors_by_name.get(doctor_name)
            if not doctor:
                # Column belongs to a doctor outside the seeded roster, or a
                # name that doesn't exactly match Provider Info -- the
                # decided exclusion rule (spec §3.2), not an error.
                continue
            age_range = _parse_age(cell)
            if not age_range:
                warnings.append(
                    f"unrecognized age value {cell!r} for {doctor_name} / {term!r} -- skipped"
                )
                continue
            min_age, max_age = age_range
            db.session.add(
                TermEligibility(
                    term_id=term_row.id, doctor_id=doctor.id, min_age=min_age, max_age=max_age
                )
            )
    db.session.flush()
    return terms_by_name


def seed_directory(directory_rows, warnings):
    """Full Directory sheet, not LIBJ-scoped -- spec §5.9: a dead-end
    redirect target is often an unrelated department."""
    for row in directory_rows[1:]:
        if not any(row):
            continue
        contact, location, group_name = row[0], row[1], row[2]
        if not contact or not location or not group_name:
            warnings.append(f"directory row missing contact/location/group -- skipped: {row}")
            continue
        db.session.add(
            DirectoryEntry(
                contact=contact,
                location=location,
                group_name=group_name,
                desk_number=row[4],
                email=row[5],
            )
        )
