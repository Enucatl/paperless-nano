from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _

from paperless.signals import handle_failed_login
from paperless.signals import handle_social_account_updated


class PaperlessConfig(AppConfig):
    name = "paperless"

    verbose_name = _("Paperless")

    def ready(self) -> None:
        from importlib import import_module

        from django.contrib.auth.signals import user_login_failed

        # Framework startup owns integrations; importing parser contracts does not.
        import_module("paperless.celery")
        import_module("paperless.checks")

        user_login_failed.connect(handle_failed_login)

        from allauth.socialaccount.signals import social_account_updated

        social_account_updated.connect(handle_social_account_updated)

        AppConfig.ready(self)
