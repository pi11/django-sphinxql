from collections import OrderedDict
import os.path
import re

from django.conf import settings
from django.db import connections
from django.db.models import F
from django.db.models.expressions import Combinable

from ..exceptions import ImproperlyConfigured
from ..types import DateTime, Date
from .configurations import IndexerConfiguration, \
    SearchdConfiguration, \
    SourceConfiguration, \
    IndexConfiguration, \
    ConnectionConfiguration
from . import constants


def add_source_conf_param(source_conf, key, value):
    """
    Adds a {key: value} to source_conf taking into account if
    key is a multi_valued_parameter or single_valued_parameter.
    """
    if key in constants.source_multi_valued_parameters:
        if key not in source_conf:
            source_conf[key] = []
        source_conf[key].append(value)
    elif key in constants.source_single_valued_parameters:
        source_conf[key] = value
    else:
        raise ImproperlyConfigured('Invalid parameter "%s" in source '
                                   'configuration' % key)
    return source_conf


SPHINX_TO_DJANGO_MAP = {'user': 'sql_user',
                        'passwd': 'sql_pass', 'password': 'sql_pass',
                        'db': 'sql_db', 'database': 'sql_db',
                        'port': 'sql_port',
                        'host': 'sql_host',
                        'unix_socket': 'sql_sock'
                        }

DJANGO_TO_SPHINX_VENDOR = {'postgresql': 'pgsql',
                           'mysql': 'mysql'}

DEFAULT_INDEXER_PARAMS = {}

DEFAULT_SOURCE_PARAMS = {'sql_host': 'localhost',
                         'sql_pass': '',
                         }

DEFAULT_SEARCHD_PARAMS = {'listen': '9306:mysql41',
                          }

DEFAULT_CONNECTION_PARAMS = {'port': '9306',
                             'host': 'localhost'}

DEFAULT_INDEX_PARAMS = {'type': 'plain'}


def _pymysql_mogrify(cursor, query, args=None):
    """
    This is a copy of the code that is executed in PyMySQL Cursor.execute
    function, but only builds the string; it does not execute it.
    """
    conn = cursor._get_db()

    if args is not None:
        query = query % cursor._escape_args(args, conn)

    return query


def _generate_sql(query, vendor):
    """
    Returns a string with the SQL generated by the particular vendor.

    vendor can only be 'pgsql' or 'mysql'.
    """
    assert (vendor in ('mysql', 'pgsql'))

    db = query.db
    compiler = query.query.get_compiler(using=db)
    cursor = compiler.connection.cursor()
    if vendor == 'pgsql':
        return cursor.mogrify(*compiler.as_sql()).decode('utf-8')
    else:
        if hasattr(cursor, 'mogrify'):
            # the backend has `mogrify` to map sql, params -> sql
            return cursor.mogrify(*compiler.as_sql())
        else:
            # backend doesn't have. We use the code from PyMySQL
            return _pymysql_mogrify(cursor, *compiler.as_sql())


def _build_query(index, query, vendor):
    """
    Returns a SQL query built according to the fields we want to index.
    """
    assert (vendor in ('mysql', 'pgsql'))

    def special_annotate(query, dict_values):
        """
        This is a copy of normal Django annotate but 1. does not check for name
        collisions and 2. does not add GROUP_BY to the query.

        GROUP BY is not required since we are building a SELECT expression.
        Name collision is avoided so our indexes can have the same names as
        the Model.
        """
        obj = query._clone()

        # this line de-activates GROUP BY so we remember what magic we did it.
        # obj._setup_aggregate_query(list(dict_values.items()))

        # Add the aggregates to the query
        for (alias, aggregate_expr) in dict_values.items():
            obj.query.add_annotation(aggregate_expr, alias, is_summary=False)
            # obj.query.add_aggregate(aggregate_expr, query.model, alias,
            #                         is_summary=False)

        return obj

    annotation = OrderedDict()
    for field in index.Meta.fields:
        if isinstance(field.model_attr, str):
            annotation[field.name] = F(field.model_attr)
        elif isinstance(field.model_attr, Combinable):
            annotation[field.name] = field.model_attr
        else:
            raise ImproperlyConfigured(
                'Field "%s" model_attr must be either '
                'a string or a F expression. It is a "%s".' %
                (field.name, type(field.model_attr)))

    # this is an hacky approach, but until we find something better,
    # we have to live with it.
    query = special_annotate(query.only('id'), annotation)
    sql = _generate_sql(query, vendor)

    for field in [f for f in index.Meta.fields
                  if f.type() in (DateTime, Date)]:
        alias = '`%s`' % field.name
        expression = '%s'
        if vendor == 'mysql':
            expression = "UNIX_TIMESTAMP(CONVERT_TZ(" \
                         "%s, " \
                         "'+00:00', " \
                         "@@session.time_zone))"

        elif vendor == 'pgsql':
            alias = '"%s"' % field.name
            if field.type() is DateTime:
                expression = "EXTRACT(EPOCH FROM %s AT TIME ZONE '%s')" % \
                             ('%s', settings.TIME_ZONE)
            elif field.type() is Date:
                expression = "EXTRACT(EPOCH FROM %s)"

        sql = re.sub('([^\s]*) AS %s' % alias,
                     lambda m: '%s AS %s' % (expression % m.group(1),
                                             alias), sql)
    return sql


class Configurator(object):
    """
    The main configurator.

    Uses the settings dictionary ``INDEXES``.
    """

    def __init__(self):
        if not hasattr(settings, 'INDEXES'):
            raise ImproperlyConfigured('Django-SphinxQL requires '
                                       'settings.INDEXES')

        self.sphinx_path = settings.INDEXES.get('sphinx_path')
        self.sphinx_file = os.path.join(self.sphinx_path, 'sphinx.conf')
        self.sphinx_bin_path = settings.INDEXES.get('sphinx_bin_path')
        self.index_path = settings.INDEXES.get('path')

        # registered indexes
        self._registered_indexes = []

        # configured indexes
        self.indexes = OrderedDict()
        self.indexes_confs = []
        self.sources_confs = []

        # configured indexer and searchd
        self.indexer_conf = None
        self.searchd_conf = None
        self.connection_conf = None

        self._configured = False

    def register(self, index):
        """
        Registers an index into the configuration.

        Used by index to register itself in the configuration.
        """
        if index not in self._registered_indexes:
            self._registered_indexes.append(index)

    @staticmethod
    def _configure_source(index):
        """
        Maps an ``Index`` into a Sphinx source configuration.
        """
        source_attrs = OrderedDict()
        source_attrs.update(DEFAULT_SOURCE_PARAMS)
        source_attrs.update(settings.INDEXES.get('source_params', {}))
        source_attrs.update(getattr(index.Meta, 'source_params', {}))

        if hasattr(index.Meta, 'query'):
            query = index.Meta.query
        else:
            query = index.Meta.model.objects.all()

        ### select type from backend
        if connections[query.db].vendor not in DJANGO_TO_SPHINX_VENDOR:
            raise ImproperlyConfigured('Django-SphinxQL currently only supports '
                                       'mysql and postgresql backends')
        vendor = DJANGO_TO_SPHINX_VENDOR[connections[query.db].vendor]

        source_attrs = add_source_conf_param(source_attrs, 'type', vendor)

        if vendor == 'mysql':
            source_attrs = add_source_conf_param(source_attrs, 'sql_query_pre',
                                                 'SET CHARACTER_SET_RESULTS=utf8')

        ### build connection parameters from Django connection parameters
        connection_params = connections[query.db].get_connection_params()
        for key in connection_params:
            if key in SPHINX_TO_DJANGO_MAP:
                value = SPHINX_TO_DJANGO_MAP[key]
                source_attrs = add_source_conf_param(source_attrs,
                                                     value,
                                                     connection_params[key])

        ### create parameters for attributes
        for field in index.Meta.fields:
            if field.is_attribute:
                source_attrs = add_source_conf_param(source_attrs,
                                                     field._sphinx_field_name,
                                                     field.name)

        if hasattr(index.Meta, 'range_step'):
            # see http://sphinxsearch.com/docs/current.html#ranged-queries
            range_step = int(index.Meta.range_step)

            source_attrs = add_source_conf_param(
                source_attrs, 'sql_query_range',
                'SELECT MIN(id),MAX(id) FROM %s' % index.Meta.model._meta.db_table)
            source_attrs = add_source_conf_param(
                source_attrs, 'sql_range_step', range_step)
            query = query.extra(where=['{0}.id>=$start AND {0}.id<=$end'
                                .format(index.Meta.model._meta.db_table)])

        ### add the query
        source_attrs = add_source_conf_param(
            source_attrs,
            'sql_query', _build_query(index, query, vendor))

        return SourceConfiguration(index.build_name(), source_attrs)

    @staticmethod
    def _configure_index(index, source_name):
        """
        Maps a ``Index`` into a Sphinx index configuration.
        """
        index_params = {
            'source': source_name,
            'path': os.path.join(settings.INDEXES['path'], source_name)
        }
        index_params.update(DEFAULT_INDEX_PARAMS)
        index_params.update(settings.INDEXES.get('index_params', {}))
        index_params.update(getattr(index.Meta, 'index_params', {}))

        return IndexConfiguration(index.build_name(), index_params)

    @staticmethod
    def _configure_searchd():
        searchd_params = OrderedDict()
        searchd_params.update(DEFAULT_SEARCHD_PARAMS)
        searchd_params['pid_file'] = os.path.join(settings.INDEXES.get('sphinx_path'), 'searchd.pid')
        # see WARNING at http://sphinxsearch.com/docs/current/conf-binlog-path.html
        searchd_params['binlog_path'] = settings.INDEXES.get('sphinx_path')

        searchd_params.update(settings.INDEXES.get('searchd_params', {}))

        return SearchdConfiguration(searchd_params)

    def _configure_connection(self, test=False):
        connection_params = self._get_default_values()
        if not test:
            connection_params.update(settings.INDEXES.get('connection_params', {}))
        if connection_params.get('port') is None:
            connection_params['port'] = self._determine_port_from_listen()
        if connection_params.get('host') is None:
            connection_params['host'] = self._determine_host_from_listen()
        return ConnectionConfiguration(connection_params)

    def _get_default_values(self):
        connection_params = OrderedDict()
        default_params = DEFAULT_CONNECTION_PARAMS.copy()
        for k in default_params.keys():
            default_params[k] = None
        connection_params.update(default_params)
        return connection_params

    def _determine_port_from_listen(self):
        listen = self.searchd_conf.params.get('listen', DEFAULT_CONNECTION_PARAMS['port'])
        if ':' in listen:
            listen = listen.split(':')
            listen = listen[-2] if listen[-1] == 'mysql41' else listen[-1]
        return listen

    def _determine_host_from_listen(self):
        listen = self.searchd_conf.params.get('listen', DEFAULT_CONNECTION_PARAMS['host'])
        if ':' in listen:
            listen = listen.split(':')
            if len(listen) == 2 and listen[1] == 'mysql41':
                listen = DEFAULT_CONNECTION_PARAMS['host']
            else:
                listen = listen[0]
        return listen

    @staticmethod
    def _configure_indexer():
        indexer_params = OrderedDict()
        indexer_params.update(DEFAULT_INDEXER_PARAMS)
        indexer_params.update(settings.INDEXES.get('indexer_params', {}))
        return IndexerConfiguration(indexer_params)

    def configure(self, force=False, test=False):
        """
        Configures the registered indexes.

        This method must be called before `output`.
        """
        if self._configured and not force:
            return
        if not hasattr(settings, 'INDEXES'):
            raise ImproperlyConfigured('Django-SphinxQL requires '
                                       'settings.INDEXES')

        self._configured = True
        self.indexer_conf = self._configure_indexer()
        self.searchd_conf = self._configure_searchd()
        self.connection_conf = self._configure_connection(test=test)
        self.sources_confs.clear()
        self.indexes_confs.clear()
        self.indexes.clear()
        for index in self._registered_indexes:
            meta = getattr(index.Meta.model, '_meta', None)
            assert meta is not None

            assert index not in self.indexes.values()
            self.indexes[index.build_name()] = index

            source_conf = self._configure_source(index)
            index_conf = self._configure_index(index, source_conf.name)
            self.sources_confs.append(source_conf)
            self.indexes_confs.append(index_conf)

    def output(self):
        """
        Outputs the configuration file `sphinx.conf`.
        """
        assert self.indexes

        string_blocks = ["# WARNING! This file was automatically generated: do not "
                         "modify it.\n"]
        # output all source and indexes
        for i in range(len(self.indexes)):
            string_blocks.append(self.sources_confs[i].format_output())
            string_blocks.append(self.indexes_confs[i].format_output())

        # output indexer and searchd
        string_blocks.append(self.indexer_conf.format_output())
        string_blocks.append(self.searchd_conf.format_output())

        os.makedirs(os.path.dirname(self.sphinx_file), exist_ok=True)

        with open(self.sphinx_file, 'w') as conf_file:
            conf_file.write('\n'.join(string_blocks))
