from django.apps import AppConfig


class BillingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "billing"

    def ready(self):
        """
        Tell scheduling which visits an invoice is holding (ADR-033).

        `billing` imports `scheduling`, never the reverse. A visit on an
        invoice must not be reopened, edited or deleted from under it, and
        that is this app's rule to state, so it registers the check here and
        scheduling asks without knowing who answers.
        """
        from billing.services import visit_claims
        from scheduling.services import register_job_claim_check

        register_job_claim_check(visit_claims)
