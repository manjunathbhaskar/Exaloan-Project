"""Category H - values must sit inside the product's and the borrower's possible ranges.

These rules read demographic fields but never copy them into findings: evidence carries the
field name and the bound that was breached, not the value.
"""

from __future__ import annotations

from loan_dq.rules.base import BaseRule, LoanContext, Outcome, Severity


class H1BorrowerAgePlausible(BaseRule):
    id = "H1"
    category = "plausibility"
    severity = Severity.MEDIUM
    title = "Borrower age at disbursal within bounds"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        loan = ctx.loan
        if loan.borrower_type == "business":
            return Outcome.skip("business borrower")
        if loan.birth_year is None or loan.disbursal_date is None:
            return Outcome.skip("Birth year or Disbursal date missing")
        age = loan.disbursal_date.year - loan.birth_year
        lo, hi = ctx.config.plausibility.min_age, ctx.config.plausibility.max_age
        if lo <= age <= hi:
            return Outcome.ok()
        return Outcome.fail(
            f"borrower age at disbursal falls outside {lo}-{hi}",
            {"field": "Birth year", "bounds": [lo, hi]},
        )


class H3PaymentWithinIncome(BaseRule):
    id = "H3"
    category = "plausibility"
    severity = Severity.LOW
    title = "Monthly payment does not exceed monthly income"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        loan = ctx.loan
        if loan.borrower_type == "business":
            return Outcome.skip("business borrower")
        income = loan.family_income if loan.family_income else loan.borrower_income
        if not income or loan.monthly_payment is None:
            return Outcome.skip("income or Monthly payment missing")
        ratio = loan.monthly_payment / income
        limit = ctx.config.plausibility.max_payment_to_income
        if ratio <= limit:
            return Outcome.ok()
        return Outcome.fail(
            f"Monthly payment is {ratio:.0%} of stated monthly income (limit {limit:.0%}); "
            "review affordability and income coverage, not a confirmed data defect",
            {"payment_to_income": round(ratio, 2), "limit": limit, "confidence": "low"},
        )


class H4BorrowerTypeFieldsConsistent(BaseRule):
    id = "H4"
    category = "plausibility"
    severity = Severity.MEDIUM
    title = "Person fields present for individuals, company fields for businesses"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        loan = ctx.loan
        if loan.borrower_type is None or "Birth year" in loan.missing_optional_columns:
            return Outcome.skip("Borrower type or Birth year column missing")
        if loan.borrower_type == "individual" and loan.birth_year is None:
            return Outcome.fail("individual borrower without a birth year", {"field": "Birth year"})
        if loan.borrower_type == "business" and loan.birth_year is not None:
            return Outcome.fail(
                "business borrower carries a personal birth year", {"field": "Birth year"}
            )
        return Outcome.ok()


class H5ProductRanges(BaseRule):
    id = "H5"
    category = "plausibility"
    severity = Severity.HIGH
    title = "Interest rate, term and amount inside product ranges"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        loan = ctx.loan
        p = ctx.config.plausibility
        problems: dict[str, object] = {}
        if loan.interest_rate is not None and not (
            p.min_interest_rate <= loan.interest_rate <= p.max_interest_rate
        ):
            problems["Interest rate"] = loan.interest_rate
        if loan.loan_term_months is not None and not (
            p.min_term_months <= loan.loan_term_months <= p.max_term_months
        ):
            problems["Loan term"] = loan.loan_term_months
        if loan.loan_amount is not None and loan.loan_amount <= 0:
            problems["Loan amount"] = loan.loan_amount
        if not problems:
            return Outcome.ok()
        return Outcome.fail(
            "value(s) outside product range: "
            + ", ".join(f"{k} = {v}" for k, v in problems.items()),
            {"fields": problems},
        )


class H9NonNegativeDemographics(BaseRule):
    id = "H9"
    category = "plausibility"
    severity = Severity.MEDIUM
    title = "Income, liabilities and children are non-negative"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        loan = ctx.loan
        negatives = [
            name
            for name, value in (
                ("Family income", loan.family_income),
                ("Borrower income", loan.borrower_income),
                ("Family liabilities", loan.family_liabilities),
                ("Children", loan.children),
                ("Company age (years)", loan.company_age_years),
            )
            if value is not None and value < 0
        ]
        if not negatives:
            return Outcome.ok()
        return Outcome.fail(f"negative value in {', '.join(negatives)}", {"fields": negatives})


class H10EmploymentIncomeConsistent(BaseRule):
    id = "H10"
    category = "plausibility"
    severity = Severity.LOW
    title = "Employment status and income sources merit contextual review"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        loan = ctx.loan
        if loan.employment_status is None or loan.borrower_income is None:
            return Outcome.skip("Employment status or Borrower income missing")
        if loan.employment_status == "unemployed" and loan.borrower_income > 0:
            return Outcome.fail(
                "employment status 'unemployed' with positive borrower income; review income "
                "sources and reporting dates: income need not come from employment",
                {"fields": ["Employment status", "Borrower income"], "confidence": "low"},
            )
        return Outcome.ok()


RULES = [
    H1BorrowerAgePlausible(),
    H3PaymentWithinIncome(),
    H4BorrowerTypeFieldsConsistent(),
    H5ProductRanges(),
    H9NonNegativeDemographics(),
    H10EmploymentIncomeConsistent(),
]
