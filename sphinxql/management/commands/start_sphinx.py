from __future__ import unicode_literals

import sys
from django.core.management.base import BaseCommand

from sphinxql import configuration


class Command(BaseCommand):
    help = 'Interacts with search deamon.'

    def handle(self, **options):
        self.stdout.write('Starting Sphinx')
        self.stdout.write('---------------')

        p = configuration.start()
        p.wait()

        self.stdout.write('----')
        self.stdout.write('Done')
