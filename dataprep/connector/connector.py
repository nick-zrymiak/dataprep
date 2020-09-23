"""
This module contains the Connector class.
Every data fetching action should begin with instantiating this Connector class.
"""
import math
import sys
from asyncio import as_completed
from math import gcd, log10, pow
from pathlib import Path
from typing import Any, Awaitable, Dict, List, Optional, Union, Tuple
from aiohttp.client_reqrep import ClientResponse
from jsonpath_ng import parse as jparse
import pandas as pd
from aiohttp import ClientSession
from jinja2 import Environment, StrictUndefined, Template, UndefinedError
from warnings import warn
from ..errors import UnreachableError
from .config_manager import config_directory, ensure_config
from .errors import InvalidParameterError, RequestError, UniversalParameterOverridden
from .implicit_database import ImplicitDatabase, ImplicitTable
from .ref import Ref
from .schema import (
    ConfigDef,
    FieldDefUnion,
    OffsetPaginationDef,
    SeekPaginationDef,
    PagePaginationDef,
    TokenPaginationDef,
    TokenLocation,
)
from .throttler import Throttler, OrderedThrottleSession

INFO_TEMPLATE = Template(
    """{% for tb in tbs.keys() %}
Table {{dbname}}.{{tb}}

Parameters
----------
{% if tbs[tb].required_params %}{{", ".join(tbs[tb].required_params)}} required {% endif %}
{% if tbs[tb].optional_params %}{{", ".join(tbs[tb].optional_params)}} optional {% endif %}

Examples
--------
>>> dc.query({{", ".join(["\\\"{}\\\"".format(tb)] + tbs[tb].joined_query_fields)}})
>>> dc.show_schema("{{tb}}")
{% endfor %}
"""
)


class Connector:
    """This is the main class of the connector component.
    Initialize Connector class as the example code.

    Parameters
    ----------
    config_path
        The path to the config. It can be hosted, e.g. "yelp", or from
        local filesystem, e.g. "./yelp"
    _auth: Optional[Dict[str, Any]] = None
        The parameters for authentication, e.g. OAuth2
    _concurrency: int = 5
        The concurrency setting. By default it is 1 reqs/sec.
    **kwargs
        Parameters that shared by different queries.

    Example
    -------
    >>> from dataprep.connector import Connector
    >>> dc = Connector("yelp", _auth={"access_token": access_token})
    """

    _impdb: ImplicitDatabase
    # Varibles that used across different queries, can be overriden by query
    _vars: Dict[str, Any]
    _auth: Dict[str, Any]
    # storage for authorization
    _storage: Dict[str, Any]
    _concurrency: float
    _jenv: Environment
    _throttler: Throttler
    start_quota = None
    current_quota = None

    def __init__(
        self,
        config_path: str,
        _auth: Optional[Dict[str, Any]] = None,
        _concurrency: float = 1,
        **kwargs: Any,
    ) -> None:
        if (
            config_path.startswith(".")
            or config_path.startswith("/")
            or config_path.startswith("~")
        ):
            path = Path(config_path).resolve()
        else:
            # From Github!
            ensure_config(config_path)
            path = config_directory() / config_path

        self._impdb = ImplicitDatabase(path)

        self._vars = kwargs
        self._auth = _auth or {}
        self._storage = {}
        self._concurrency = _concurrency
        self._jenv = Environment(undefined=StrictUndefined)
        gcd = float_gcd(_concurrency, 1)
        self._throttler = Throttler(int(_concurrency / gcd), int(1 / gcd))

    async def query(  # pylint: disable=too-many-locals
        self,
        table: str,
        *,
        _auth: Optional[Dict[str, Any]] = None,
        _count: Optional[int] = None,
        **where: Any,
    ) -> Union[Awaitable[pd.DataFrame], pd.DataFrame]:
        """
        Query the API to get a table.

        Parameters
        ----------
        table
            The table name.
        _auth: Optional[Dict[str, Any]] = None
            The parameters for authentication. Usually the authentication parameters
            should be defined when instantiating the Connector. In case some tables have different
            authentication options, a different authentication parameter can be defined here.
            This parameter will override the one from Connector if passed.
        _count: Optional[int] = None
            Count of returned records.
        **where
            The additional parameters required for the query.
        """
        allowed_params = self._impdb.tables[table].config.request.params
        for key in where:
            if key not in allowed_params:
                raise InvalidParameterError(key)

        return await self._query_imp(table, where, _auth=_auth, _count=_count)

    @property
    def table_names(self) -> List[str]:
        """
        Return all the names of the available tables in a list.
        Note
        ----
        We abstract each website as a database containing several tables.
        For example in Spotify, we have artist and album table.
        """
        return list(self._impdb.tables.keys())

    def info(self) -> None:
        """
        Show the basic information and provide guidance for users to issue queries.
        """

        # get info
        tbs: Dict[str, Any] = {}
        for cur_table in self._impdb.tables:
            table_config_content: ConfigDef = self._impdb.tables[cur_table].config
            params_required = []
            params_optional = []
            example_query_fields = []
            count = 1
            for k, val in table_config_content.request.params.items():
                if isinstance(val, bool) and val:
                    params_required.append(k)
                    example_query_fields.append(f"""{k}="word{count}\"""")
                    count += 1
                elif isinstance(val, bool):
                    params_optional.append(k)
            tbs[cur_table] = {}
            tbs[cur_table]["required_params"] = params_required
            tbs[cur_table]["optional_params"] = params_optional
            tbs[cur_table]["joined_query_fields"] = example_query_fields

        # show table info
        print(
            INFO_TEMPLATE.render(
                ntables=len(self.table_names), dbname=self._impdb.name, tbs=tbs
            )
        )

    def show_schema(self, table_name: str) -> pd.DataFrame:
        """This method shows the schema of the table that will be returned,
        so that the user knows what information to expect.

        Parameters
        ----------
        table_name
            The table name.

        Returns
        -------
        pd.DataFrame
            The returned data's schema.

        Note
        ----
        The schema is defined in the configuration file.
        The user can either use the default one or change it by editing the configuration file.
        """
        print(f"table: {table_name}")
        table_config_content: ConfigDef = self._impdb.tables[table_name].config
        schema = table_config_content.response.schema_
        new_schema_dict: Dict[str, List[Any]] = {}
        new_schema_dict["column_name"] = []
        new_schema_dict["data_type"] = []
        for k in schema.keys():
            new_schema_dict["column_name"].append(k)
            new_schema_dict["data_type"].append(schema[k].type)
        return pd.DataFrame.from_dict(new_schema_dict)

    async def _query_imp(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
        self,
        table: str,
        kwargs: Dict[str, Any],
        *,
        _auth: Optional[Dict[str, Any]] = None,
        _count: Optional[int] = None,
    ) -> pd.DataFrame:
        if table not in self._impdb.tables:
            raise ValueError(f"No such table {table} in {self._impdb.name}")

        itable = self._impdb.tables[table]
        reqconf = itable.config.request

        if reqconf.pagination is None and _count is not None:
            print(
                f"ignoring _count since {table} has no pagination settings",
                file=sys.stderr,
            )

        if _count is not None and _count <= 0:
            raise RuntimeError("_count should be larger than 0")

        async with ClientSession() as client:

            throttler = self._throttler.session()

            if reqconf.pagination is None or _count is None:
                df = await self._fetch(
                    itable, kwargs, _client=client, _throttler=throttler, _auth=_auth,
                )
                return df

            pagdef = reqconf.pagination

            # pagination begins
            max_per_page = pagdef.max_count
            total = _count
            n_page = math.ceil(total / max_per_page)

            if isinstance(pagdef, SeekPaginationDef):
                last_id = 0
                dfs = []
                # No way to parallelize for seek type
                for i in range(n_page):
                    count = min(total - i * max_per_page, max_per_page)

                    df = await self._fetch(
                        itable,
                        kwargs,
                        _client=client,
                        _throttler=throttler,
                        _page=i,
                        _auth=_auth,
                        _limit=count,
                        _anchor=last_id - 1,
                    )

                    if df is None:
                        raise NotImplementedError

                    if len(df) == 0:
                        # The API returns empty for this page, maybe we've reached the end
                        break

                    last_id = int(df.iloc[-1, df.columns.get_loc(pagdef.seek_id)]) - 1  # type: ignore
                    dfs.append(df)
            elif isinstance(pagdef, TokenPaginationDef):
                next_token = None
                dfs = []
                # No way to parallelize for seek type
                for i in range(n_page):
                    count = min(total - i * max_per_page, max_per_page)
                    df, resp = await self._fetch(  # type: ignore
                        itable,
                        kwargs,
                        _client=client,
                        _throttler=throttler,
                        _page=i,
                        _auth=_auth,
                        _limit=count,
                        _anchor=next_token,
                        _raw=True,
                    )

                    if pagdef.token_location == TokenLocation.Header:
                        next_token = resp.headers[pagdef.token_accessor]
                    elif pagdef.token_location == TokenLocation.Body:
                        # only json body implemented
                        token_expr = jparse(pagdef.token_accessor)
                        (token_elem,) = token_expr.find(await resp.json())
                        next_token = token_elem.value

                    dfs.append(df)
            elif isinstance(pagdef, (OffsetPaginationDef, PagePaginationDef)):
                resps_coros = []
                allowed_page = Ref(n_page)
                for i in range(n_page):
                    count = min(total - i * max_per_page, max_per_page)
                    if pagdef.type == "offset":
                        anchor = i * max_per_page
                    elif pagdef.type == "page":
                        anchor = i + 1
                    else:
                        raise ValueError(f"Unknown pagination type {pagdef.type}")

                    resps_coros.append(
                        self._fetch(
                            itable,
                            kwargs,
                            _client=client,
                            _throttler=throttler,
                            _page=i,
                            _allowed_page=allowed_page,
                            _auth=_auth,
                            _limit=count,
                            _anchor=anchor,
                        )
                    )

                dfs = []
                for resp_coro in as_completed(resps_coros):
                    df = await resp_coro
                    if df is not None:
                        dfs.append(df)
            else:
                raise NotImplementedError

        df = pd.concat(dfs, axis=0).reset_index(drop=True)

        return df

    async def _fetch(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
        self,
        table: ImplicitTable,
        kwargs: Dict[str, Any],
        *,
        _client: ClientSession,
        _throttler: OrderedThrottleSession,
        _page: int = 0,
        _allowed_page: Optional[Ref[int]] = None,
        _limit: Optional[int] = None,
        _anchor: Optional[Any] = None,
        _auth: Optional[Dict[str, Any]] = None,
        _raw: bool = False,
    ) -> Union[Optional[pd.DataFrame], Tuple[Optional[pd.DataFrame], ClientResponse]]:

        reqdef = table.config.request
        method = reqdef.method
        url = reqdef.url
        req_data: Dict[str, Dict[str, Any]] = {
            "headers": {},
            "params": {},
            "cookies": {},
        }
        merged_vars = {**self._vars, **kwargs}

        if reqdef.authorization is not None:
            reqdef.authorization.build(req_data, _auth or self._auth, self._storage)

        for key in ["headers", "params", "cookies"]:
            field_def = getattr(reqdef, key, None)
            if field_def is not None:
                instantiated_fields = populate_field(field_def, self._jenv, merged_vars)
                req_data[key].update(**instantiated_fields)

        if reqdef.body is not None:
            # TODO: do we support binary body?
            instantiated_fields = populate_field(
                reqdef.body.content, self._jenv, merged_vars
            )
            if reqdef.body.ctype == "application/x-www-form-urlencoded":
                req_data["data"] = instantiated_fields
            elif reqdef.body.ctype == "application/json":
                req_data["json"] = instantiated_fields
            else:
                raise NotImplementedError(reqdef.body.ctype)

        if reqdef.pagination is not None and _limit is not None:
            pagdef = reqdef.pagination
            limit_key = pagdef.limit_key

            if isinstance(pagdef, SeekPaginationDef):
                anchor = pagdef.seek_key
            elif isinstance(pagdef, OffsetPaginationDef):
                anchor = pagdef.offset_key
            elif isinstance(pagdef, PagePaginationDef):
                anchor = pagdef.page_key
            elif isinstance(pagdef, TokenPaginationDef):
                anchor = pagdef.token_key
            else:
                raise ValueError(f"Unknown pagination type {pagdef.type}.")

            if limit_key in req_data["params"]:
                raise UniversalParameterOverridden(limit_key, "_limit")
            req_data["params"][limit_key] = _limit

            if anchor in req_data["params"]:
                raise UniversalParameterOverridden(anchor, "_offset")

            if _anchor is not None:
                req_data["params"][anchor] = _anchor

        while True:
            async with _throttler.acquire(_page) as (cancel, fail):

                if _allowed_page is not None and int(_allowed_page) <= _page:
                    # cancel current throttler counter since the request is not sent out
                    cancel()
                    return None

                async with _client.request(
                    method=method,
                    url=url,
                    headers=req_data["headers"],
                    params=req_data["params"],
                    json=req_data.get("json"),
                    data=req_data.get("data"),
                    cookies=req_data["cookies"],
                ) as resp:

                    if "ratelimit-resettime" in resp.headers:
                        # print(
                        #     "reset",
                        #     resp.headers["ratelimit-resettime"],
                        #     "limit",
                        #     resp.headers["ratelimit-dailylimit"],
                        #     "remaining",
                        #     resp.headers["ratelimit-remaining"],
                        # )
                        if self.start_quota is None:
                            self.start_quota = resp.headers["ratelimit-remaining"]
                        self.current_quota = resp.headers["ratelimit-remaining"]

                    if resp.status == 429:
                        print(await resp.text())
                        print(resp.header)
                        fail()
                        # warn(
                        #     f"HTTP 429 error, decreasing the concurrency level to {_throttler.req_per_window}",
                        #     RuntimeWarning,
                        # )
                        continue
                    elif resp.status != 200:
                        raise RequestError(
                            status_code=resp.status, message=await resp.text()
                        )

                    content = await resp.text()
                    break

        df = table.from_response(content)

        if len(df) == 0 and _allowed_page is not None and _page is not None:
            _allowed_page.set(_page)
            df = None

        if _raw:
            return df, resp
        else:
            return df


def populate_field(  # pylint: disable=too-many-branches
    fields: Dict[str, FieldDefUnion], jenv: Environment, params: Dict[str, Any]
) -> Dict[str, str]:
    """Populate a dict based on the fields definition and provided vars."""

    ret: Dict[str, str] = {}

    for key, def_ in fields.items():
        from_key, to_key = key, key

        if isinstance(def_, bool):
            required = def_
            value = params.get(from_key)
            if value is None and required:
                raise KeyError(from_key)
            remove_if_empty = False
        elif isinstance(def_, str):
            # is a template
            tmplt = jenv.from_string(def_)
            value = tmplt.render(**params)
            remove_if_empty = False
        else:
            template = def_.template
            remove_if_empty = def_.remove_if_empty
            to_key = def_.to_key or to_key
            from_key = def_.from_key or from_key

            if template is None:
                required = def_.required
                value = params.get(from_key)
                if value is None and required:
                    raise KeyError(from_key)
            else:
                tmplt = jenv.from_string(template)
                try:
                    value = tmplt.render(**params)
                except UndefinedError:
                    value = ""  # This empty string will be removed if `remove_if_empty` is True

        if value is not None:
            str_value = str(value)
            if not (remove_if_empty and not str_value):
                if to_key in ret:
                    print(f"Param {key} conflicting with {to_key}", file=sys.stderr)
                ret[to_key] = str_value
                continue
    return ret


def float_scale(x: float) -> int:
    max_digits = 14
    int_part = int(abs(x))
    magnitude = 1 if int_part == 0 else int(log10(int_part)) + 1
    if magnitude >= max_digits:
        return 0
    frac_part = abs(x) - int_part
    multiplier = 10 ** (max_digits - magnitude)
    frac_digits = multiplier + int(multiplier * frac_part + 0.5)
    while frac_digits % 10 == 0:
        frac_digits /= 10
    return int(log10(frac_digits))


def float_gcd(a: float, b: float) -> float:
    sc = float_scale(a)
    sc_b = float_scale(b)
    sc = sc_b if sc_b > sc else sc
    fac = pow(10, sc)

    a = int(round(a * fac))
    b = int(round(b * fac))

    return round(gcd(a, b) / fac, sc)
