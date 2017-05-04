Querying with Sphinx
====================

.. currentmodule:: sphinxql.query

This document presents the API of the Django-SphinxQL queryset, the high-level
interface for interacting with Sphinx from Django.

SearchQuerySet
--------------

.. class:: SearchQuerySet

    ``SearchQuerySet`` is a subclass of Django ``QuerySet`` to allow text-based
    search with Sphinx; This search is constructed by ``search*`` methods and is
    lazily applied to the Django QuerySet *before* it hits Django's database.

    Formally, a ``SearchQuerySet`` is initialized with one parameter, the index
    it is bound to::

        >>> q = SearchQuerySet(index, query=None, using=None)

    that initializes Django's queryset from the :attr:`Index.Meta.model
    <sphinxql.indexes.Index.Meta.model>`.

    The API of ``SearchQuerySet`` is the same as ``QuerySet``, with the following
    additional methods:

    * :meth:`search`: for text searching
    * :meth:`search_order_by`: for ordering the results of the search
    * :meth:`search_filter`: for filtering the results of the search

    If you don't use any of these methods, ``SearchQuerySet`` is equivalent to
    a Django ``QuerySet`` and can be directly replaced without any change.

    When you apply :meth:`search`, ``SearchQuerySet`` assumes you want to use
    Sphinx on it. When :meth:`search` or :meth:`search_order_by` is called,
    the queryset performs a search in Sphinx database with the query built from
    the ``search*`` methods before interacting with Django database:

    * filtering done by :meth:`search` and :meth:`search_filter` are applied
      before Django's query, restricting the valid ``id`` in the Django's query.
    * :meth:`search_order_by` orders the results.

    At most, ``SearchQuerySet`` does O(1) database hits in Sphinx, followed by the
    Django hit. The amount of results from Sphinx is given by searchd configuration
    max_matches. The number of hits is then at most ceil of max_matches / 1000 since
    in one hit we fetch at most 1000 results.

    If Sphinx is used, model objects are annotated with an attribute
    ``search_result`` with the :class:`~sphinxql.indexes.Index` populated the
    values retrieved from Sphinx database.

    .. _extended query syntax: http://sphinxsearch.com/docs/current.html#extended-syntax

    Below, the full API is explained in detail:

    .. method:: search(*extended_queries)

        Adds a filter to text-search using Sphinx `extended query syntax`_,
        defined by the strings ``extended_queries``. Subsequent calls of this
        method concatenate the different ``extended_query`` with a space (equivalent
        to an ``AND``).

        This method automatically sets a search order according to relevance of the
        results given by the text search.

        For instance::

            >>> q = q.search('@text Hello world')
            >>> q = q.filter(number__gt=2)

        1. Searches for models with ``Hello world`` on the field ``text``
        2. orders them by most relevant first and retrieves the first
           :attr:`max_search_count` entries
        3. filters the remaining entries with the Django query.

        Notice that this method is orderless in the chain: Sphinx is always applied
        before the Django query.

        :meth:`search` supports arbitrary arguments to automatically restrict the
        search; the following are equivalent::

            >>> q.search('@text Hello world @summary "my search"')
            >>> q.search('@text Hello world', '@summary "my search"')

        For convenience, here is a list of some operators (full list `here
        <extended query syntax>`_):

        * And: ``' '`` (a space)
        * Or: ``'|'`` (``'hello | world'``)
        * Not: ``'-'`` or ``'!'`` (e.g. ``'hello -world'``)
        * Mandatory first term, optional second term: ``'MAYBE'`` (e.g.
          ``'hello MAYBE world'``)
        * Phrase match: ``'"<...>"'`` (e.g. ``'"hello world"'``)
        * Before match: ``'<<'`` (e.g. ``'hello << world'``)

    .. method:: search_order_by(*expressions)

        Adds ``ORDER BY`` clauses to Sphinx query. For example::

            >>> q = q.search(a).search_order_by('-number')

        will order first by the search relevance (:meth:`search` added it)
        and then by ``number`` in decreasing order. Use ``search_order_by()``
        to clear the ordering (default order is by ``id``).

        There are two built-in columns, ``'@id'`` and ``'@relevance'``,
        that are used to order by Django ``id`` and by relevance of the results,
        respectively.

        Notice that search ordering is applied *before* Django's query is performed.
        Yet, the final result (after Django query) is ordered according to Django
        ordering unless you didn't set any ordering to Django's query. For example::

        >>> q = q.order_by('id').search(a)

        orders the final results by ``id`` and::

        >>> q = q.order_by('id').search(a).order_by()

        orders the results by search relevance (because ``order_by()``
        cleared Django's ordering).

        In other words, the results are ordered by search ordering unless
        there is an explicit call of ``order_by``.

    .. method:: search_filter(*conditions, **lookups)

        Adds a filter to the search query, allowing you to restrict the search
        results of the search.

        ``lookups`` are like Django lookups for ``filter``. Just remember that
        the field name must be defined on the :class:`sphinxql.indexes.Index`.

        ``conditions`` should be :doc:`Django-SphinxQL expressions <expression>`
        that return a boolean value (e.g. ``>=``) and are used to produce more
        complex filters.

        You can use ``lookups`` and ``conditions`` at the same time::

        >>> q = q.search_filter(number__in=(2,3), C('number1')**2 > 10)

        The method joins all and each ``lookup`` and ``condition`` with ``AND``.

        Like in Django, ``"id__"`` is reserved to indicate the object id (Sphinx
        shares the same ids as Django).

SphinxQuerySet
--------------

.. class:: query.SphinxQuerySet

    ``SphinxQuerySet`` is a Django-equivalent ``SphinxQuerySet`` to indexes. Contrary
    to :class:`SearchQuerySet`, this SphinxQuerySet only interacts with the Sphinx
    database and returns instances of the Index. This can be useful when you need
    to present results of a search that don't need any extra data from Django.

    The interface of SearchQuerySet is equivalent to Django QuerySet: it
    is lazy and allows chaining. However, the current implemented methods are
    limited:

    .. method:: search(*extended_queries)

        Same as :meth:`SearchQuerySet.search`.

    .. method:: filter(*conditions, **lookups)

        Same as :meth:`SearchQuerySet.search_filter`.

    .. method:: order_by(*expressions)

        Same as :meth:`SearchQuerySet.search_order_by`.

    .. method:: count()

        Same as Django's count.
