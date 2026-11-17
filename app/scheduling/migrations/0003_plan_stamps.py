"""
Stamp each plan visit with what the plan wrote (ADR-032).

Three nullable columns on `Job`, a backfill, and the constraint that a stamp is
whole or absent. Also corrects the help text on `plan_occurrence`, which still
said "Immutable" after `regenerate_plan` learned to re-key it.

The backfill is one UPDATE, not a row loop: every visit a plan created
(`plan_occurrence IS NOT NULL`), soft-deleted ones included, is stamped with
its own current price, end and note. That says "nobody has changed this
since the plan wrote it", which is right for every row made before this
branch, because no page could edit a visit. The one case it gets wrong is a
plan visit PATCHed through the API before this ran: its hand-set values
become its stamp, so it reads as untouched and the next plan edit rebuilds
it. That is no worse than what happened to it before the stamp existed.

One-off visits are left NULL: no plan wrote them.

The reverse is a no-op. Going backwards drops the columns, and there is
nothing to un-stamp first.
"""

from django.db import migrations, models

BACKFILL_PLAN_STAMPS = """
    UPDATE scheduling_job
       SET planned_price_cents = price_cents,
           planned_end = scheduled_end,
           planned_notes = notes
     WHERE plan_occurrence IS NOT NULL
       AND planned_end IS NULL
"""


class Migration(migrations.Migration):
    dependencies = [
        ("scheduling", "0002_upload_size_limits"),
    ]

    operations = [
        migrations.AddField(
            model_name="job",
            name="planned_end",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="job",
            name="planned_notes",
            field=models.TextField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="job",
            name="planned_price_cents",
            field=models.PositiveIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AlterField(
            model_name="job",
            name="plan_occurrence",
            field=models.DateTimeField(
                blank=True,
                editable=False,
                help_text="The UTC instant of the plan occurrence this visit accounts for. No caller can write it: the materializer sets it, and regenerate_plan re-keys a kept visit to the same local day when the plan's time moves. This, not scheduled_start, is the materializer's idempotency key -- a rescheduled job must not be re-created at its old slot.",
                null=True,
            ),
        ),
        # After the columns exist and before the constraint, so the constraint
        # is checked against stamped rows.
        migrations.RunSQL(BACKFILL_PLAN_STAMPS, reverse_sql=migrations.RunSQL.noop),
        migrations.AddConstraint(
            model_name="job",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("planned_end__isnull", True),
                        ("planned_notes__isnull", True),
                        ("planned_price_cents__isnull", True),
                    ),
                    models.Q(
                        ("planned_end__isnull", False),
                        ("planned_notes__isnull", False),
                        ("planned_price_cents__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="job_plan_stamp_is_whole",
            ),
        ),
    ]
