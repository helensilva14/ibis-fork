# Copyright 2014 Cloudera Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import pytest

import ibis
import ibis.expr.types as ir
from ibis.tests.expr.mocks import MockBackend
from ibis.tests.util import assert_equal


def test_warns_on_deprecated_import():
    with pytest.warns(FutureWarning, match=r"ibis\.expr\.analytics"):
        import ibis.expr.analytics  # noqa: F401


@pytest.fixture
def con():
    return MockBackend()


@pytest.fixture
def alltypes(con):
    return con.table('functional_alltypes')


@pytest.fixture
def airlines():
    return ibis.table(
        [('dest', 'string'), ('origin', 'string'), ('arrdelay', 'int32')],
        'airlines',
    )


def test_category_project(alltypes):
    t = alltypes

    tier = t.double_col.bucket([0, 50, 100]).name('tier')
    expr = t[tier, t]

    assert isinstance(expr.tier, ir.CategoryColumn)


def test_bucket(alltypes):
    d = alltypes.double_col
    bins = [0, 10, 50, 100]

    expr = d.bucket(bins)
    assert isinstance(expr, ir.CategoryColumn)
    assert expr.op().nbuckets == 3

    expr = d.bucket(bins, include_over=True)
    assert expr.op().nbuckets == 4

    expr = d.bucket(bins, include_over=True, include_under=True)
    assert expr.op().nbuckets == 5


def test_bucket_error_cases(alltypes):
    d = alltypes.double_col

    with pytest.raises(ValueError):
        d.bucket([])

    with pytest.raises(ValueError):
        d.bucket([1, 2], closed="foo")

    # it works!
    d.bucket([10], include_under=True, include_over=True)

    with pytest.raises(ValueError):
        d.bucket([10])

    with pytest.raises(ValueError):
        d.bucket([10], include_under=True)

    with pytest.raises(ValueError):
        d.bucket([10], include_over=True)


def test_histogram(alltypes):
    d = alltypes.double_col

    with pytest.raises(ValueError):
        d.histogram(nbins=10, binwidth=5)

    with pytest.raises(ValueError):
        d.histogram()

    with pytest.raises(ValueError):
        d.histogram(10, closed="foo")


def test_topk_analysis_bug(airlines):
    # GH #398
    dests = ['ORD', 'JFK', 'SFO']
    t = airlines[airlines.dest.isin(dests)]
    delay_filter = t.origin.topk(10, by=t.arrdelay.mean())

    filtered = t.filter([delay_filter])

    assert delay_filter._to_semi_join(t)[t].equals(filtered)


def test_topk_function_late_bind(airlines):
    # GH #520
    expr1 = airlines.dest.topk(5, by=lambda x: x.arrdelay.mean())
    expr2 = airlines.dest.topk(5, by=airlines.arrdelay.mean())

    assert_equal(expr1.to_aggregation(), expr2.to_aggregation())


def test_summary_filter_warns(airlines):
    dests = ['ORD', 'JFK', 'SFO']
    t = airlines[airlines.dest.isin(dests)]
    delay_filter = t.origin.topk(10, by=t.arrdelay.mean())

    with pytest.warns(FutureWarning):
        delay_filter.to_filter()
