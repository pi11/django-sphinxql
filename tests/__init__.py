from sphinxql.unit_test import SphinxQLTestCase

try:
    import pymysql

    pymysql.install_as_MySQLdb()
except ImportError:
    pass

assert SphinxQLTestCase
