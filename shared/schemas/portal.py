"""Profile of a portal (login, registration, payments, common problems)."""
from __future__ import annotations

from .common import Fact, StrictModel


class CommonProblem(StrictModel):
    problem: str
    fix: Fact


class PortalProfile(StrictModel):
    portal_id: str
    name: str
    url: str
    login_methods: list[str] = []  # e.g. ["mobile + OTP", "username + password"]
    registration_steps: list[Fact] = []
    common_problems: list[CommonProblem] = []
    helpline: Fact = Fact.unknown()
    payment_methods: list[Fact] = []
