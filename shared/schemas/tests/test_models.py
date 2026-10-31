"""Instantiate every contract with example data (values are illustrative, not verified facts)."""
from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from shared.schemas import (
    UNKNOWN,
    AskBack,
    CommonError,
    CommonProblem,
    Document,
    Fact,
    FieldDocument,
    FormField,
    FormMap,
    FormPage,
    Glossary,
    GuidanceFile,
    PortalProfile,
    Scheme,
    Service,
    ServiceDetails,
    Source,
)

URL = "https://www.digitalgujarat.gov.in/"
NOW = datetime(2026, 10, 31, 23, 0, tzinfo=timezone.utc)


def fact(value: str) -> Fact:
    return Fact(value=value, quote=f"example quote for {value}", source_url=URL)


def test_fact_rules():
    assert Fact.unknown().is_unknown
    assert Fact(value=UNKNOWN).quote is None
    with pytest.raises(ValidationError):
        Fact(value="₹20", quote=None, source_url=URL)
    with pytest.raises(ValidationError):
        Fact(value="₹20", quote="Fee ₹20", source_url=URL, extra_key=1)


def test_source():
    s = Source(
        source_id="digitalgujarat_home", name="Digital Gujarat", official_url=URL,
        government_level="state", state="Gujarat", department="Science & Technology",
        source_type="portal", collection_method="scrape", last_checked_at=NOW,
    )
    assert s.government_level.value == "state"


def test_service():
    s = Service(
        service_id="gj_income_certificate", name="Income Certificate",
        alternate_names=["आय प्रमाण पत्र", "આવકનો દાખલો", "aavak ka praman patra"],
        department="Revenue Department", government_level="state", state="Gujarat",
        category="certificate", application_mode="both", official_url=URL,
        portal_id="digital_gujarat", source_ids=["digitalgujarat_home"], last_verified_at=NOW,
    )
    assert s.verification_status.value == "pending"


def test_service_details():
    d = ServiceDetails(
        service_id="gj_income_certificate",
        eligibility=[fact("Resident of Gujarat")],
        documents=[Document(name="Ration card", formats=["pdf", "jpg"], max_size_kb=500,
                            fact=fact("Ration card required"))],
        fees=[fact("₹20")],
        timeline=Fact.unknown(),
        issuing_authority=fact("Mamlatdar"),
        steps=[fact("Register on Digital Gujarat"), fact("Fill the application")],
        tracking_url=URL,
    )
    assert d.appeal.is_unknown
    itr = ServiceDetails(service_id="itr_filing", disclaimer="Guidance only, not tax advice.")
    assert itr.timeline.is_unknown


def test_portal_profile():
    p = PortalProfile(
        portal_id="digital_gujarat", name="Digital Gujarat", url=URL,
        login_methods=["mobile + OTP"], registration_steps=[fact("Click Register")],
        common_problems=[CommonProblem(problem="OTP not received", fix=fact("Click Resend OTP"))],
        helpline=fact("1800-233-5500"), payment_methods=[fact("Net banking")],
    )
    assert p.common_problems[0].fix.source_url == URL


def test_form_map():
    fm = FormMap(
        process_id="gj_income_certificate",
        pages=[FormPage(
            page_id="applicant_details", order=1, title="Applicant Details",
            recognise_by=["Applicant Details", "અરજદારની વિગત"],
            fields=[
                FormField(field_id="applicant_name", order=1, label="Applicant Name",
                          label_aliases=["અરજદારનું નામ"], type="text", required=True,
                          meaning="Full name as on Aadhaar", rules=["Must match Aadhaar"],
                          documents=[FieldDocument(name="Aadhaar card", formats=["pdf"], max_size_kb=200)],
                          common_errors=[CommonError(message_contains="invalid name", fix="Remove digits")],
                          ask_back="applying_for", source_url=URL),
                FormField(field_id="aadhaar_number", order=2, label="Aadhaar Number",
                          type="number", required=True, sensitive=True),
            ],
        )],
        ask_backs=[AskBack(id="applying_for", question="For yourself or a family member?",
                           options=["self", "family"], affects=["applicant_name"])],
        pre_submit_checklist=["Name matches Aadhaar"],
        after_submit={"next_steps": "Note the application number", "tracking_url": URL},
    )
    assert fm.pages[0].fields[1].sensitive
    with pytest.raises(ValidationError):
        FormField(field_id="x", order=1, label="X", type="textarea", required=False)


def test_scheme():
    s = Scheme(
        scheme_id="example_scholarship", name="Example Post-Matric Scholarship", level="state",
        state="Gujarat", summary="Scholarship for students", eligibility=["Family income below limit"],
        documents=["Income certificate"], official_url=URL,
        source_url="https://www.myscheme.gov.in/", verified_on=date(2026, 10, 31),
        tags=["student", "education"], required_service_ids=["gj_income_certificate"],
    )
    assert s.required_service_ids == ["gj_income_certificate"]
    with pytest.raises(ValidationError):
        Scheme(scheme_id="x", name="x", level="central", summary="x", official_url=URL,
               source_url=URL, tags=["not_a_tag"])


def test_glossary_and_guidance():
    g = Glossary.model_validate([
        {"term": "income certificate", "hi": "आय प्रमाण पत्र", "gu": "આવકનો દાખલો", "source_url": URL},
        {"term": "OTP", "keep_english": True},
    ])
    assert g.root[1].keep_english
    gf = GuidanceFile.model_validate(
        {"applicant_name": {"say": "अपना पूरा नाम लिखिए", "sources": [URL], "reviewed": True}}
    )
    assert gf.root["applicant_name"].reviewed
