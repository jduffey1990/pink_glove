"""
The hook another app uses to hold a visit still (ADR-033).

Scheduling does not know who registers with it. These tests use a stand-in
check, so they say what the mechanism does without naming billing: the real
rule, with real invoices, is tested from billing's side in
`billing/tests/test_visit_guard.py`.

Three things are pinned here that nothing else would catch:

* with nothing registered, scheduling behaves exactly as it did before the
  hook existed;
* a list asks the registered check once for the page, never once per row;
* no module under `scheduling/` imports `billing`. The hook exists so that
  it never has to, and an import would quietly close the cycle.
"""

import ast
import pathlib

import pytest
from django.urls import reverse

import scheduling
from scheduling import services
from scheduling.enums import TERMINAL_STATUSES, JobStatus
from scheduling.tests.factories import JobFactory

LIST = reverse("scheduling:job-list")

HELD = "This visit is spoken for. Let go of it first."


def detail(job):
    return reverse("scheduling:job-detail", args=[job.id])


def reopen(client, job):
    return client.post(
        reverse("scheduling:job-status", args=[job.id]), {"status": "scheduled"}, format="json"
    )


def finished(job, status=JobStatus.COMPLETE):
    job.status = status
    job.save()
    return job


@pytest.fixture
def no_checks(monkeypatch):
    """Scheduling on its own, as if no other app had registered anything."""
    monkeypatch.setattr(services, "_claim_checks", [])


@pytest.fixture
def holding(monkeypatch):
    """
    Register a stand-in that holds whichever jobs the test names, and counts
    how often it is asked and about how many.
    """
    held: set = set()
    calls: list[int] = []

    def check(jobs):
        calls.append(len(jobs))
        return {job.pk: HELD for job in jobs if job.pk in held}

    monkeypatch.setattr(services, "_claim_checks", [])
    services.register_job_claim_check(check)
    check.held = held
    check.calls = calls
    return check


@pytest.mark.django_db
class TestWithNothingRegistered:
    """Exactly today's behaviour. If this class changes, the hook leaked."""

    @pytest.mark.parametrize("status", TERMINAL_STATUSES)
    def test_a_finished_visit_reopens(self, no_checks, dispatcher_client, job, status):
        finished(job, status)

        row = dispatcher_client.get(detail(job)).json()
        response = reopen(dispatcher_client, job)

        assert [move["status"] for move in row["next_statuses"]] == ["scheduled"]
        assert response.status_code == 200

    def test_edit_and_delete_are_offered_and_accepted_as_before(
        self, no_checks, dispatcher_client, job
    ):
        row = dispatcher_client.get(detail(job)).json()

        assert row["available_actions"] == ["edit", "delete"]
        assert (
            dispatcher_client.patch(detail(job), {"notes": "x"}, format="json").status_code == 200
        )
        assert dispatcher_client.delete(detail(job)).status_code == 204

    def test_nothing_is_ever_claimed(self, no_checks, job):
        assert services.job_claims([job]) == {}
        assert services.claim_on(job) is None


@pytest.mark.django_db
class TestARegisteredCheck:
    def test_it_stops_a_reopen_in_its_own_words(self, holding, dispatcher_client, job):
        finished(job)
        holding.held.add(job.pk)

        response = reopen(dispatcher_client, job)

        job.refresh_from_db()
        assert response.status_code == 409
        assert response.json() == {"detail": HELD, "status": "complete", "allowed": []}
        assert job.status == JobStatus.COMPLETE

    def test_the_held_visit_is_not_offered_a_reopen(self, holding, dispatcher_client, job):
        finished(job)
        holding.held.add(job.pk)

        assert dispatcher_client.get(detail(job)).json()["next_statuses"] == []

    def test_a_stale_page_is_not_handed_a_reopen_by_another_409(
        self, holding, dispatcher_client, job
    ):
        """An illegal move answers with what is allowed. For a held visit that is nothing."""
        finished(job)
        holding.held.add(job.pk)

        response = dispatcher_client.post(
            reverse("scheduling:job-status", args=[job.id]),
            {"status": "cancelled", "reason": "Late."},
            format="json",
        )

        assert response.status_code == 409
        assert response.json()["allowed"] == []

    def test_a_visit_it_does_not_hold_is_untouched(self, holding, dispatcher_client, job):
        finished(job)

        assert reopen(dispatcher_client, job).status_code == 200

    def test_it_stops_an_edit_and_a_delete_of_a_visit_that_is_not_finished(
        self, holding, dispatcher_client, job
    ):
        """Held but still scheduled: the state a visit reopened before the guard is in."""
        holding.held.add(job.pk)

        row = dispatcher_client.get(detail(job)).json()
        edit = dispatcher_client.patch(detail(job), {"notes": "x"}, format="json")
        delete = dispatcher_client.delete(detail(job))

        assert row["available_actions"] == []
        assert edit.status_code == delete.status_code == 409
        assert edit.json()["detail"] == delete.json()["detail"] == HELD

    def test_the_forward_moves_never_ask(self, holding, dispatcher_client, job):
        """Only reopening can be held, so en route and the rest pay nothing for the hook."""
        dispatcher_client.post(
            reverse("scheduling:job-status", args=[job.id]), {"status": "en_route"}, format="json"
        )

        # Once, for serializing the response's `available_actions`. Not for the move.
        assert holding.calls == [1]

    def test_a_cleaner_is_never_asked_about(self, holding, cleaner_client, assigned_job):
        finished(assigned_job)
        holding.held.add(assigned_job.pk)

        row = cleaner_client.get(detail(assigned_job)).json()

        assert row["next_statuses"] == []
        assert row["available_actions"] == []
        assert holding.calls == []

    def test_a_list_asks_once_for_the_whole_page(
        self, holding, dispatcher_client, organization, customer, location, service
    ):
        jobs = [
            finished(
                JobFactory(
                    organization=organization, customer=customer, location=location, service=service
                )
            )
            for _ in range(6)
        ]
        holding.held.update({jobs[0].pk, jobs[3].pk})

        rows = {row["id"]: row for row in dispatcher_client.get(LIST).json()["results"]}

        assert holding.calls == [6]
        assert rows[str(jobs[0].id)]["next_statuses"] == []
        assert rows[str(jobs[3].id)]["next_statuses"] == []
        assert [m["status"] for m in rows[str(jobs[1].id)]["next_statuses"]] == ["scheduled"]

    def test_registering_the_same_check_twice_asks_once(self, holding, job):
        services.register_job_claim_check(holding)

        services.job_claims([job])

        assert holding.calls == [1]

    def test_two_checks_are_both_asked_and_the_first_to_claim_is_heard(self, holding, job):
        holding.held.add(job.pk)
        services.register_job_claim_check(lambda jobs: {job.pk: "A later claim." for job in jobs})

        assert services.job_claims([job]) == {job.pk: HELD}

    def test_an_empty_batch_asks_nobody(self, holding):
        assert services.job_claims([]) == {}
        assert holding.calls == []


def test_no_scheduling_module_imports_billing():
    """
    The dependency runs one way: billing imports scheduling. A scheduling
    import of billing would close the cycle the registration hook exists to
    avoid, and nothing else would notice until an import order changed.

    Walks every Python file under `scheduling/`, tests included, and reads the
    imports out of the syntax tree, so a lazy import inside a function counts.
    """
    root = pathlib.Path(scheduling.__file__).parent
    offenders = []

    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name == "billing" or name.startswith("billing.") for name in names):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")

    assert offenders == []
