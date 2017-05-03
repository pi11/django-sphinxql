import shutil
import os

from io import StringIO

import sys
from subprocess import DEVNULL
from time import sleep

from django.conf import settings
from django.core.management import call_command
from django.test import TransactionTestCase

from sphinxql import configuration

try:
    import pymysql

    pymysql.install_as_MySQLdb()
except ImportError:
    pass


class Searchd:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(Searchd, cls).__new__(cls, *args, **kwargs)
            cls._instance.running = False
            cls.setUpBool = True

        return cls._instance

    def start(self):
        if self.running:
            return

        # Django does not support apps using its connections on loading, see
        # https://docs.djangoproject.com/en/1.7/ref/applications/#django.apps.AppConfig.ready
        # Doing so picks the wrong database for tests.
        # We reconfigure after loading to get the right test database.
        configuration.indexes_configurator.configure()
        configuration.indexes_configurator.output()

        self._clean_sphinx_index()

        configuration.index()
        configuration.start(output=DEVNULL)
        self.running = True

    def stop(self):
        if self.running:
            call_command('stop_sphinx', stdout=StringIO())
            shutil.rmtree(settings.INDEXES['path'], ignore_errors=True)
            self.running = False

    def index(self):
        call_command('index_sphinx', stdout=StringIO())

    def _clean_sphinx_index(self):
        if os.path.exists(settings.INDEXES['path']):
            try:
                self.stop()
            except:
                pass
        try:
            shutil.rmtree(settings.INDEXES['path'])
        except:
            sleep(5) # Wait until the server stops and releases all the locks
            shutil.rmtree(settings.INDEXES['path'])
        os.path.exists(settings.INDEXES['path'])
        os.mkdir(settings.INDEXES['path'])


class SphinxQLTestCase(TransactionTestCase):
    running = False

    def setUp(self):
        super(SphinxQLTestCase, self).setUp()
        Searchd().start()

    def index(self):
        """
        Used to reindex the sphinx index.
        """
        Searchd().index()

    def tearDown(cls):
        Searchd().stop()
        super(SphinxQLTestCase, cls).tearDown()
