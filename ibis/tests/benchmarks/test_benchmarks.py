import copy
import functools
import inspect
import itertools
import string
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import tomli
from packaging.version import parse as vparse

import ibis
import ibis.expr.datatypes as dt
import ibis.expr.types as ir
from ibis.backends.pandas.udf import udf


def make_t():
    return ibis.table(
        [
            ('_timestamp', 'int32'),
            ('dim1', 'int32'),
            ('dim2', 'int32'),
            ('valid_seconds', 'int32'),
            ('meas1', 'int32'),
            ('meas2', 'int32'),
            ('year', 'int32'),
            ('month', 'int32'),
            ('day', 'int32'),
            ('hour', 'int32'),
            ('minute', 'int32'),
        ],
        name="t",
    )


@pytest.fixture(scope="module")
def t():
    return make_t()


def make_base(t):
    return (
        (t.year > 2016)
        | ((t.year == 2016) & (t.month > 6))
        | ((t.year == 2016) & (t.month == 6) & (t.day > 6))
        | ((t.year == 2016) & (t.month == 6) & (t.day == 6) & (t.hour > 6))
        | (
            (t.year == 2016)
            & (t.month == 6)
            & (t.day == 6)
            & (t.hour == 6)
            & (t.minute >= 5)
        )
    ) & (
        (t.year < 2016)
        | ((t.year == 2016) & (t.month < 6))
        | ((t.year == 2016) & (t.month == 6) & (t.day < 6))
        | ((t.year == 2016) & (t.month == 6) & (t.day == 6) & (t.hour < 6))
        | (
            (t.year == 2016)
            & (t.month == 6)
            & (t.day == 6)
            & (t.hour == 6)
            & (t.minute <= 5)
        )
    )


@pytest.fixture(scope="module")
def base(t):
    return make_base(t)


def make_large_expr(t, base):
    src_table = t[base]
    src_table = src_table.mutate(
        _timestamp=(src_table['_timestamp'] - src_table['_timestamp'] % 3600)
        .cast('int32')
        .name('_timestamp'),
        valid_seconds=300,
    )

    aggs = []
    for meas in ['meas1', 'meas2']:
        aggs.append(src_table[meas].sum().cast('float').name(meas))
    src_table = src_table.aggregate(
        aggs, by=['_timestamp', 'dim1', 'dim2', 'valid_seconds']
    )

    part_keys = ['year', 'month', 'day', 'hour', 'minute']
    ts_col = src_table['_timestamp'].cast('timestamp')
    new_cols = {}
    for part_key in part_keys:
        part_col = getattr(ts_col, part_key)()
        new_cols[part_key] = part_col
    src_table = src_table.mutate(**new_cols)
    return src_table[
        [
            '_timestamp',
            'dim1',
            'dim2',
            'meas1',
            'meas2',
            'year',
            'month',
            'day',
            'hour',
            'minute',
        ]
    ]


@pytest.fixture(scope="module")
def large_expr(t, base):
    return make_large_expr(t, base)


@pytest.mark.benchmark(group="construction")
@pytest.mark.parametrize(
    "construction_fn",
    [
        pytest.param(lambda *_: make_t(), id="small"),
        pytest.param(lambda t, *_: make_base(t), id="medium"),
        pytest.param(lambda t, base: make_large_expr(t, base), id="large"),
    ],
)
def test_construction(benchmark, construction_fn, t, base):
    benchmark(construction_fn, t, base)


@pytest.mark.benchmark(group="builtins")
@pytest.mark.parametrize(
    "expr_fn",
    [
        pytest.param(lambda t, _base, _large_expr: t, id="small"),
        pytest.param(lambda _t, base, _large_expr: base, id="medium"),
        pytest.param(lambda _t, _base, large_expr: large_expr, id="large"),
    ],
)
@pytest.mark.parametrize("builtin", [hash, str])
def test_builtins(benchmark, expr_fn, builtin, t, base, large_expr):
    expr = expr_fn(t, base, large_expr)
    benchmark(builtin, expr)


_backends = tomli.loads(
    Path(ibis.__file__).parent.parent.joinpath("pyproject.toml").read_text()
)["tool"]["poetry"]["plugins"]["ibis.backends"]

# spark is a duplicate of pyspark and pandas compilation is a no-op
del _backends["spark"], _backends["pandas"]


_XFAIL_COMPILE_BACKENDS = {"dask", "datafusion", "pyspark"}


@pytest.mark.benchmark(group="compilation")
@pytest.mark.parametrize(
    "module",
    [
        pytest.param(
            mod,
            marks=pytest.mark.xfail(
                condition=mod in _XFAIL_COMPILE_BACKENDS,
                reason=f"{mod} backend doesn't support compiling UnboundTable",
            ),
        )
        for mod in _backends.keys()
    ],
)
@pytest.mark.parametrize(
    "expr_fn",
    [
        pytest.param(lambda t, _base, _large_expr: t, id="small"),
        pytest.param(lambda _t, base, _large_expr: base, id="medium"),
        pytest.param(lambda _t, _base, large_expr: large_expr, id="large"),
    ],
)
def test_compile(benchmark, module, expr_fn, t, base, large_expr):
    try:
        mod = getattr(ibis, module)
    except AttributeError as e:
        pytest.skip(str(e))
    else:
        expr = expr_fn(t, base, large_expr)
        benchmark(mod.compile, expr)


@pytest.fixture(scope="module")
def pt():
    n = 60_000
    data = pd.DataFrame(
        {
            'key': np.random.choice(16000, size=n),
            'low_card_key': np.random.choice(30, size=n),
            'value': np.random.rand(n),
            'timestamps': pd.date_range(
                start='now', periods=n, freq='s'
            ).values,
            'timestamp_strings': pd.date_range(
                start='now', periods=n, freq='s'
            ).values.astype(str),
            'repeated_timestamps': pd.date_range(
                start='2018-09-01', periods=30
            ).repeat(int(n / 30)),
        }
    )

    return ibis.pandas.connect(dict(df=data)).table('df')


def high_card_group_by(t):
    return t.groupby(t.key).aggregate(avg_value=t.value.mean())


def cast_to_dates(t):
    return t.timestamps.cast(dt.date)


def cast_to_dates_from_strings(t):
    return t.timestamp_strings.cast(dt.date)


def multikey_group_by_with_mutate(t):
    return (
        t.mutate(dates=t.timestamps.cast('date'))
        .groupby(['low_card_key', 'dates'])
        .aggregate(avg_value=lambda t: t.value.mean())
    )


def simple_sort(t):
    return t.sort_by([t.key])


def simple_sort_projection(t):
    return t[['key', 'value']].sort_by(['key'])


def multikey_sort(t):
    return t.sort_by(['low_card_key', 'key'])


def multikey_sort_projection(t):
    return t[['low_card_key', 'key', 'value']].sort_by(['low_card_key', 'key'])


def low_card_rolling_window(t):
    return ibis.trailing_range_window(
        ibis.interval(days=2),
        order_by=t.repeated_timestamps,
        group_by=t.low_card_key,
    )


def low_card_grouped_rolling(t):
    return t.value.mean().over(low_card_rolling_window(t))


def high_card_rolling_window(t):
    return ibis.trailing_range_window(
        ibis.interval(days=2),
        order_by=t.repeated_timestamps,
        group_by=t.key,
    )


def high_card_grouped_rolling(t):
    return t.value.mean().over(high_card_rolling_window(t))


@udf.reduction(['double'], 'double')
def my_mean(series):
    return series.mean()


def low_card_grouped_rolling_udf_mean(t):
    return my_mean(t.value).over(low_card_rolling_window(t))


def high_card_grouped_rolling_udf_mean(t):
    return my_mean(t.value).over(high_card_rolling_window(t))


@udf.analytic(['double'], 'double')
def my_zscore(series):
    return (series - series.mean()) / series.std()


def low_card_window(t):
    return ibis.window(group_by=t.low_card_key)


def high_card_window(t):
    return ibis.window(group_by=t.key)


def low_card_window_analytics_udf(t):
    return my_zscore(t.value).over(low_card_window(t))


def high_card_window_analytics_udf(t):
    return my_zscore(t.value).over(high_card_window(t))


@udf.reduction(['double', 'double'], 'double')
def my_wm(v, w):
    return np.average(v, weights=w)


def low_card_grouped_rolling_udf_wm(t):
    return my_wm(t.value, t.value).over(low_card_rolling_window(t))


def high_card_grouped_rolling_udf_wm(t):
    return my_wm(t.value, t.value).over(low_card_rolling_window(t))


broken_pandas_grouped_rolling = pytest.mark.xfail(
    condition=vparse("1.4") <= vparse(pd.__version__) < vparse("1.4.2"),
    raises=ValueError,
    reason="https://github.com/pandas-dev/pandas/pull/44068",
)


@pytest.mark.benchmark(group="execution")
@pytest.mark.parametrize(
    "expression_fn",
    [
        pytest.param(high_card_group_by, id="high_card_group_by"),
        pytest.param(cast_to_dates, id="cast_to_dates"),
        pytest.param(
            cast_to_dates_from_strings, id="cast_to_dates_from_strings"
        ),
        pytest.param(
            multikey_group_by_with_mutate, id="multikey_group_by_with_mutate"
        ),
        pytest.param(simple_sort, id="simple_sort"),
        pytest.param(simple_sort_projection, id="simple_sort_projection"),
        pytest.param(multikey_sort, id="multikey_sort"),
        pytest.param(multikey_sort_projection, id="multikey_sort_projection"),
        pytest.param(
            low_card_grouped_rolling,
            id="low_card_grouped_rolling",
            marks=[broken_pandas_grouped_rolling],
        ),
        pytest.param(
            high_card_grouped_rolling,
            id="high_card_grouped_rolling",
            marks=[broken_pandas_grouped_rolling],
        ),
        pytest.param(
            low_card_grouped_rolling_udf_mean,
            id="low_card_grouped_rolling_udf_mean",
            marks=[broken_pandas_grouped_rolling],
        ),
        pytest.param(
            high_card_grouped_rolling_udf_mean,
            id="high_card_grouped_rolling_udf_mean",
            marks=[broken_pandas_grouped_rolling],
        ),
        pytest.param(
            low_card_window_analytics_udf, id="low_card_window_analytics_udf"
        ),
        pytest.param(
            high_card_window_analytics_udf, id="high_card_window_analytics_udf"
        ),
        pytest.param(
            low_card_grouped_rolling_udf_wm,
            id="low_card_grouped_rolling_udf_wm",
            marks=[broken_pandas_grouped_rolling],
        ),
        pytest.param(
            high_card_grouped_rolling_udf_wm,
            id="high_card_grouped_rolling_udf_wm",
            marks=[broken_pandas_grouped_rolling],
        ),
    ],
)
def test_execute(benchmark, expression_fn, pt):
    expr = expression_fn(pt)
    benchmark(expr.execute)


@pytest.fixture(scope="module")
def part():
    return ibis.table(
        dict(
            p_partkey="int64",
            p_size="int64",
            p_type="string",
            p_mfgr="string",
        ),
        name="part",
    )


@pytest.fixture(scope="module")
def supplier():
    return ibis.table(
        dict(
            s_suppkey="int64",
            s_nationkey="int64",
            s_name="string",
            s_acctbal="decimal(15, 3)",
            s_address="string",
            s_phone="string",
            s_comment="string",
        ),
        name="supplier",
    )


@pytest.fixture(scope="module")
def partsupp():
    return ibis.table(
        dict(
            ps_partkey="int64",
            ps_suppkey="int64",
            ps_supplycost="decimal(15, 3)",
        ),
        name="partsupp",
    )


@pytest.fixture(scope="module")
def nation():
    return ibis.table(
        dict(n_nationkey="int64", n_regionkey="int64", n_name="string"),
        name="nation",
    )


@pytest.fixture(scope="module")
def region():
    return ibis.table(
        dict(r_regionkey="int64", r_name="string"), name="region"
    )


@pytest.fixture(scope="module")
def tpc_h02(part, supplier, partsupp, nation, region):
    REGION = "EUROPE"
    SIZE = 25
    TYPE = "BRASS"

    expr = (
        part.join(partsupp, part.p_partkey == partsupp.ps_partkey)
        .join(supplier, supplier.s_suppkey == partsupp.ps_suppkey)
        .join(nation, supplier.s_nationkey == nation.n_nationkey)
        .join(region, nation.n_regionkey == region.r_regionkey)
    )

    subexpr = (
        partsupp.join(supplier, supplier.s_suppkey == partsupp.ps_suppkey)
        .join(nation, supplier.s_nationkey == nation.n_nationkey)
        .join(region, nation.n_regionkey == region.r_regionkey)
    )

    subexpr = subexpr[
        (subexpr.r_name == REGION) & (expr.p_partkey == subexpr.ps_partkey)
    ]

    filters = [
        expr.p_size == SIZE,
        expr.p_type.like(f"%{TYPE}"),
        expr.r_name == REGION,
        expr.ps_supplycost == subexpr.ps_supplycost.min(),
    ]
    q = expr.filter(filters)

    q = q.select(
        [
            q.s_acctbal,
            q.s_name,
            q.n_name,
            q.p_partkey,
            q.p_mfgr,
            q.s_address,
            q.s_phone,
            q.s_comment,
        ]
    )

    return q.sort_by(
        [
            ibis.desc(q.s_acctbal),
            q.n_name,
            q.s_name,
            q.p_partkey,
        ]
    ).limit(100)


@pytest.mark.benchmark(group="repr")
def test_repr_tpc_h02(benchmark, tpc_h02):
    benchmark(repr, tpc_h02)


@pytest.mark.benchmark(group="repr")
def test_repr_huge_union(benchmark):
    n = 10
    raw_types = [
        "int64",
        "float64",
        "string",
        "array<struct<a: array<string>, b: map<string, array<int64>>>>",
    ]
    tables = [
        ibis.table(
            list(zip(string.ascii_letters, itertools.cycle(raw_types))),
            name=f"t{i:d}",
        )
        for i in range(n)
    ]
    expr = functools.reduce(ir.Table.union, tables)
    benchmark(repr, expr)


@pytest.mark.benchmark(group="node_args")
def test_op_argnames(benchmark):
    t = ibis.table([("a", "int64")])
    expr = t[["a"]]
    benchmark(lambda op: op.argnames, expr.op())


@pytest.mark.benchmark(group="node_args")
def test_op_args(benchmark):
    t = ibis.table([("a", "int64")])
    expr = t[["a"]]
    benchmark(lambda op: op.args, expr.op())


@pytest.mark.benchmark(group="datatype")
def test_complex_datatype_parse(benchmark):
    type_str = "array<struct<a: array<string>, b: map<string, array<int64>>>>"
    expected = dt.Array(
        dt.Struct.from_dict(
            dict(
                a=dt.Array(dt.string), b=dt.Map(dt.string, dt.Array(dt.int64))
            )
        )
    )
    assert dt.parse(type_str) == expected
    benchmark(dt.parse, type_str)


@pytest.mark.benchmark(group="datatype")
@pytest.mark.parametrize("func", [str, hash])
def test_complex_datatype_builtins(benchmark, func):
    datatype = dt.Array(
        dt.Struct.from_dict(
            dict(
                a=dt.Array(dt.string), b=dt.Map(dt.string, dt.Array(dt.int64))
            )
        )
    )
    benchmark(func, datatype)


@pytest.mark.benchmark(group="equality")
def test_large_expr_equals(benchmark, tpc_h02):
    benchmark(ir.Expr.equals, tpc_h02, copy.deepcopy(tpc_h02))


@pytest.mark.benchmark(group="datatype")
@pytest.mark.parametrize(
    "dtypes",
    [
        pytest.param(
            [
                obj
                for _, obj in inspect.getmembers(
                    dt,
                    lambda obj: isinstance(obj, dt.DataType),
                )
            ],
            id="singletons",
        ),
        pytest.param(
            dt.Array(
                dt.Struct.from_dict(
                    dict(
                        a=dt.Array(dt.string),
                        b=dt.Map(dt.string, dt.Array(dt.int64)),
                    )
                )
            ),
            id="complex",
        ),
    ],
)
def test_eq_datatypes(benchmark, dtypes):
    def eq(a, b):
        assert a == b

    benchmark(eq, dtypes, copy.deepcopy(dtypes))
