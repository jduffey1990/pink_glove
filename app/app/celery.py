import os

from celery import Celery

# Production, like wsgi.py and asgi.py: a worker started without the variable
# must not come up with LOCAL = True, DEBUG and the development Fernet key.
# manage.py, pytest and docker-compose.yml each set it before this runs.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings.production")

celery_app = Celery("pink_glove")
celery_app.config_from_object("django.conf:settings", namespace="CELERY")
celery_app.autodiscover_tasks()
