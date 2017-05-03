from __future__ import unicode_literals

import logging
from django.core.management.base import BaseCommand
from django.db.utils import InternalError, OperationalError
from sphinxql.configuration import indexes_configurator

_logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Generates the sphinx.conf file."

    def add_arguments(self, parser):
        parser.add_argument(
            '--update',
            action='store_true',
            help='',
            default=True)

    def handle(self, **options):
        try:
            indexes_configurator.configure()
            indexes_configurator.output()
        except (InternalError, OperationalError):
            _logger.warning('Sphinx was not configured: no database found.')
            pass
