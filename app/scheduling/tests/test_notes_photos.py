"""
Job notes and photos.

`user` is stamped from the request on both. Accepting it from the payload would
let anyone file a note or a photo under somebody else's name, which is exactly
the kind of record that gets read out in a dispute.
"""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from scheduling.models import JobNote, JobPhoto
from scheduling.tests.factories import JobFactory

NOTES = reverse("scheduling:jobnote-list")
PHOTOS = reverse("scheduling:jobphoto-list")


def note_detail(note):
    return reverse("scheduling:jobnote-detail", args=[note.id])


def _png() -> SimpleUploadedFile:
    """A one-pixel PNG -- Pillow validates ImageField uploads, so this must be real."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(buffer, format="PNG")
    return SimpleUploadedFile("after.png", buffer.getvalue(), content_type="image/png")


@pytest.mark.django_db
class TestCreatingNotes:
    def test_an_assigned_cleaner_can_add_a_note(self, cleaner_client, assigned_job, cleaner):
        response = cleaner_client.post(
            NOTES, {"job": str(assigned_job.id), "body": "Fridge seal is torn."}, format="json"
        )

        assert response.status_code == 201
        assert JobNote.objects.get(job=assigned_job).user_id == cleaner.id

    def test_the_author_is_taken_from_the_session_not_the_payload(
        self, cleaner_client, assigned_job, cleaner, other_cleaner
    ):
        response = cleaner_client.post(
            NOTES,
            {"job": str(assigned_job.id), "body": "Note", "user": str(other_cleaner.id)},
            format="json",
        )

        assert response.status_code == 201
        assert JobNote.objects.get(job=assigned_job).user_id == cleaner.id

    def test_a_cleaner_cannot_write_on_a_job_they_are_not_on(
        self, cleaner_client, assigned_job, other_job
    ):
        response = cleaner_client.post(
            NOTES, {"job": str(other_job.id), "body": "Nosy"}, format="json"
        )

        assert response.status_code == 403
        assert not JobNote.objects.filter(job=other_job).exists()

    def test_a_dispatcher_can_write_on_any_job(self, dispatcher_client, job):
        response = dispatcher_client.post(
            NOTES, {"job": str(job.id), "body": "Customer called."}, format="json"
        )

        assert response.status_code == 201

    def test_a_customer_cannot_write_a_note(self, customer_client, job):
        response = customer_client.post(NOTES, {"job": str(job.id), "body": "Hello"}, format="json")

        assert response.status_code == 403

    def test_a_note_on_another_organizations_job_is_refused(
        self, dispatcher_client, other_organization
    ):
        rival_job = JobFactory(organization=other_organization)

        response = dispatcher_client.post(
            NOTES, {"job": str(rival_job.id), "body": "Nope"}, format="json"
        )

        assert response.status_code == 400
        assert not JobNote.objects.exists()


@pytest.mark.django_db
class TestReadingNotes:
    def test_a_cleaner_sees_notes_on_their_own_jobs(
        self, cleaner_client, dispatcher_client, assigned_job, other_job
    ):
        dispatcher_client.post(NOTES, {"job": str(assigned_job.id), "body": "Mine"}, format="json")
        dispatcher_client.post(NOTES, {"job": str(other_job.id), "body": "Not mine"}, format="json")

        response = cleaner_client.get(NOTES)

        bodies = [row["body"] for row in response.json()["results"]]
        assert bodies == ["Mine"]

    def test_a_cleaner_taken_off_a_job_stops_seeing_its_notes(
        self, cleaner_client, dispatcher_client, cleaner, assigned_job
    ):
        from scheduling.models import JobAssignment

        dispatcher_client.post(NOTES, {"job": str(assigned_job.id), "body": "Mine"}, format="json")
        JobAssignment.objects.get(job=assigned_job, user=cleaner).delete()

        assert cleaner_client.get(NOTES).json()["count"] == 0

    def test_a_dispatcher_sees_them_all(self, dispatcher_client, assigned_job, other_job):
        dispatcher_client.post(NOTES, {"job": str(assigned_job.id), "body": "A"}, format="json")
        dispatcher_client.post(NOTES, {"job": str(other_job.id), "body": "B"}, format="json")

        assert dispatcher_client.get(NOTES).json()["count"] == 2

    def test_a_customer_cannot_read_notes(self, customer_client, dispatcher_client, job):
        dispatcher_client.post(NOTES, {"job": str(job.id), "body": "Internal"}, format="json")

        response = customer_client.get(NOTES)

        assert response.status_code == 403 or response.json()["count"] == 0

    def test_filter_by_job(self, dispatcher_client, assigned_job, other_job):
        dispatcher_client.post(NOTES, {"job": str(assigned_job.id), "body": "A"}, format="json")
        dispatcher_client.post(NOTES, {"job": str(other_job.id), "body": "B"}, format="json")

        response = dispatcher_client.get(NOTES, {"job": str(other_job.id)})

        assert [row["body"] for row in response.json()["results"]] == ["B"]

    def test_another_organizations_notes_are_invisible(
        self, dispatcher_client, rival_job, rival_cleaner
    ):
        note = JobNote.objects.create(
            organization=rival_job.organization, job=rival_job, user=rival_cleaner, body="Rival"
        )

        assert dispatcher_client.get(NOTES).json()["count"] == 0
        assert dispatcher_client.get(note_detail(note)).status_code == 404


@pytest.mark.django_db
class TestEditingNotes:
    """Editing follows deleting: the author, or a dispatcher. Never the job it hangs off."""

    def test_the_author_can_edit_their_own(self, cleaner_client, assigned_job):
        created = cleaner_client.post(
            NOTES, {"job": str(assigned_job.id), "body": "Frige"}, format="json"
        ).json()
        note = JobNote.objects.get(id=created["id"])

        response = cleaner_client.patch(note_detail(note), {"body": "Fridge"}, format="json")

        note.refresh_from_db()
        assert response.status_code == 200
        assert note.body == "Fridge"

    def test_a_cleaner_cannot_edit_someone_elses(
        self, cleaner_client, dispatcher_client, assigned_job
    ):
        created = dispatcher_client.post(
            NOTES, {"job": str(assigned_job.id), "body": "Dispatcher's"}, format="json"
        ).json()
        note = JobNote.objects.get(id=created["id"])

        response = cleaner_client.patch(note_detail(note), {"body": "Rewritten"}, format="json")

        note.refresh_from_db()
        assert response.status_code == 403
        assert note.body == "Dispatcher's"

    def test_a_note_cannot_be_moved_to_another_job(
        self, dispatcher_client, assigned_job, other_job
    ):
        created = dispatcher_client.post(
            NOTES, {"job": str(assigned_job.id), "body": "Stays put"}, format="json"
        ).json()
        note = JobNote.objects.get(id=created["id"])

        response = dispatcher_client.patch(
            note_detail(note), {"job": str(other_job.id)}, format="json"
        )

        note.refresh_from_db()
        assert response.status_code == 400
        assert note.job_id == assigned_job.id

    def test_another_organizations_note_cannot_be_edited(
        self, dispatcher_client, rival_job, rival_cleaner
    ):
        note = JobNote.objects.create(
            organization=rival_job.organization, job=rival_job, user=rival_cleaner, body="Rival"
        )

        response = dispatcher_client.patch(note_detail(note), {"body": "Ours now"}, format="json")

        assert response.status_code == 404


@pytest.mark.django_db
class TestDeletingNotes:
    def test_the_author_can_delete_their_own(self, cleaner_client, assigned_job):
        created = cleaner_client.post(
            NOTES, {"job": str(assigned_job.id), "body": "Oops"}, format="json"
        ).json()
        note = JobNote.objects.get(id=created["id"])

        response = cleaner_client.delete(note_detail(note))

        note.refresh_from_db()
        assert response.status_code == 204
        assert note.deleted_at is not None

    def test_a_dispatcher_can_delete_anyones(self, dispatcher_client, cleaner_client, assigned_job):
        created = cleaner_client.post(
            NOTES, {"job": str(assigned_job.id), "body": "Theirs"}, format="json"
        ).json()

        response = dispatcher_client.delete(note_detail(JobNote.objects.get(id=created["id"])))

        assert response.status_code == 204

    def test_a_cleaner_cannot_delete_someone_elses(
        self, cleaner_client, dispatcher_client, assigned_job
    ):
        created = dispatcher_client.post(
            NOTES, {"job": str(assigned_job.id), "body": "Dispatcher's"}, format="json"
        ).json()

        response = cleaner_client.delete(note_detail(JobNote.objects.get(id=created["id"])))

        assert response.status_code == 403


@pytest.mark.django_db
class TestPhotos:
    def test_an_assigned_cleaner_can_upload(self, cleaner_client, assigned_job, cleaner):
        response = cleaner_client.post(
            PHOTOS,
            {"job": str(assigned_job.id), "image": _png(), "caption": "After"},
            format="multipart",
        )

        assert response.status_code == 201
        assert JobPhoto.objects.get(job=assigned_job).user_id == cleaner.id

    def test_an_image_is_required(self, cleaner_client, assigned_job):
        response = cleaner_client.post(
            PHOTOS, {"job": str(assigned_job.id), "caption": "No picture"}, format="multipart"
        )

        assert response.status_code == 400
        assert "image" in response.json()

    def test_a_cleaner_cannot_upload_to_a_job_they_are_not_on(
        self, cleaner_client, assigned_job, other_job
    ):
        response = cleaner_client.post(
            PHOTOS, {"job": str(other_job.id), "image": _png()}, format="multipart"
        )

        assert response.status_code == 403

    def test_a_customer_cannot_upload(self, customer_client, job):
        response = customer_client.post(
            PHOTOS, {"job": str(job.id), "image": _png()}, format="multipart"
        )

        assert response.status_code == 403

    def test_a_cleaner_sees_photos_from_their_own_jobs_only(
        self, cleaner_client, dispatcher_client, assigned_job, other_job
    ):
        cleaner_client.post(
            PHOTOS, {"job": str(assigned_job.id), "image": _png()}, format="multipart"
        )
        dispatcher_client.post(
            PHOTOS, {"job": str(other_job.id), "image": _png()}, format="multipart"
        )

        assert cleaner_client.get(PHOTOS).json()["count"] == 1
        assert dispatcher_client.get(PHOTOS).json()["count"] == 2
