from __future__ import unicode_literals

from django.core.management.base import BaseCommand

from sphinxql import configuration


class Command(BaseCommand):
    help = 'Interacts with search deamon.'

    def handle(self, **options):
        self.stdout.write('Starting Sphinx')
        self.stdout.write('---------------')

        configuration.start()

        self.stdout.write('----')
        self.stdout.write('Done')
