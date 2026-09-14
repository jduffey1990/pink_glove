from django.urls import include, path
from rest_framework.routers import SimpleRouter

from scheduling.views import (
    JobNoteViewSet,
    JobPhotoViewSet,
    JobViewSet,
    RecurringPlanViewSet,
    TimeEntryViewSet,
)

app_name = "scheduling"

router = SimpleRouter()
router.register("plans", RecurringPlanViewSet, basename="plan")
router.register("jobs", JobViewSet, basename="job")
router.register("time-entries", TimeEntryViewSet, basename="timeentry")
router.register("job-notes", JobNoteViewSet, basename="jobnote")
router.register("job-photos", JobPhotoViewSet, basename="jobphoto")

urlpatterns = [path("", include(router.urls))]
