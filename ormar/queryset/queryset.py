from typing import Any, Dict, List, Optional, Sequence, Set, TYPE_CHECKING, Type, Union

import databases
import sqlalchemy
from sqlalchemy import bindparam

import ormar  # noqa I100
from ormar import MultipleMatches, NoMatch
from ormar.exceptions import ModelError, ModelPersistenceError, QueryDefinitionError
from ormar.queryset import FilterQuery
from ormar.queryset.clause import QueryClause
from ormar.queryset.prefetch_query import PrefetchQuery
from ormar.queryset.query import Query
from ormar.queryset.utils import update, update_dict_from_list

if TYPE_CHECKING:  # pragma no cover
    from ormar import Model
    from ormar.models.metaclass import ModelMeta
    from ormar.relations.querysetproxy import QuerysetProxy


class QuerySet:
    """
    Main class to perform database queries, exposed on each model as objects attribute.
    """

    def __init__(  # noqa CFQ002
        self,
        model_cls: Type["Model"] = None,
        filter_clauses: List = None,
        exclude_clauses: List = None,
        select_related: List = None,
        limit_count: int = None,
        offset: int = None,
        columns: Dict = None,
        exclude_columns: Dict = None,
        order_bys: List = None,
        prefetch_related: List = None,
        limit_raw_sql: bool = False,
    ) -> None:
        self.model_cls = model_cls
        self.filter_clauses = [] if filter_clauses is None else filter_clauses
        self.exclude_clauses = [] if exclude_clauses is None else exclude_clauses
        self._select_related = [] if select_related is None else select_related
        self._prefetch_related = [] if prefetch_related is None else prefetch_related
        self.limit_count = limit_count
        self.query_offset = offset
        self._columns = columns or {}
        self._exclude_columns = exclude_columns or {}
        self.order_bys = order_bys or []
        self.limit_sql_raw = limit_raw_sql

    def __get__(
        self,
        instance: Optional[Union["QuerySet", "QuerysetProxy"]],
        owner: Union[Type["Model"], Type["QuerysetProxy"]],
    ) -> "QuerySet":
        if issubclass(owner, ormar.Model):
            if owner.Meta.requires_ref_update:
                raise ModelError(
                    f"Model {owner.get_name()} has not updated "
                    f"ForwardRefs. \nBefore using the model you "
                    f"need to call update_forward_refs()."
                )
        if issubclass(owner, ormar.Model):
            return self.__class__(model_cls=owner)
        return self.__class__()  # pragma: no cover

    @property
    def model_meta(self) -> "ModelMeta":
        """
        Shortcut to model class Meta set on QuerySet model.

        :return: Meta class of the model
        :rtype: model Meta class
        """
        if not self.model_cls:  # pragma nocover
            raise ValueError("Model class of QuerySet is not initialized")
        return self.model_cls.Meta

    @property
    def model(self) -> Type["Model"]:
        """
        Shortcut to model class set on QuerySet.

        :return: model class
        :rtype: Type[Model]
        """
        if not self.model_cls:  # pragma nocover
            raise ValueError("Model class of QuerySet is not initialized")
        return self.model_cls

    async def _prefetch_related_models(
        self, models: Sequence[Optional["Model"]], rows: List
    ) -> Sequence[Optional["Model"]]:
        """
        Performs prefetch query for selected models names.

        :param models: list of already parsed main Models from main query
        :type models: List[Model]
        :param rows: database rows from main query
        :type rows: List[sqlalchemy.engine.result.RowProxy]
        :return: list of models with prefetch models populated
        :rtype: List[Model]
        """
        query = PrefetchQuery(
            model_cls=self.model,
            fields=self._columns,
            exclude_fields=self._exclude_columns,
            prefetch_related=self._prefetch_related,
            select_related=self._select_related,
            orders_by=self.order_bys,
        )
        return await query.prefetch_related(models=models, rows=rows)  # type: ignore

    def _process_query_result_rows(self, rows: List) -> Sequence[Optional["Model"]]:
        """
        Process database rows and initialize ormar Model from each of the rows.

        :param rows: list of database rows from query result
        :type rows: List[sqlalchemy.engine.result.RowProxy]
        :return: list of models
        :rtype: List[Model]
        """
        result_rows = [
            self.model.from_row(
                row=row,
                select_related=self._select_related,
                fields=self._columns,
                exclude_fields=self._exclude_columns,
            )
            for row in rows
        ]
        if result_rows:
            return self.model.merge_instances_list(result_rows)  # type: ignore
        return result_rows

    @staticmethod
    def check_single_result_rows_count(rows: Sequence[Optional["Model"]]) -> None:
        """
        Verifies if the result has one and only one row.

        :param rows: one element list of Models
        :type rows: List[Model]
        """
        if not rows or rows[0] is None:
            raise NoMatch()
        if len(rows) > 1:
            raise MultipleMatches()

    @property
    def database(self) -> databases.Database:
        """
        Shortcut to models database from Meta class.

        :return: database
        :rtype: databases.Database
        """
        return self.model_meta.database

    @property
    def table(self) -> sqlalchemy.Table:
        """
        Shortcut to models table from Meta class.

        :return: database table
        :rtype: sqlalchemy.Table
        """
        return self.model_meta.table

    def build_select_expression(
        self, limit: int = None, offset: int = None, order_bys: List = None,
    ) -> sqlalchemy.sql.select:
        """
        Constructs the actual database query used in the QuerySet.
        If any of the params is not passed the QuerySet own value is used.

        :param limit: number to limit the query
        :type limit: int
        :param offset: number to offset by
        :type offset: int
        :param order_bys: list of order-by fields names
        :type order_bys: List
        :return: built sqlalchemy select expression
        :rtype: sqlalchemy.sql.selectable.Select
        """
        qry = Query(
            model_cls=self.model,
            select_related=self._select_related,
            filter_clauses=self.filter_clauses,
            exclude_clauses=self.exclude_clauses,
            offset=offset or self.query_offset,
            limit_count=limit or self.limit_count,
            fields=self._columns,
            exclude_fields=self._exclude_columns,
            order_bys=order_bys or self.order_bys,
            limit_raw_sql=self.limit_sql_raw,
        )
        exp = qry.build_select_expression()
        # print("\n", exp.compile(compile_kwargs={"literal_binds": True}))
        return exp

    def filter(self, _exclude: bool = False, **kwargs: Any) -> "QuerySet":  # noqa: A003
        """
        Allows you to filter by any `Model` attribute/field
        as well as to fetch instances, with a filter across an FK relationship.

        You can use special filter suffix to change the filter operands:

        *  exact - like `album__name__exact='Malibu'` (exact match)
        *  iexact - like `album__name__iexact='malibu'` (exact match case insensitive)
        *  contains - like `album__name__contains='Mal'` (sql like)
        *  icontains - like `album__name__icontains='mal'` (sql like case insensitive)
        *  in - like `album__name__in=['Malibu', 'Barclay']` (sql in)
        *  gt - like `position__gt=3` (sql >)
        *  gte - like `position__gte=3` (sql >=)
        *  lt - like `position__lt=3` (sql <)
        *  lte - like `position__lte=3` (sql <=)
        *  startswith - like `album__name__startswith='Mal'` (exact start match)
        *  istartswith - like `album__name__istartswith='mal'` (case insensitive)
        *  endswith - like `album__name__endswith='ibu'` (exact end match)
        *  iendswith - like `album__name__iendswith='IBU'` (case insensitive)

        :param _exclude: flag if it should be exclude or filter
        :type _exclude: bool
        :param kwargs: fields names and proper value types
        :type kwargs: Any
        :return: filtered QuerySet
        :rtype: QuerySet
        """
        # TODO: delay processing of filter clauses or switch to group one
        #  that keeps all aliases even if duplicated - now initialized too late
        #  in the join
        qryclause = QueryClause(
            model_cls=self.model,
            select_related=self._select_related,
            filter_clauses=self.filter_clauses,
        )
        filter_clauses, select_related = qryclause.prepare_filter(**kwargs)
        if _exclude:
            exclude_clauses = filter_clauses
            filter_clauses = self.filter_clauses
        else:
            exclude_clauses = self.exclude_clauses
            filter_clauses = filter_clauses

        return self.__class__(
            model_cls=self.model,
            filter_clauses=filter_clauses,
            exclude_clauses=exclude_clauses,
            select_related=select_related,
            limit_count=self.limit_count,
            offset=self.query_offset,
            columns=self._columns,
            exclude_columns=self._exclude_columns,
            order_bys=self.order_bys,
            prefetch_related=self._prefetch_related,
            limit_raw_sql=self.limit_sql_raw,
        )

    def exclude(self, **kwargs: Any) -> "QuerySet":  # noqa: A003
        """
        Works exactly the same as filter and all modifiers (suffixes) are the same,
        but returns a *not* condition.

        So if you use `filter(name='John')` which is `where name = 'John'` in SQL,
        the `exclude(name='John')` equals to `where name <> 'John'`

        Note that all conditions are joined so if you pass multiple values it
        becomes a union of conditions.

        `exclude(name='John', age>=35)` will become
        `where not (name='John' and age>=35)`

        :param kwargs: fields names and proper value types
        :type kwargs: Any
        :return: filtered QuerySet
        :rtype: QuerySet
        """
        return self.filter(_exclude=True, **kwargs)

    def select_related(self, related: Union[List, str]) -> "QuerySet":
        """
        Allows to prefetch related models during the same query.

        **With `select_related` always only one query is run against the database**,
        meaning that one (sometimes complicated) join is generated and later nested
        models are processed in python.

        To fetch related model use `ForeignKey` names.

        To chain related `Models` relation use double underscores between names.

        :param related: list of relation field names, can be linked by '__' to nest
        :type related: Union[List, str]
        :return: QuerySet
        :rtype: QuerySet
        """
        if not isinstance(related, list):
            related = [related]

        related = list(set(list(self._select_related) + related))
        return self.__class__(
            model_cls=self.model,
            filter_clauses=self.filter_clauses,
            exclude_clauses=self.exclude_clauses,
            select_related=related,
            limit_count=self.limit_count,
            offset=self.query_offset,
            columns=self._columns,
            exclude_columns=self._exclude_columns,
            order_bys=self.order_bys,
            prefetch_related=self._prefetch_related,
            limit_raw_sql=self.limit_sql_raw,
        )

    def prefetch_related(self, related: Union[List, str]) -> "QuerySet":
        """
        Allows to prefetch related models during query - but opposite to
        `select_related` each subsequent model is fetched in a separate database query.

        **With `prefetch_related` always one query per Model is run against the
        database**, meaning that you will have multiple queries executed one
        after another.

        To fetch related model use `ForeignKey` names.

        To chain related `Models` relation use double underscores between names.

        :param related: list of relation field names, can be linked by '__' to nest
        :type related: Union[List, str]
        :return: QuerySet
        :rtype: QuerySet
        """
        if not isinstance(related, list):
            related = [related]

        related = list(set(list(self._prefetch_related) + related))
        return self.__class__(
            model_cls=self.model,
            filter_clauses=self.filter_clauses,
            exclude_clauses=self.exclude_clauses,
            select_related=self._select_related,
            limit_count=self.limit_count,
            offset=self.query_offset,
            columns=self._columns,
            exclude_columns=self._exclude_columns,
            order_bys=self.order_bys,
            prefetch_related=related,
            limit_raw_sql=self.limit_sql_raw,
        )

    def fields(self, columns: Union[List, str, Set, Dict]) -> "QuerySet":
        """
        With `fields()` you can select subset of model columns to limit the data load.

        Note that `fields()` and `exclude_fields()` works both for main models
        (on normal queries like `get`, `all` etc.)
        as well as `select_related` and `prefetch_related`
        models (with nested notation).

        You can select specified fields by passing a `str, List[str], Set[str] or
        dict` with nested definition.

        To include related models use notation
        `{related_name}__{column}[__{optional_next} etc.]`.

        `fields()` can be called several times, building up the columns to select.

        If you include related models into `select_related()` call but you won't specify
        columns for those models in fields - implies a list of all fields for
        those nested models.

        Mandatory fields cannot be excluded as it will raise `ValidationError`,
        to exclude a field it has to be nullable.

        Pk column cannot be excluded - it's always auto added even if
        not explicitly included.

        You can also pass fields to include as dictionary or set.

        To mark a field as included in a dictionary use it's name as key
        and ellipsis as value.

        To traverse nested models use nested dictionaries.

        To include fields at last level instead of nested dictionary a set can be used.

        To include whole nested model specify model related field name and ellipsis.

        :param columns: columns to include
        :type columns: Union[List, str, Set, Dict]
        :return: QuerySet
        :rtype: QuerySet
        """
        if isinstance(columns, str):
            columns = [columns]

        current_included = self._columns
        if not isinstance(columns, dict):
            current_included = update_dict_from_list(current_included, columns)
        else:
            current_included = update(current_included, columns)

        return self.__class__(
            model_cls=self.model,
            filter_clauses=self.filter_clauses,
            exclude_clauses=self.exclude_clauses,
            select_related=self._select_related,
            limit_count=self.limit_count,
            offset=self.query_offset,
            columns=current_included,
            exclude_columns=self._exclude_columns,
            order_bys=self.order_bys,
            prefetch_related=self._prefetch_related,
            limit_raw_sql=self.limit_sql_raw,
        )

    def exclude_fields(self, columns: Union[List, str, Set, Dict]) -> "QuerySet":
        """
        With `exclude_fields()` you can select subset of model columns that will
        be excluded to limit the data load.

        It's the opposite of `fields()` method so check documentation above
        to see what options are available.

        Especially check above how you can pass also nested dictionaries
        and sets as a mask to exclude fields from whole hierarchy.

        Note that `fields()` and `exclude_fields()` works both for main models
        (on normal queries like `get`, `all` etc.)
        as well as `select_related` and `prefetch_related` models
        (with nested notation).

        Mandatory fields cannot be excluded as it will raise `ValidationError`,
        to exclude a field it has to be nullable.

        Pk column cannot be excluded - it's always auto added even
        if explicitly excluded.

        :param columns: columns to exclude
        :type columns: Union[List, str, Set, Dict]
        :return: QuerySet
        :rtype: QuerySet
        """
        if isinstance(columns, str):
            columns = [columns]

        current_excluded = self._exclude_columns
        if not isinstance(columns, dict):
            current_excluded = update_dict_from_list(current_excluded, columns)
        else:
            current_excluded = update(current_excluded, columns)

        return self.__class__(
            model_cls=self.model,
            filter_clauses=self.filter_clauses,
            exclude_clauses=self.exclude_clauses,
            select_related=self._select_related,
            limit_count=self.limit_count,
            offset=self.query_offset,
            columns=self._columns,
            exclude_columns=current_excluded,
            order_bys=self.order_bys,
            prefetch_related=self._prefetch_related,
            limit_raw_sql=self.limit_sql_raw,
        )

    def order_by(self, columns: Union[List, str]) -> "QuerySet":
        """
        With `order_by()` you can order the results from database based on your
        choice of fields.

        You can provide a string with field name or list of strings with fields names.

        Ordering in sql will be applied in order of names you provide in order_by.

        By default if you do not provide ordering `ormar` explicitly orders by
        all primary keys

        If you are sorting by nested models that causes that the result rows are
        unsorted by the main model `ormar` will combine those children rows into
        one main model.

        The main model will never duplicate in the result

        To order by main model field just provide a field name

        To sort on nested models separate field names with dunder '__'.

        You can sort this way across all relation types -> `ForeignKey`,
        reverse virtual FK and `ManyToMany` fields.

        To sort in descending order provide a hyphen in front of the field name

        :param columns: columns by which models should be sorted
        :type columns: Union[List, str]
        :return: QuerySet
        :rtype: QuerySet
        """
        if not isinstance(columns, list):
            columns = [columns]

        order_bys = self.order_bys + [x for x in columns if x not in self.order_bys]
        return self.__class__(
            model_cls=self.model,
            filter_clauses=self.filter_clauses,
            exclude_clauses=self.exclude_clauses,
            select_related=self._select_related,
            limit_count=self.limit_count,
            offset=self.query_offset,
            columns=self._columns,
            exclude_columns=self._exclude_columns,
            order_bys=order_bys,
            prefetch_related=self._prefetch_related,
            limit_raw_sql=self.limit_sql_raw,
        )

    async def exists(self) -> bool:
        """
        Returns a bool value to confirm if there are rows matching the given criteria
        (applied with `filter` and `exclude` if set).

        :return: result of the check
        :rtype: bool
        """
        expr = self.build_select_expression()
        expr = sqlalchemy.exists(expr).select()
        return await self.database.fetch_val(expr)

    async def count(self) -> int:
        """
        Returns number of rows matching the given criteria
        (applied with `filter` and `exclude` if set before).

        :return: number of rows
        :rtype: int
        """
        expr = self.build_select_expression().alias("subquery_for_count")
        expr = sqlalchemy.func.count().select().select_from(expr)
        return await self.database.fetch_val(expr)

    async def update(self, each: bool = False, **kwargs: Any) -> int:
        """
        Updates the model table after applying the filters from kwargs.

        You have to either pass a filter to narrow down a query or explicitly pass
        each=True flag to affect whole table.

        :param each: flag if whole table should be affected if no filter is passed
        :type each: bool
        :param kwargs: fields names and proper value types
        :type kwargs: Any
        :return: number of updated rows
        :rtype: int
        """
        self_fields = self.model.extract_db_own_fields().union(
            self.model.extract_related_names()
        )
        updates = {k: v for k, v in kwargs.items() if k in self_fields}
        updates = self.model.translate_columns_to_aliases(updates)
        if not each and not self.filter_clauses:
            raise QueryDefinitionError(
                "You cannot update without filtering the queryset first. "
                "If you want to update all rows use update(each=True, **kwargs)"
            )
        expr = FilterQuery(filter_clauses=self.filter_clauses).apply(
            self.table.update().values(**updates)
        )
        return await self.database.execute(expr)

    async def delete(self, each: bool = False, **kwargs: Any) -> int:
        """
        Deletes from the model table after applying the filters from kwargs.

        You have to either pass a filter to narrow down a query or explicitly pass
        each=True flag to affect whole table.

        :param each: flag if whole table should be affected if no filter is passed
        :type each: bool
        :param kwargs: fields names and proper value types
        :type kwargs: Any
        :return: number of deleted rows
        :rtype:int
        """
        if kwargs:
            return await self.filter(**kwargs).delete()
        if not each and not self.filter_clauses:
            raise QueryDefinitionError(
                "You cannot delete without filtering the queryset first. "
                "If you want to delete all rows use delete(each=True)"
            )
        expr = FilterQuery(filter_clauses=self.filter_clauses).apply(
            self.table.delete()
        )
        return await self.database.execute(expr)

    def limit(self, limit_count: int, limit_raw_sql: bool = None) -> "QuerySet":
        """
        You can limit the results to desired number of parent models.

        To limit the actual number of database query rows instead of number of main
        models use the `limit_raw_sql` parameter flag, and set it to `True`.

        :param limit_raw_sql: flag if raw sql should be limited
        :type limit_raw_sql: bool
        :param limit_count: number of models to limit
        :type limit_count: int
        :return: QuerySet
        :rtype: QuerySet
        """
        limit_raw_sql = self.limit_sql_raw if limit_raw_sql is None else limit_raw_sql
        return self.__class__(
            model_cls=self.model,
            filter_clauses=self.filter_clauses,
            exclude_clauses=self.exclude_clauses,
            select_related=self._select_related,
            limit_count=limit_count,
            offset=self.query_offset,
            columns=self._columns,
            exclude_columns=self._exclude_columns,
            order_bys=self.order_bys,
            prefetch_related=self._prefetch_related,
            limit_raw_sql=limit_raw_sql,
        )

    def offset(self, offset: int, limit_raw_sql: bool = None) -> "QuerySet":
        """
        You can also offset the results by desired number of main models.

        To offset the actual number of database query rows instead of number of main
        models use the `limit_raw_sql` parameter flag, and set it to `True`.

        :param limit_raw_sql: flag if raw sql should be offset
        :type limit_raw_sql: bool
        :param offset: numbers of models to offset
        :type offset: int
        :return: QuerySet
        :rtype: QuerySet
        """
        limit_raw_sql = self.limit_sql_raw if limit_raw_sql is None else limit_raw_sql
        return self.__class__(
            model_cls=self.model,
            filter_clauses=self.filter_clauses,
            exclude_clauses=self.exclude_clauses,
            select_related=self._select_related,
            limit_count=self.limit_count,
            offset=offset,
            columns=self._columns,
            exclude_columns=self._exclude_columns,
            order_bys=self.order_bys,
            prefetch_related=self._prefetch_related,
            limit_raw_sql=limit_raw_sql,
        )

    async def first(self, **kwargs: Any) -> "Model":
        """
        Gets the first row from the db ordered by primary key column ascending.

        :raises NoMatch: if no rows are returned
        :raises MultipleMatches: if more than 1 row is returned.
        :param kwargs: fields names and proper value types
        :type kwargs: Any
        :return: returned model
        :rtype: Model
        """
        if kwargs:
            return await self.filter(**kwargs).first()

        expr = self.build_select_expression(
            limit=1, order_bys=[f"{self.model.Meta.pkname}"] + self.order_bys
        )
        rows = await self.database.fetch_all(expr)
        processed_rows = self._process_query_result_rows(rows)
        if self._prefetch_related and processed_rows:
            processed_rows = await self._prefetch_related_models(processed_rows, rows)
        self.check_single_result_rows_count(processed_rows)
        return processed_rows[0]  # type: ignore

    async def get(self, **kwargs: Any) -> "Model":
        """
        Get's the first row from the db meeting the criteria set by kwargs.

        If no criteria set it will return the last row in db sorted by pk.

        Passing a criteria is actually calling filter(**kwargs) method described below.

        :raises NoMatch: if no rows are returned
        :raises MultipleMatches: if more than 1 row is returned.
        :param kwargs: fields names and proper value types
        :type kwargs: Any
        :return: returned model
        :rtype: Model
        """
        if kwargs:
            return await self.filter(**kwargs).get()

        if not self.filter_clauses:
            expr = self.build_select_expression(
                limit=1, order_bys=[f"-{self.model.Meta.pkname}"] + self.order_bys
            )
        else:
            expr = self.build_select_expression()

        rows = await self.database.fetch_all(expr)
        processed_rows = self._process_query_result_rows(rows)
        if self._prefetch_related and processed_rows:
            processed_rows = await self._prefetch_related_models(processed_rows, rows)
        self.check_single_result_rows_count(processed_rows)
        return processed_rows[0]  # type: ignore

    async def get_or_create(self, **kwargs: Any) -> "Model":
        """
        Combination of create and get methods.

        Tries to get a row meeting the criteria fro kwargs
        and if `NoMatch` exception is raised
        it creates a new one with given kwargs.

        :param kwargs: fields names and proper value types
        :type kwargs: Any
        :return: returned or created Model
        :rtype: Model
        """
        try:
            return await self.get(**kwargs)
        except NoMatch:
            return await self.create(**kwargs)

    async def update_or_create(self, **kwargs: Any) -> "Model":
        """
        Updates the model, or in case there is no match in database creates a new one.

        :param kwargs: fields names and proper value types
        :type kwargs: Any
        :return: updated or created model
        :rtype: Model
        """
        pk_name = self.model_meta.pkname
        if "pk" in kwargs:
            kwargs[pk_name] = kwargs.pop("pk")
        if pk_name not in kwargs or kwargs.get(pk_name) is None:
            return await self.create(**kwargs)
        model = await self.get(pk=kwargs[pk_name])
        return await model.update(**kwargs)

    async def all(self, **kwargs: Any) -> Sequence[Optional["Model"]]:  # noqa: A003
        """
        Returns all rows from a database for given model for set filter options.

        Passing kwargs is a shortcut and equals to calling `filter(**kwrags).all()`.

        If there are no rows meeting the criteria an empty list is returned.

        :param kwargs: fields names and proper value types
        :type kwargs: Any
        :return: list of returned models
        :rtype: List[Model]
        """
        if kwargs:
            return await self.filter(**kwargs).all()

        expr = self.build_select_expression()
        rows = await self.database.fetch_all(expr)
        result_rows = self._process_query_result_rows(rows)
        if self._prefetch_related and result_rows:
            result_rows = await self._prefetch_related_models(result_rows, rows)

        return result_rows

    async def create(self, **kwargs: Any) -> "Model":
        """
        Creates the model instance, saves it in a database and returns the updates model
        (with pk populated if not passed and autoincrement is set).

        The allowed kwargs are `Model` fields names and proper value types.

        :param kwargs: fields names and proper value types
        :type kwargs: Any
        :return: created model
        :rtype: Model
        """
        new_kwargs = dict(**kwargs)
        new_kwargs = self.model.prepare_model_to_save(new_kwargs)

        expr = self.table.insert()
        expr = expr.values(**new_kwargs)

        instance = self.model(**kwargs)
        await self.model.Meta.signals.pre_save.send(
            sender=self.model, instance=instance
        )
        pk = await self.database.execute(expr)

        pk_name = self.model.get_column_alias(self.model_meta.pkname)
        if pk_name not in kwargs and pk_name in new_kwargs:
            instance.pk = new_kwargs[self.model_meta.pkname]
        if pk and isinstance(pk, self.model.pk_type()):
            instance.pk = pk

        # refresh server side defaults
        if any(
            field.server_default is not None
            for name, field in self.model.Meta.model_fields.items()
            if name not in kwargs
        ):
            instance = await instance.load()
        instance.set_save_status(True)
        await self.model.Meta.signals.post_save.send(
            sender=self.model, instance=instance
        )
        return instance

    async def bulk_create(self, objects: List["Model"]) -> None:
        """
        Performs a bulk update in one database session to speed up the process.

        Allows you to create multiple objects at once.

        A valid list of `Model` objects needs to be passed.

        Bulk operations do not send signals.

        :param objects: list of ormar models already initialized and ready to save.
        :type objects: List[Model]
        """
        ready_objects = []
        for objt in objects:
            new_kwargs = objt.dict()
            new_kwargs = objt.prepare_model_to_save(new_kwargs)
            ready_objects.append(new_kwargs)

        expr = self.table.insert()
        await self.database.execute_many(expr, ready_objects)

        for objt in objects:
            objt.set_save_status(True)

    async def bulk_update(  # noqa:  CCR001
        self, objects: List["Model"], columns: List[str] = None
    ) -> None:
        """
        Performs bulk update in one database session to speed up the process.

        Allows to update multiple instance at once.

        All `Models` passed need to have primary key column populated.

        You can also select which fields to update by passing `columns` list
        as a list of string names.

        Bulk operations do not send signals.

        :param objects: list of ormar models
        :type objects: List[Model]
        :param columns: list of columns to update
        :type columns: List[str]
        """
        ready_objects = []
        pk_name = self.model_meta.pkname
        if not columns:
            columns = list(
                self.model.extract_db_own_fields().union(
                    self.model.extract_related_names()
                )
            )

        if pk_name not in columns:
            columns.append(pk_name)

        columns = [self.model.get_column_alias(k) for k in columns]

        for objt in objects:
            new_kwargs = objt.dict()
            if pk_name not in new_kwargs or new_kwargs.get(pk_name) is None:
                raise ModelPersistenceError(
                    "You cannot update unsaved objects. "
                    f"{self.model.__name__} has to have {pk_name} filled."
                )
            new_kwargs = self.model.substitute_models_with_pks(new_kwargs)
            new_kwargs = self.model.translate_columns_to_aliases(new_kwargs)
            new_kwargs = {"new_" + k: v for k, v in new_kwargs.items() if k in columns}
            ready_objects.append(new_kwargs)

        pk_column = self.model_meta.table.c.get(self.model.get_column_alias(pk_name))
        pk_column_name = self.model.get_column_alias(pk_name)
        table_columns = [c.name for c in self.model_meta.table.c]
        expr = self.table.update().where(
            pk_column == bindparam("new_" + pk_column_name)
        )
        expr = expr.values(
            **{
                k: bindparam("new_" + k)
                for k in columns
                if k != pk_column_name and k in table_columns
            }
        )
        # databases bind params only where query is passed as string
        # otherwise it just passes all data to values and results in unconsumed columns
        expr = str(expr)
        await self.database.execute_many(expr, ready_objects)

        for objt in objects:
            objt.set_save_status(True)
