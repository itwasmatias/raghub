"""Tests for application question classification and answer authority."""
from job_application.candidate import synthetic_test_profile
from job_application.questions import (
    AnswerAuthority,
    QuestionCategory,
    QuestionClassifier,
)


def classify(text: str, qid: str = "q1", required: bool = True):
    profile = synthetic_test_profile()
    return QuestionClassifier().classify(text, qid, required, profile)


class TestDemographicClassification:
    def test_race_is_prohibited(self):
        q = classify("What is your race/ethnicity?")
        assert q.answer_authority == AnswerAuthority.PROHIBITED_TO_INFER
        assert q.answer is None

    def test_gender_is_prohibited(self):
        q = classify("What is your gender identity?")
        assert q.answer_authority == AnswerAuthority.PROHIBITED_TO_INFER
        assert q.answer is None

    def test_veteran_status_is_prohibited(self):
        q = classify("Are you a protected veteran?")
        assert q.answer_authority == AnswerAuthority.PROHIBITED_TO_INFER
        assert q.answer is None

    def test_disability_is_prohibited(self):
        q = classify("Do you have a disability?")
        assert q.answer_authority == AnswerAuthority.PROHIBITED_TO_INFER
        assert q.answer is None

    def test_eeo_category_assigned(self):
        q = classify("Please complete our EEO survey.")
        assert q.category == QuestionCategory.DEMOGRAPHIC_EEO


class TestLegalAttestationClassification:
    def test_certify_is_prohibited(self):
        q = classify("I certify that all information provided is true and accurate.")
        assert q.answer_authority == AnswerAuthority.PROHIBITED_TO_INFER
        assert q.answer is None

    def test_attest_is_prohibited(self):
        q = classify("I attest to the accuracy of this application.")
        assert q.answer_authority == AnswerAuthority.PROHIBITED_TO_INFER


class TestWorkAuthorizationClassification:
    def test_work_authorization_is_user_required(self):
        q = classify("Are you authorized to work in the US?")
        assert q.answer_authority == AnswerAuthority.USER_REQUIRED
        assert q.answer is None

    def test_sponsorship_is_user_required(self):
        q = classify("Do you require visa sponsorship?")
        assert q.category == QuestionCategory.SPONSORSHIP
        assert q.answer_authority == AnswerAuthority.USER_REQUIRED
        assert q.answer is None

    def test_h1b_is_sponsorship(self):
        q = classify("Will you require H-1B sponsorship?")
        assert q.category == QuestionCategory.SPONSORSHIP


class TestSalaryClassification:
    def test_salary_is_user_required(self):
        q = classify("What is your desired salary?")
        assert q.answer_authority == AnswerAuthority.USER_REQUIRED
        assert q.answer is None

    def test_compensation_is_user_required(self):
        q = classify("What are your compensation expectations?")
        assert q.answer_authority == AnswerAuthority.USER_REQUIRED


class TestProfileFactClassification:
    def test_email_answered_from_profile(self):
        q = classify("Email address")
        assert q.answer_authority == AnswerAuthority.PROFILE_FACT
        assert q.answer == "alex.testworthy@example.invalid"

    def test_github_answered_from_profile(self):
        q = classify("GitHub profile URL")
        assert q.answer_authority == AnswerAuthority.PROFILE_FACT
        assert "testworthy-synthetic" in (q.answer or "")

    def test_linkedin_answered_from_profile(self):
        q = classify("LinkedIn URL")
        assert q.answer_authority == AnswerAuthority.PROFILE_FACT
        assert "testworthy-synthetic" in (q.answer or "")

    def test_full_name_answered_from_profile(self):
        q = classify("Full name")
        assert q.answer_authority == AnswerAuthority.PROFILE_FACT
        assert q.answer == "Alex Testworthy"

    def test_location_answered_from_profile(self):
        q = classify("City and state")
        assert q.answer_authority == AnswerAuthority.PROFILE_FACT
        assert "Kansas City" in (q.answer or "")


class TestJobSpecificGenerated:
    def test_why_company_is_job_specific_generated(self):
        q = classify("Why do you want to work at this company?")
        assert q.answer_authority == AnswerAuthority.JOB_SPECIFIC_GENERATED
        assert q.answer is None

    def test_why_role_is_job_specific_generated(self):
        q = classify("Why are you interested in this role?")
        assert q.answer_authority == AnswerAuthority.JOB_SPECIFIC_GENERATED


class TestUnknownProfile:
    def test_education_unknown_in_profile_is_user_required(self):
        q = classify("Highest level of education completed")
        assert q.answer_authority == AnswerAuthority.USER_REQUIRED

    def test_years_experience_with_profile_fact(self):
        q = classify("How many years of professional software experience do you have?")
        # synthetic profile has years_professional_software_experience as USER_SUPPLIED_FACT = "2"
        assert q.answer_authority == AnswerAuthority.PROFILE_FACT
        assert q.answer == "2"
