from collections import OrderedDict
from typing import List

import django.db.models.query

from .core.query import Query
from .core import base
from .core.lookups import LOOKUP_SEPARATOR, parse_lookup
from sphinxql.exceptions import NotSupportedError
from .types import Bool
from .sql import Match, And, Neg, C, Column, All, Count
from sphinxql.configuration import indexes_configurator


def iterate_over_queryset(query_set, callback, amount=1000):
    """
    The function iterates over the given query_set and calls give callback function for each entry. The query_set is
    not iterated whole at a time but by a blocks with specified size. The callback function has the ability to stop the
    iteration.
    :param query_set: the query_set to be iterated
    :param callback: the callback function to be called. It shall return True if the iteration shall stop after current
    entry, False otherwise
    :param amount: block size
    :return: nothing
    """
    offset = 0
    finished = False

    while offset < indexes_configurator.searchd_conf.max_matches:
        current_block = query_set[offset:amount + offset]
        if len(current_block) == 0:
            break

        for row in current_block:
            finished = callback(row)
            if finished:
                break

        if finished:
            break

        offset += amount


class SphinxQuerySet(object):
    def __init__(self, index):
        self._index = index
        self.query = Query()
        self.query.fromm.append(index)

        # Sphinx: there can be only one match per query.
        # This is a global constraint on queries, we keep it here.
        self._match = ''

        self._result_cache = None
        self._fetch_cache = None

        self._set_default_fields(self.query)

    def _fetch_raw(self):
        # type: ()->List
        """
        Fetches by hitting Sphinx
        """
        if self._fetch_cache is None:
            self._fetch_cache = list(self._get_query())
        assert isinstance(self._fetch_cache, list)
        return self._fetch_cache

    def _get_query(self):
        """
        Returns a copy of the query exactly prior to hit db.
        """
        clone = self.query.clone()
        if self._match:
            clone.where = self._add_condition(clone.where, Match(self._match))
        return clone

    def _parsed_results(self):
        """
        Hits Sphinx and parses the results into indexes instances.
        """
        for result in self._fetch_raw():
            instance = self._index()

            setattr(instance, 'id', result[0])
            i = 1  # 1 is id
            for field in self._index.Meta.fields:
                if field.is_attribute:
                    setattr(instance, field.name, field.type().to_python(result[i]))
                    i += 1

            yield instance

    def __iter__(self):
        if self.query.limit is None:
            raise IndexError('Sphinx does not support unbounded iterations over the results.')
        return self._parsed_results()

    def __len__(self):
        if self._fetch_cache is not None:
            return len(self._fetch_cache)
        return self.count()

    def __getitem__(self, item):
        if not isinstance(item, (slice, int)):
            raise TypeError
        if isinstance(item, slice):
            if item.stop is None:
                raise NotSupportedError('Sphinx does not support '
                                        'unbounded slicing.')

        if isinstance(item, slice):
            offset = item.start or 0
            count = item.stop - offset

            clone = self.clone()
            clone.query.limit = (offset, count)
            return list(clone)
        else:
            offset = item
            count = 1
            clone = self.clone()
            clone.query.limit = (offset, count)
            return list(clone)[0]

    def all(self):
        return self

    def count(self):
        q = self._get_query()
        q.select.clear()
        q.select.append(Count(All()))

        result = list(q)
        if result:
            # first row, second entry (first entry is row's `id`)
            return result[0][1]
        else:
            return 0

    def filter(self, *conditions, **lookups):
        clone = self.clone()

        conditions = list(conditions)
        for lookup in lookups:
            condition = parse_lookup(lookup, lookups[lookup])
            conditions.append(condition)

        for condition in conditions:
            assert isinstance(condition, base.SQLExpression)
            condition = condition.resolve_columns(self._index)
            assert condition.type() == Bool
            clone.query.where = self._add_condition(clone.query.where, condition)

        return clone

    def search(self, *extended_queries):
        clone = self.clone()
        if clone._match == '':
            clone._match = ' '.join(list(extended_queries))
        else:
            clone._match = ' '.join([clone._match] + list(extended_queries))
        return clone

    def order_by(self, *args):
        """
        Only accepts Neg, C, Columns
        """
        clone = self.clone()

        if not args:
            clone.query.order_by.clear()
            return clone

        for arg in args:
            # parse string
            if isinstance(arg, str):
                if LOOKUP_SEPARATOR in arg:
                    raise NotImplementedError('Django-SphinxQL does not support '
                                              'lookups in order by.')
                if arg[0] == '-':
                    arg = Neg(C(arg[1:]))
                else:
                    arg = C(arg)

            # parse negation of column
            assert isinstance(arg, (Neg, C, Column))
            ascending = True
            if isinstance(arg, Neg):
                ascending = False
                arg = arg.value[0]
                assert isinstance(arg, (C, Column))
            if isinstance(arg, C):
                column = arg.resolve_columns(clone._index)
            else:
                column = arg

            clone.query.order_by.append(column, ascending=ascending)

        return clone

    def _set_default_fields(self, query):
        fields = self._index.Meta.fields

        query.select.clear()
        for field in fields:
            if field.is_attribute:
                query.select.append(field)

    @staticmethod
    def _add_condition(where, condition):
        if where is None:
            where = condition
        else:
            where = where | And | condition
        return where

    def clone(self):
        clone = SphinxQuerySet(self._index)
        clone._match = self._match
        clone.query = self.query.clone()
        return clone


class ResultStrategy(object):
    def __init__(self, search_query_set):
        self._search_query_set = search_query_set


class ModelResultStrategy(ResultStrategy):
    """
    Used when no search filter is applied. Returns the models directly using the parent methods.
    """

    def __init__(self, search_query_set):
        super(ModelResultStrategy, self).__init__(search_query_set)

    def __iter__(self):
        return self._search_query_set._model_query_set.__iter__()

    def __len__(self):
        return self._search_query_set._model_query_set.__len__()

    def __getitem__(self, item):
        return self._search_query_set._model_query_set.__getitem__(item)

    def count(self):
        return self._search_query_set._model_query_set.count()


class SphinxSearchResultStrategy(ResultStrategy):
    """
    Search mode is applied. First we query sphinx to get the document ids and then we filter using the returned ids.
    """

    def __init__(self, search_query_set):
        super(SphinxSearchResultStrategy, self).__init__(search_query_set)
        self._result_cache = None

    def __iter__(self):
        return iter(self._get_models())

    def __len__(self):
        return len(self._get_model_queryset_with_sphinx_filter())

    def __getitem__(self, item):
        return self._get_models()[item]

    def count(self):
        return self._get_model_queryset_with_sphinx_filter().count()

    def _get_models(self):
        """
        Returns the models annotated with `search_result`. Uses `_result_cache`.
        """
        if self._result_cache is None:
            self._result_cache = self._fetch_models()
        return self._result_cache

    def _get_model_queryset_with_sphinx_filter(self, id_list=None):
        """
        Returns a Django queryset restricted to the ids in `id_list`.
        If `id_list` is None, hits Sphinx to retrieve it.
        """
        if id_list is None:
            id_list = self._fetch_sphinx_indexes().keys()
        return self._search_query_set._model_query_set.filter(pk__in=id_list)

    def _fetch_models(self):
        indexes = self._fetch_sphinx_indexes()
        models = self._fetch_filtered_models(indexes)
        return self._order_models(models, indexes)

    def _order_models(self, models, indexes):
        sphinx_queryset = self._search_query_set._sphinx_query_set

        if sphinx_queryset.query.order_by and self._has_explicit_ordering():
            raise NotImplementedError('Can not order by both database and sphinx')

        if sphinx_queryset.query.order_by and not self._has_explicit_ordering():
            def check_callback(model_id, models, _indexes):
                return model_id in models

            return list(self._prepare_search_results(indexes, models, indexes, check_callback))
        else:
            return list(self._prepare_search_results(models, models, indexes))

    def _prepare_search_results(self, collection, models, indexes, check_callback=None):
        if not check_callback:
            check_callback = lambda model_id, models, indexes: True
        for model_id in collection:
            if check_callback(model_id, models, indexes):
                model = self._annotate_search_result(model_id, models, indexes)
                yield model

    def _annotate_search_result(self, model_id, models, indexes):
        model = models[model_id]
        model.search_result = indexes[model_id]
        return model

    def _fetch_sphinx_indexes(self):
        sphinx_queryset = self._search_query_set._sphinx_query_set
        result = []

        def callback(index_obj):
            result.append((index_obj.id, index_obj))
            return False

        iterate_over_queryset(sphinx_queryset, callback, )
        return OrderedDict(result)

    def _fetch_filtered_models(self, indexes):
        clone = self._get_model_queryset_with_sphinx_filter(indexes.keys())
        return OrderedDict([(obj.id, obj) for obj in clone])

    def _has_explicit_ordering(self):
        """
        A weaker version of ``ordered`` that ignores default ordering and
        Meta.ordering.
        """
        query = self._search_query_set._model_query_set.query
        return query.extra_order_by or query.order_by


def clone_query_set(f):
    def f_with_clone(self, *args, **kwargs):
        clone = self._clone()
        result = f(self, *args, **kwargs, clone=clone)
        return clone if result is None else result

    return f_with_clone


class SearchQuerySet(object):
    """
    A queryset to translate search results into Django models.
    """

    def __init__(self, index, query=None, using=None, hints=None):
        self._index = index
        self._model_query_set = django.db.models.query.QuerySet(index.Meta.model, query, using, hints=hints)
        self._sphinx_query_set = SphinxQuerySet(index)
        self._result_strategy = ModelResultStrategy(self)

    @clone_query_set
    def search_filter(self, *conditions, clone=None, **lookups):
        clone._sphinx_query_set = self._sphinx_query_set.filter(*conditions, **lookups)

    @clone_query_set
    def search(self, *extended_queries, order_by_relevance=True, clone=None):
        clone._sphinx_query_set = clone._sphinx_query_set.search(*extended_queries)
        clone._result_strategy = SphinxSearchResultStrategy(clone)
        if not clone._sphinx_query_set.query.order_by and order_by_relevance:
            clone = clone.search_order_by(C('@relevance'))
        return clone

    @clone_query_set
    def search_order_by(self, *columns, clone=None):
        clone._sphinx_query_set = clone._sphinx_query_set.order_by(*columns)
        clone._result_strategy = SphinxSearchResultStrategy(clone)

    @clone_query_set
    def filter(self, *conditions, clone=None, **lookups):
        clone._model_query_set = self._model_query_set.filter(*conditions, **lookups)

    @clone_query_set
    def annotate(self, *args, clone=None, **kwargs):
        clone._model_query_set = self._model_query_set.annotate(*args, **kwargs)

    @clone_query_set
    def all(self, clone=None):
        pass

    @clone_query_set
    def order_by(self, *field_names, clone=None):
        clone._model_query_set = self._model_query_set.order_by(*field_names)

    def _clone(self, class_=None):
        clone = self._create_cloned_instance(class_)
        self._fill_cloned_instance(clone)
        return clone

    def _create_cloned_instance(self, class_):
        if class_ is None:
            class_ = self.__class__
        return class_(self._index)

    def _fill_cloned_instance(self, clone):
        clone._model_query_set = self._model_query_set
        clone._sphinx_query_set = self._sphinx_query_set
        clone._result_strategy = self._result_strategy.__class__(clone)

    def __iter__(self):
        return self._result_strategy.__iter__()

    def __len__(self):
        return self._result_strategy.__len__()

    def __getitem__(self, item):
        return self._result_strategy.__getitem__(item)

    def count(self):
        return self._result_strategy.count()
