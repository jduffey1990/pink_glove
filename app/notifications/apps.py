from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "notifications"

    def ready(self):
        """
        Listen for visits being saved (ADR-035). `notifications` imports
        `scheduling`, never the reverse, so the receiver is connected from
        here and scheduling does not know it exists.
        """
        from django.db.models.signals import post_save

        from notifications.signals import queue_reminder_if_due
        from scheduling.models import Job

        post_save.connect(
            queue_reminder_if_due, sender=Job, dispatch_uid="notifications.queue_reminder_if_due"
        )
