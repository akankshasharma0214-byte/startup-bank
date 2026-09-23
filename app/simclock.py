"""A simulated 'today' so ACH/wire float (1-2 business days) can be demoed by advancing days
instead of waiting for real time to pass. Every part of the app that needs 'today' calls sim_today(),
never date.today() directly."""

from datetime import date, timedelta

from sqlalchemy.orm import Session

from .models import SimClockState


def sim_today(db: Session) -> date:
    state = db.get(SimClockState, 1)
    if state is None:
        state = SimClockState(id=1, sim_date=date.today())
        db.add(state)
        db.flush()
    return state.sim_date


def set_sim_date(db: Session, d: date) -> date:
    """Set the simulated clock to an exact date. Intended for setting up a deterministic starting
    point (tests, demos) before any activity happens -- not for rewinding a clock that has already
    been used, which could make already-settled transactions look like they are back in the future."""
    state = db.get(SimClockState, 1)
    if state is None:
        state = SimClockState(id=1, sim_date=d)
        db.add(state)
    else:
        state.sim_date = d
    db.flush()
    return state.sim_date


def advance_days(db: Session, days: int) -> date:
    if days <= 0:
        raise ValueError("days must be positive")
    state = db.get(SimClockState, 1)
    if state is None:
        state = SimClockState(id=1, sim_date=date.today())
        db.add(state)
        db.flush()
    state.sim_date = state.sim_date + timedelta(days=days)
    db.flush()
    return state.sim_date


def next_business_day(d: date, business_days: int = 1) -> date:
    """Simplified: skips weekends, ignores bank holidays."""
    for _ in range(business_days):
        d += timedelta(days=1)
        while d.weekday() >= 5:  # Sat=5, Sun=6
            d += timedelta(days=1)
    return d
