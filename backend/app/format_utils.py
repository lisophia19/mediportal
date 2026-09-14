# Shared spoken/confirmation date-time formatting (spec §5.5's
# `spoken_label`, §5.6's confirmation `when`) -- both blueprints need the
# same "Wednesday, September 17th at 10:30 AM" style string, so it lives
# here once instead of twice.
from datetime import datetime


def parse_iso_date(value):
    """'YYYY-MM-DD' -> date, or None if malformed."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def format_doctor_name(doctor):
    return f"Dr. {doctor.first_name} {doctor.last_name}" if doctor else None


def serialize_patient(patient):
    if patient is None:
        return None
    return {
        "id": patient.id,
        "first_name": patient.first_name,
        "last_name": patient.last_name,
        "date_of_birth": patient.date_of_birth.isoformat(),
        "phone": patient.phone,
        "home_zip": patient.home_zip,
    }


def slot_to_dict(slot):
    """A SchedulingProvider SlotResult as the JSON shape spec §5.5/§5.6 both
    return for an offered slot."""
    return {
        "slot_id": slot.slot_id,
        "start_time": slot.start_time.isoformat(),
        "duration_minutes": slot.duration_minutes,
        "practice_name": slot.practice_name,
        "spoken_label": format_spoken_datetime(slot.start_time),
    }


def ordinal(day):
    if 11 <= day % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def format_spoken_datetime(moment):
    """'Wednesday, September 17th at 10:30 AM'.

    Deviation from spec §5.5's fully-spelled-out example
    ("ten thirty in the morning") -- digit-based time reads fine for a
    baseline demo and avoids a number-to-words step that isn't otherwise
    needed anywhere in the backend.
    """
    weekday_month = moment.strftime("%A, %B")
    day = ordinal(moment.day)
    time_str = moment.strftime("%I:%M %p").lstrip("0")
    return f"{weekday_month} {day} at {time_str}"
