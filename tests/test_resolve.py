from __future__ import print_function, absolute_import
import json
import unittest
from os.path import dirname, join

import pytest

from conda.resolve import MatchSpec, Package, Resolve, NoPackagesFound, Unsatisfiable
from tests.helpers import raises
from conda import config

with open(join(dirname(__file__), 'index.json')) as fi:
    index = json.load(fi)
index['mkl@'] = {
    'name': 'mkl@', 'channel': '@', 'priority': 0,
    'version': '0', 'build_number': 0,
    'build': '', 'depends': [], 'track_features': 'mkl'}
r = Resolve(index)

f_mkl = set(['mkl'])

class TestMatchSpec(unittest.TestCase):

    def test_match(self):
        for spec, res in [
            ('numpy 1.7*', True),          ('numpy 1.7.1', True),
            ('numpy 1.7', False),          ('numpy 1.5*', False),
            ('numpy >=1.5', True),         ('numpy >=1.5,<2', True),
            ('numpy >=1.8,<1.9', False),   ('numpy >1.5,<2,!=1.7.1', False),
            ('numpy >1.8,<2|==1.7', False),('numpy >1.8,<2|>=1.7.1', True),
            ('numpy >=1.8|1.7*', True),    ('numpy ==1.7', False),
            ('numpy >=1.5,>1.6', True),    ('numpy ==1.7.1', True),
            ('numpy >=1,*.7.*', True),     ('numpy *.7.*,>=1', True),
            ('numpy >=1,*.8.*', False),    ('numpy >=2,*.7.*', False),
            ('numpy 1.6*|1.7*', True),     ('numpy 1.6*|1.8*', False),
            ('numpy 1.6.2|1.7*', True),    ('numpy 1.6.2|1.7.1', True),
            ('numpy 1.6.2|1.7.0', False),  ('numpy 1.7.1 py27_0', True),
            ('numpy 1.7.1 py26_0', False), ('numpy >1.7.1a', True),
            ('python', False),
            ]:
            m = MatchSpec(spec)
            self.assertEqual(m.match('numpy-1.7.1-py27_0.tar.bz2'), res)

        # both version numbers conforming to PEP 440
        self.assertFalse(MatchSpec('numpy >=1.0.1').match('numpy-1.0.1a-0.tar.bz2'))
        # both version numbers non-conforming to PEP 440
        self.assertFalse(MatchSpec('numpy >=1.0.1.vc11').match('numpy-1.0.1a.vc11-0.tar.bz2'))
        self.assertTrue(MatchSpec('numpy >=1.0.1*.vc11').match('numpy-1.0.1a.vc11-0.tar.bz2'))
        # one conforming, other non-conforming to PEP 440
        self.assertTrue(MatchSpec('numpy <1.0.1').match('numpy-1.0.1.vc11-0.tar.bz2'))
        self.assertTrue(MatchSpec('numpy <1.0.1').match('numpy-1.0.1a.vc11-0.tar.bz2'))
        self.assertFalse(MatchSpec('numpy >=1.0.1.vc11').match('numpy-1.0.1a-0.tar.bz2'))
        self.assertTrue(MatchSpec('numpy >=1.0.1a').match('numpy-1.0.1z-0.tar.bz2'))

    def test_to_filename(self):
        ms = MatchSpec('foo 1.7 52')
        self.assertEqual(ms.to_filename(), 'foo-1.7-52.tar.bz2')

        for spec in 'bitarray', 'pycosat 0.6.0', 'numpy 1.6*':
            ms = MatchSpec(spec)
            self.assertEqual(ms.to_filename(), None)

    def test_hash(self):
        a, b = MatchSpec('numpy 1.7*'), MatchSpec('numpy 1.7*')
        # optional should not change the hash
        d = MatchSpec('numpy 1.7* (optional)')
        self.assertTrue(a is not b)
        self.assertTrue(a is not d)
        self.assertEqual(a, b)
        self.assertNotEqual(a, d)
        self.assertEqual(hash(a), hash(b))
        self.assertEqual(hash(a), hash(d))
        c, d = MatchSpec('python'), MatchSpec('python 2.7.4')
        self.assertNotEqual(a, c)
        self.assertNotEqual(hash(a), hash(c))
        self.assertNotEqual(c, d)
        self.assertNotEqual(hash(c), hash(d))

    def test_string(self):
        a = MatchSpec("foo1 >=1.3 2 (optional,target=burg)")
        assert a.optional and a.target=='burg'

class TestPackage(unittest.TestCase):

    def test_llvm(self):
        ms = MatchSpec('llvm')
        pkgs = [Package(fn, r.index[fn]) for fn in r.find_matches(ms)]
        pkgs.sort()
        self.assertEqual([p.fn for p in pkgs],
                         ['llvm-3.1-0.tar.bz2',
                          'llvm-3.1-1.tar.bz2',
                          'llvm-3.2-0.tar.bz2'])

    def test_different_names(self):
        pkgs = [Package(fn, r.index[fn]) for fn in [
                'llvm-3.1-1.tar.bz2', 'python-2.7.5-0.tar.bz2']]
        self.assertRaises(TypeError, pkgs.sort)


class TestSolve(unittest.TestCase):

    def assert_have_mkl(self, dists, names):
        for fn in dists:
            if fn.rsplit('-', 2)[0] in names:
                self.assertEqual(r.features(fn), f_mkl)

    def test_explicit0(self):
        self.assertEqual(r.explicit([]), [])

    def test_explicit1(self):
        self.assertEqual(r.explicit(['pycosat 0.6.0 py27_0']), None)
        self.assertEqual(r.explicit(['zlib']), None)
        self.assertEqual(r.explicit(['zlib 1.2.7']), None)
        # because zlib has no dependencies it is also explicit
        self.assertEqual(r.explicit(['zlib 1.2.7 0']),
                         ['zlib-1.2.7-0.tar.bz2'])

    def test_explicit2(self):
        self.assertEqual(r.explicit(['pycosat 0.6.0 py27_0',
                                     'zlib 1.2.7 0']),
                         ['pycosat-0.6.0-py27_0.tar.bz2',
                          'zlib-1.2.7-0.tar.bz2'])
        self.assertEqual(r.explicit(['pycosat 0.6.0 py27_0',
                                     'zlib 1.2.7']), None)

    def test_explicitNone(self):
        self.assertEqual(r.explicit(['pycosat 0.6.0 notarealbuildstring']), None)

    def test_empty(self):
        self.assertEqual(r.install([]), [])

    def test_anaconda_14(self):
        specs = ['anaconda 1.4.0 np17py33_0']
        res = r.explicit(specs)
        self.assertEqual(len(res), 51)
        self.assertEqual(r.install(specs), res)
        specs.append('python 3.3*')
        self.assertEqual(r.explicit(specs), None)
        self.assertEqual(r.install(specs), res)

    def test_iopro_nomkl(self):
        self.assertEqual(
            r.install(['iopro 1.4*', 'python 2.7*', 'numpy 1.7*'], returnall=True),
            [['iopro-1.4.3-np17py27_p0.tar.bz2',
              'numpy-1.7.1-py27_0.tar.bz2',
              'openssl-1.0.1c-0.tar.bz2',
              'python-2.7.5-0.tar.bz2',
              'readline-6.2-0.tar.bz2',
              'sqlite-3.7.13-0.tar.bz2',
              'system-5.8-1.tar.bz2',
              'tk-8.5.13-0.tar.bz2',
              'unixodbc-2.3.1-0.tar.bz2',
              'zlib-1.2.7-0.tar.bz2']])

    def test_iopro_mkl(self):
        self.assertEqual(
            r.install(['iopro 1.4*', 'python 2.7*', 'numpy 1.7*', '@mkl'], returnall=True),
            [['iopro-1.4.3-np17py27_p0.tar.bz2',
              'mkl-rt-11.0-p0.tar.bz2',
              'numpy-1.7.1-py27_p0.tar.bz2',
              'openssl-1.0.1c-0.tar.bz2',
              'python-2.7.5-0.tar.bz2',
              'readline-6.2-0.tar.bz2',
              'sqlite-3.7.13-0.tar.bz2',
              'system-5.8-1.tar.bz2',
              'tk-8.5.13-0.tar.bz2',
              'unixodbc-2.3.1-0.tar.bz2',
              'zlib-1.2.7-0.tar.bz2']])

    def test_mkl(self):
        self.assertEqual(r.install(['mkl']),
                         r.install(['mkl 11*', '@mkl']))

    def test_accelerate(self):
        self.assertEqual(
            r.install(['accelerate']),
            r.install(['accelerate', '@mkl']))

    def test_scipy_mkl(self):
        dists = r.install(['scipy', 'python 2.7*', 'numpy 1.7*', '@mkl'])
        self.assert_have_mkl(dists, ('numpy', 'scipy'))
        self.assertTrue('scipy-0.12.0-np17py27_p0.tar.bz2' in dists)

    def test_anaconda_nomkl(self):
        dists = r.install(['anaconda 1.5.0', 'python 2.7*', 'numpy 1.7*'])
        self.assertEqual(len(dists), 107)
        self.assertTrue('scipy-0.12.0-np17py27_0.tar.bz2' in dists)

    def test_anaconda_mkl_2(self):
        # to test "with_features_depends"
        dists = r.install(['anaconda 1.5.0', 'python 2.7*', 'numpy 1.7*', '@mkl'])
        self.assert_have_mkl(dists, ('numpy', 'scipy', 'numexpr', 'scikit-learn'))
        self.assertTrue('scipy-0.12.0-np17py27_p0.tar.bz2' in dists)
        self.assertTrue('mkl-rt-11.0-p0.tar.bz2' in dists)
        self.assertEqual(len(dists), 108)

        dists2 = r.install(['anaconda 1.5.0', 'python 2.7*', 'numpy 1.7*', 'mkl'])
        self.assertTrue(set(dists) <= set(dists2))
        self.assertEqual(len(dists2), 110)

    def test_anaconda_mkl_3(self):
        # to test "with_features_depends"
        dists = r.install(['anaconda 1.5.0', 'python 3*', '@mkl'])
        self.assert_have_mkl(dists, ('numpy', 'scipy'))
        self.assertTrue('scipy-0.12.0-np17py33_p0.tar.bz2' in dists)
        self.assertTrue('mkl-rt-11.0-p0.tar.bz2' in dists)
        self.assertEqual(len(dists), 61)


class TestFindSubstitute(unittest.TestCase):

    def test1(self):
        installed = r.install(['anaconda 1.5.0', 'python 2.7*', 'numpy 1.7*', '@mkl'])
        for old, new in [('numpy-1.7.1-py27_p0.tar.bz2',
                          'numpy-1.7.1-py27_0.tar.bz2'),
                         ('scipy-0.12.0-np17py27_p0.tar.bz2',
                          'scipy-0.12.0-np17py27_0.tar.bz2'),
                         ('mkl-rt-11.0-p0.tar.bz2', None)]:
            self.assertTrue(old in installed)
            self.assertEqual(r.find_substitute(installed, f_mkl, old), new)


def test_pseudo_boolean():
    # The latest version of iopro, 1.5.0, was not built against numpy 1.5
    assert r.install(['iopro', 'python 2.7*', 'numpy 1.5*'], returnall=True) == [[
        'iopro-1.4.3-np15py27_p0.tar.bz2',
        'numpy-1.5.1-py27_4.tar.bz2',
        'openssl-1.0.1c-0.tar.bz2',
        'python-2.7.5-0.tar.bz2',
        'readline-6.2-0.tar.bz2',
        'sqlite-3.7.13-0.tar.bz2',
        'system-5.8-1.tar.bz2',
        'tk-8.5.13-0.tar.bz2',
        'unixodbc-2.3.1-0.tar.bz2',
        'zlib-1.2.7-0.tar.bz2',
    ]]

    assert r.install(['iopro', 'python 2.7*', 'numpy 1.5*', '@mkl'], returnall=True) == [[
        'iopro-1.4.3-np15py27_p0.tar.bz2',
        'mkl-rt-11.0-p0.tar.bz2',
        'numpy-1.5.1-py27_p4.tar.bz2',
        'openssl-1.0.1c-0.tar.bz2',
        'python-2.7.5-0.tar.bz2',
        'readline-6.2-0.tar.bz2',
        'sqlite-3.7.13-0.tar.bz2',
        'system-5.8-1.tar.bz2',
        'tk-8.5.13-0.tar.bz2',
        'unixodbc-2.3.1-0.tar.bz2',
        'zlib-1.2.7-0.tar.bz2',
    ]]

def test_get_dists():
    dists = r.get_dists(["anaconda 1.5.0"])[0]
    assert 'anaconda-1.5.0-np17py27_0.tar.bz2' in dists
    assert 'dynd-python-0.3.0-np17py33_0.tar.bz2' in dists

def test_generate_eq():
    specs = ['anaconda']
    dists, specs = r.get_dists(specs)
    r2 = Resolve(dists, True, True)
    C = r2.gen_clauses()
    eqv, eqb = r2.generate_version_metrics(C, specs)
    # Should satisfy the following criteria:
    # - lower versions of the same package should should have higher
    #   coefficients.
    # - the same versions of the same package (e.g., different build strings)
    #   should have the same coefficients.
    # - a package that only has one version should not appear, unless
    #   include=True as it will have a 0 coefficient. The same is true of the
    #   latest version of a package.
    assert eqv == {
        'astropy-0.2-np15py26_0.tar.bz2': 1,
        'astropy-0.2-np16py26_0.tar.bz2': 1,
        'astropy-0.2-np17py26_0.tar.bz2': 1,
        'astropy-0.2-np17py33_0.tar.bz2': 1,
        'bitarray-0.8.0-py26_0.tar.bz2': 1,
        'bitarray-0.8.0-py33_0.tar.bz2': 1,
        'cython-0.18-py26_0.tar.bz2': 1,
        'cython-0.18-py33_0.tar.bz2': 1,
        'distribute-0.6.34-py26_1.tar.bz2': 1,
        'distribute-0.6.34-py33_1.tar.bz2': 1,
        'ipython-0.13.1-py26_1.tar.bz2': 1,
        'ipython-0.13.1-py33_1.tar.bz2': 1,
        'llvmpy-0.11.1-py26_0.tar.bz2': 1,
        'llvmpy-0.11.1-py33_0.tar.bz2': 1,
        'lxml-3.0.2-py26_0.tar.bz2': 1,
        'lxml-3.0.2-py33_0.tar.bz2': 1,
        'matplotlib-1.2.0-np15py26_1.tar.bz2': 1,
        'matplotlib-1.2.0-np16py26_1.tar.bz2': 1,
        'matplotlib-1.2.0-np17py26_1.tar.bz2': 1,
        'matplotlib-1.2.0-np17py33_1.tar.bz2': 1,
        'nose-1.2.1-py26_0.tar.bz2': 1,
        'nose-1.2.1-py33_0.tar.bz2': 1,
        'numpy-1.5.1-py26_3.tar.bz2': 3,
        'numpy-1.6.2-py26_3.tar.bz2': 2,
        'numpy-1.6.2-py26_4.tar.bz2': 2,
        'numpy-1.6.2-py27_4.tar.bz2': 2,
        'numpy-1.7.0-py26_0.tar.bz2': 1,
        'numpy-1.7.0-py33_0.tar.bz2': 1,
        'pip-1.2.1-py26_1.tar.bz2': 1,
        'pip-1.2.1-py33_1.tar.bz2': 1,
        'psutil-0.6.1-py26_0.tar.bz2': 1,
        'psutil-0.6.1-py33_0.tar.bz2': 1,
        'pyflakes-0.6.1-py26_0.tar.bz2': 1,
        'pyflakes-0.6.1-py33_0.tar.bz2': 1,
        'python-2.6.8-6.tar.bz2': 3,
        'python-2.7.4-0.tar.bz2': 2,
        'python-3.3.0-4.tar.bz2': 1,
        'pytz-2012j-py26_0.tar.bz2': 1,
        'pytz-2012j-py33_0.tar.bz2': 1,
        'requests-0.13.9-py26_0.tar.bz2': 1,
        'requests-0.13.9-py33_0.tar.bz2': 1,
        'scipy-0.11.0-np15py26_3.tar.bz2': 1,
        'scipy-0.11.0-np16py26_3.tar.bz2': 1,
        'scipy-0.11.0-np17py26_3.tar.bz2': 1,
        'scipy-0.11.0-np17py33_3.tar.bz2': 1,
        'six-1.2.0-py26_0.tar.bz2': 1,
        'six-1.2.0-py33_0.tar.bz2': 1,
        'sqlalchemy-0.7.8-py26_0.tar.bz2': 1,
        'sqlalchemy-0.7.8-py33_0.tar.bz2': 1,
        'tornado-2.4.1-py26_0.tar.bz2': 1,
        'tornado-2.4.1-py33_0.tar.bz2': 1,
        'xlrd-0.9.0-py26_0.tar.bz2': 1,
        'xlrd-0.9.0-py33_0.tar.bz2': 1}
    assert eqb == {
        'dateutil-2.1-py26_0.tar.bz2': 1,
        'dateutil-2.1-py33_0.tar.bz2': 1,
        'numpy-1.6.2-py26_3.tar.bz2': 1,
        'pyzmq-2.2.0.1-py26_0.tar.bz2': 1,
        'pyzmq-2.2.0.1-py33_0.tar.bz2': 1,
        'sphinx-1.1.3-py26_2.tar.bz2': 1,
        'sphinx-1.1.3-py33_2.tar.bz2': 1,
        'system-5.8-0.tar.bz2': 1,
        'zeromq-2.2.0-0.tar.bz2': 1}

def test_unsat():
    # scipy 0.12.0b1 is not built for numpy 1.5, only 1.6 and 1.7
    assert raises(Unsatisfiable, lambda: r.install(['numpy 1.5*', 'scipy 0.12.0b1']))
    # numpy 1.5 does not have a python 3 package
    assert raises(Unsatisfiable, lambda: r.install(['numpy 1.5*', 'python 3*']))
    assert raises(Unsatisfiable, lambda: r.install(['numpy 1.5*', 'numpy 1.6*']))

def test_nonexistent():
    assert raises(NoPackagesFound, lambda: r.get_pkgs('notarealpackage 2.0*'))
    assert raises(NoPackagesFound, lambda: r.install(['notarealpackage 2.0*']))
    # This exact version of NumPy does not exist
    assert raises(NoPackagesFound, lambda: r.install(['numpy 1.5']))

def test_nonexistent_deps():
    index2 = index.copy()
    index2['mypackage-1.0-py33_0.tar.bz2'] = {
        'build': 'py33_0',
        'build_number': 0,
        'depends': ['nose', 'python 3.3*', 'notarealpackage 2.0*'],
        'name': 'mypackage',
        'requires': ['nose 1.2.1', 'python 3.3'],
        'version': '1.0',
    }
    index2['mypackage-1.1-py33_0.tar.bz2'] = {
        'build': 'py33_0',
        'build_number': 0,
        'depends': ['nose', 'python 3.3*'],
        'name': 'mypackage',
        'requires': ['nose 1.2.1', 'python 3.3'],
        'version': '1.1',
    }
    index2['anotherpackage-1.0-py33_0.tar.bz2'] = {
        'build': 'py33_0',
        'build_number': 0,
        'depends': ['nose', 'mypackage 1.1'],
        'name': 'anotherpackage',
        'requires': ['nose', 'mypackage 1.1'],
        'version': '1.0',
    }
    index2['anotherpackage-2.0-py33_0.tar.bz2'] = {
        'build': 'py33_0',
        'build_number': 0,
        'depends': ['nose', 'mypackage'],
        'name': 'anotherpackage',
        'requires': ['nose', 'mypackage'],
        'version': '2.0',
    }
    r = Resolve(index2)

    assert set(r.find_matches(MatchSpec('mypackage'))) == {
        'mypackage-1.0-py33_0.tar.bz2',
        'mypackage-1.1-py33_0.tar.bz2',
    }
    assert set(r.get_dists(['mypackage'])[0].keys()) == {
        'mypackage-1.1-py33_0.tar.bz2',
        'nose-1.1.2-py33_0.tar.bz2',
        'nose-1.2.1-py33_0.tar.bz2',
        'nose-1.3.0-py33_0.tar.bz2',
        'openssl-1.0.1c-0.tar.bz2',
        'python-3.3.0-2.tar.bz2',
        'python-3.3.0-3.tar.bz2',
        'python-3.3.0-4.tar.bz2',
        'python-3.3.0-pro0.tar.bz2',
        'python-3.3.0-pro1.tar.bz2',
        'python-3.3.1-0.tar.bz2',
        'python-3.3.2-0.tar.bz2',
        'readline-6.2-0.tar.bz2',
        'sqlite-3.7.13-0.tar.bz2',
        'system-5.8-0.tar.bz2',
        'system-5.8-1.tar.bz2',
        'tk-8.5.13-0.tar.bz2',
        'zlib-1.2.7-0.tar.bz2'}

    assert r.install(['mypackage']) == r.install(['mypackage 1.1']) == [
        'mypackage-1.1-py33_0.tar.bz2',
        'nose-1.3.0-py33_0.tar.bz2',
        'openssl-1.0.1c-0.tar.bz2',
        'python-3.3.2-0.tar.bz2',
        'readline-6.2-0.tar.bz2',
        'sqlite-3.7.13-0.tar.bz2',
        'system-5.8-1.tar.bz2',
        'tk-8.5.13-0.tar.bz2',
        'zlib-1.2.7-0.tar.bz2',
    ]
    assert raises(NoPackagesFound, lambda: r.install(['mypackage 1.0']))
    assert raises(NoPackagesFound, lambda: r.install(['mypackage 1.0', 'burgertime 1.0']))

    assert r.install(['anotherpackage 1.0']) == [
        'anotherpackage-1.0-py33_0.tar.bz2',
        'mypackage-1.1-py33_0.tar.bz2',
        'nose-1.3.0-py33_0.tar.bz2',
        'openssl-1.0.1c-0.tar.bz2',
        'python-3.3.2-0.tar.bz2',
        'readline-6.2-0.tar.bz2',
        'sqlite-3.7.13-0.tar.bz2',
        'system-5.8-1.tar.bz2',
        'tk-8.5.13-0.tar.bz2',
        'zlib-1.2.7-0.tar.bz2',
    ]

    assert r.install(['anotherpackage']) == [
        'anotherpackage-2.0-py33_0.tar.bz2',
        'mypackage-1.1-py33_0.tar.bz2',
        'nose-1.3.0-py33_0.tar.bz2',
        'openssl-1.0.1c-0.tar.bz2',
        'python-3.3.2-0.tar.bz2',
        'readline-6.2-0.tar.bz2',
        'sqlite-3.7.13-0.tar.bz2',
        'system-5.8-1.tar.bz2',
        'tk-8.5.13-0.tar.bz2',
        'zlib-1.2.7-0.tar.bz2',
    ]

    # This time, the latest version is messed up
    index3 = index.copy()
    index3['mypackage-1.1-py33_0.tar.bz2'] = {
        'build': 'py33_0',
        'build_number': 0,
        'depends': ['nose', 'python 3.3*', 'notarealpackage 2.0*'],
        'name': 'mypackage',
        'requires': ['nose 1.2.1', 'python 3.3'],
        'version': '1.1',
    }
    index3['mypackage-1.0-py33_0.tar.bz2'] = {
        'build': 'py33_0',
        'build_number': 0,
        'depends': ['nose', 'python 3.3*'],
        'name': 'mypackage',
        'requires': ['nose 1.2.1', 'python 3.3'],
        'version': '1.0',
    }
    index3['anotherpackage-1.0-py33_0.tar.bz2'] = {
        'build': 'py33_0',
        'build_number': 0,
        'depends': ['nose', 'mypackage 1.0'],
        'name': 'anotherpackage',
        'requires': ['nose', 'mypackage 1.0'],
        'version': '1.0',
    }
    index3['anotherpackage-2.0-py33_0.tar.bz2'] = {
        'build': 'py33_0',
        'build_number': 0,
        'depends': ['nose', 'mypackage'],
        'name': 'anotherpackage',
        'requires': ['nose', 'mypackage'],
        'version': '2.0',
    }
    r = Resolve(index3)

    assert set(r.find_matches(MatchSpec('mypackage'))) == {
        'mypackage-1.0-py33_0.tar.bz2',
        'mypackage-1.1-py33_0.tar.bz2',
        }
    assert set(r.get_dists(['mypackage'])[0].keys()) == {
        'mypackage-1.0-py33_0.tar.bz2',
        'nose-1.1.2-py33_0.tar.bz2',
        'nose-1.2.1-py33_0.tar.bz2',
        'nose-1.3.0-py33_0.tar.bz2',
        'openssl-1.0.1c-0.tar.bz2',
        'python-3.3.0-2.tar.bz2',
        'python-3.3.0-3.tar.bz2',
        'python-3.3.0-4.tar.bz2',
        'python-3.3.0-pro0.tar.bz2',
        'python-3.3.0-pro1.tar.bz2',
        'python-3.3.1-0.tar.bz2',
        'python-3.3.2-0.tar.bz2',
        'readline-6.2-0.tar.bz2',
        'sqlite-3.7.13-0.tar.bz2',
        'system-5.8-0.tar.bz2',
        'system-5.8-1.tar.bz2',
        'tk-8.5.13-0.tar.bz2',
        'zlib-1.2.7-0.tar.bz2'}

    assert r.install(['mypackage']) == r.install(['mypackage 1.0']) == [
        'mypackage-1.0-py33_0.tar.bz2',
        'nose-1.3.0-py33_0.tar.bz2',
        'openssl-1.0.1c-0.tar.bz2',
        'python-3.3.2-0.tar.bz2',
        'readline-6.2-0.tar.bz2',
        'sqlite-3.7.13-0.tar.bz2',
        'system-5.8-1.tar.bz2',
        'tk-8.5.13-0.tar.bz2',
        'zlib-1.2.7-0.tar.bz2',
    ]
    assert raises(NoPackagesFound, lambda: r.install(['mypackage 1.1']))


    assert r.install(['anotherpackage 1.0']) == [
        'anotherpackage-1.0-py33_0.tar.bz2',
        'mypackage-1.0-py33_0.tar.bz2',
        'nose-1.3.0-py33_0.tar.bz2',
        'openssl-1.0.1c-0.tar.bz2',
        'python-3.3.2-0.tar.bz2',
        'readline-6.2-0.tar.bz2',
        'sqlite-3.7.13-0.tar.bz2',
        'system-5.8-1.tar.bz2',
        'tk-8.5.13-0.tar.bz2',
        'zlib-1.2.7-0.tar.bz2',
    ]

    # If recursive checking is working correctly, this will give
    # anotherpackage 2.0, not anotherpackage 1.0
    assert r.install(['anotherpackage']) == [
        'anotherpackage-2.0-py33_0.tar.bz2',
        'mypackage-1.0-py33_0.tar.bz2',
        'nose-1.3.0-py33_0.tar.bz2',
        'openssl-1.0.1c-0.tar.bz2',
        'python-3.3.2-0.tar.bz2',
        'readline-6.2-0.tar.bz2',
        'sqlite-3.7.13-0.tar.bz2',
        'system-5.8-1.tar.bz2',
        'tk-8.5.13-0.tar.bz2',
        'zlib-1.2.7-0.tar.bz2',
    ]


def test_install_package_with_feature():
    index2 = index.copy()
    index2['mypackage-1.0-featurepy33_0.tar.bz2'] = {
        'build': 'featurepy33_0',
        'build_number': 0,
        'depends': ['python 3.3*'],
        'name': 'mypackage',
        'version': '1.0',
        'features': 'feature',
    }
    index2['feature-1.0-py33_0.tar.bz2'] = {
        'build': 'py33_0',
        'build_number': 0,
        'depends': ['python 3.3*'],
        'name': 'feature',
        'version': '1.0',
        'track_features': 'feature',
    }

    r = Resolve(index2)

    # It should not raise
    r.install(['mypackage','feature 1.0'])


def test_circular_dependencies():
    index2 = index.copy()
    index2['package1-1.0-0.tar.bz2'] = {
        'build': '0',
        'build_number': 0,
        'depends': ['package2'],
        'name': 'package1',
        'requires': ['package2'],
        'version': '1.0',
    }
    index2['package2-1.0-0.tar.bz2'] = {
        'build': '0',
        'build_number': 0,
        'depends': ['package1'],
        'name': 'package2',
        'requires': ['package1'],
        'version': '1.0',
    }
    r = Resolve(index2)

    assert set(r.find_matches(MatchSpec('package1'))) == {
        'package1-1.0-0.tar.bz2',
    }
    assert set(r.get_dists(['package1'])[0].keys()) == {
        'package1-1.0-0.tar.bz2',
        'package2-1.0-0.tar.bz2',
    }
    assert r.install(['package1']) == r.install(['package2']) == \
        r.install(['package1', 'package2']) == [
        'package1-1.0-0.tar.bz2',
        'package2-1.0-0.tar.bz2',
    ]


def test_package_ordering():
    sympy_071 = Package('sympy-0.7.1-py27_0.tar.bz2', r.index['sympy-0.7.1-py27_0.tar.bz2'])
    sympy_072 = Package('sympy-0.7.2-py27_0.tar.bz2', r.index['sympy-0.7.2-py27_0.tar.bz2'])
    python_275 = Package('python-2.7.5-0.tar.bz2', r.index['python-2.7.5-0.tar.bz2'])
    numpy = Package('numpy-1.7.1-py27_0.tar.bz2', r.index['numpy-1.7.1-py27_0.tar.bz2'])
    numpy_mkl = Package('numpy-1.7.1-py27_p0.tar.bz2', r.index['numpy-1.7.1-py27_p0.tar.bz2'])

    assert sympy_071 < sympy_072
    assert not sympy_071 < sympy_071
    assert not sympy_072 < sympy_071
    raises(TypeError, lambda: sympy_071 < python_275)

    assert sympy_071 <= sympy_072
    assert sympy_071 <= sympy_071
    assert not sympy_072 <= sympy_071
    assert raises(TypeError, lambda: sympy_071 <= python_275)

    assert sympy_071 == sympy_071
    assert not sympy_071 == sympy_072
    assert (sympy_071 == python_275) is False
    assert (sympy_071 == 1) is False

    assert not sympy_071 != sympy_071
    assert sympy_071 != sympy_072
    assert (sympy_071 != python_275) is True

    assert not sympy_071 > sympy_072
    assert not sympy_071 > sympy_071
    assert sympy_072 > sympy_071
    raises(TypeError, lambda: sympy_071 > python_275)

    assert not sympy_071 >= sympy_072
    assert sympy_071 >= sympy_071
    assert sympy_072 >= sympy_071
    assert raises(TypeError, lambda: sympy_071 >= python_275)

    # The first four are a bit arbitrary. For now, we just test that it
    # doesn't prefer the mkl version.
    assert not numpy > numpy_mkl
    assert not numpy >= numpy_mkl
    assert numpy < numpy_mkl
    assert numpy <= numpy_mkl
    assert (numpy != numpy_mkl) is True
    assert (numpy == numpy_mkl) is False

def test_irrational_version():
    assert r.install(['pytz 2012d', 'python 3*'], returnall=True) == [[
        'openssl-1.0.1c-0.tar.bz2',
        'python-3.3.2-0.tar.bz2',
        'pytz-2012d-py33_0.tar.bz2',
        'readline-6.2-0.tar.bz2',
        'sqlite-3.7.13-0.tar.bz2',
        'system-5.8-1.tar.bz2',
        'tk-8.5.13-0.tar.bz2',
        'zlib-1.2.7-0.tar.bz2'
    ]]

def test_no_features():
    # Without this, there would be another solution including 'scipy-0.11.0-np16py26_p3.tar.bz2'.
    assert r.install(['python 2.6*', 'numpy 1.6*', 'scipy 0.11*'],
        returnall=True) == [[
            'numpy-1.6.2-py26_4.tar.bz2',
            'openssl-1.0.1c-0.tar.bz2',
            'python-2.6.8-6.tar.bz2',
            'readline-6.2-0.tar.bz2',
            'scipy-0.11.0-np16py26_3.tar.bz2',
            'sqlite-3.7.13-0.tar.bz2',
            'system-5.8-1.tar.bz2',
            'tk-8.5.13-0.tar.bz2',
            'zlib-1.2.7-0.tar.bz2',
            ]]

    assert r.install(['python 2.6*', 'numpy 1.6*', 'scipy 0.11*', '@mkl'],
        returnall=True) == [[
            'mkl-rt-11.0-p0.tar.bz2',           # This,
            'numpy-1.6.2-py26_p4.tar.bz2',      # this,
            'openssl-1.0.1c-0.tar.bz2',
            'python-2.6.8-6.tar.bz2',
            'readline-6.2-0.tar.bz2',
            'scipy-0.11.0-np16py26_p3.tar.bz2', # and this are different.
            'sqlite-3.7.13-0.tar.bz2',
            'system-5.8-1.tar.bz2',
            'tk-8.5.13-0.tar.bz2',
            'zlib-1.2.7-0.tar.bz2',
            ]]

    index2 = index.copy()
    index2["pandas-0.12.0-np16py27_0.tar.bz2"] = \
        {
            "build": "np16py27_0",
            "build_number": 0,
            "depends": [
              "dateutil",
              "numpy 1.6*",
              "python 2.7*",
              "pytz"
            ],
            "name": "pandas",
            "requires": [
              "dateutil 1.5",
              "numpy 1.6",
              "python 2.7",
              "pytz"
            ],
            "version": "0.12.0"
        }
    # Make it want to choose the pro version by having it be newer.
    index2["numpy-1.6.2-py27_p5.tar.bz2"] = \
        {
            "build": "py27_p5",
            "build_number": 5,
            "depends": [
              "mkl-rt 11.0",
              "python 2.7*"
            ],
            "features": "mkl",
            "name": "numpy",
            "pub_date": "2013-04-29",
            "requires": [
              "mkl-rt 11.0",
              "python 2.7"
            ],
            "version": "1.6.2"
        }

    r2 = Resolve(index2)

    # This should not pick any mkl packages (the difference here is that none
    # of the specs directly have mkl versions)
    assert r2.solve(['pandas 0.12.0 np16py27_0', 'python 2.7*'],
        returnall=True) == [[
            'dateutil-2.1-py27_1.tar.bz2',
            'numpy-1.6.2-py27_4.tar.bz2',
            'openssl-1.0.1c-0.tar.bz2',
            'pandas-0.12.0-np16py27_0.tar.bz2',
            'python-2.7.5-0.tar.bz2',
            'pytz-2013b-py27_0.tar.bz2',
            'readline-6.2-0.tar.bz2',
            'six-1.3.0-py27_0.tar.bz2',
            'sqlite-3.7.13-0.tar.bz2',
            'system-5.8-1.tar.bz2',
            'tk-8.5.13-0.tar.bz2',
            'zlib-1.2.7-0.tar.bz2',
            ]]

    assert r2.solve(['pandas 0.12.0 np16py27_0', 'python 2.7*', '@mkl'],
        returnall=True)[0] == [[
            'dateutil-2.1-py27_1.tar.bz2',
            'mkl-rt-11.0-p0.tar.bz2',           # This
            'numpy-1.6.2-py27_p5.tar.bz2',      # and this are different.
            'openssl-1.0.1c-0.tar.bz2',
            'pandas-0.12.0-np16py27_0.tar.bz2',
            'python-2.7.5-0.tar.bz2',
            'pytz-2013b-py27_0.tar.bz2',
            'readline-6.2-0.tar.bz2',
            'six-1.3.0-py27_0.tar.bz2',
            'sqlite-3.7.13-0.tar.bz2',
            'system-5.8-1.tar.bz2',
            'tk-8.5.13-0.tar.bz2',
            'zlib-1.2.7-0.tar.bz2',
            ]][0]

def test_multiple_solution():
    index2 = index.copy()
    fn = 'pandas-0.11.0-np16py27_1.tar.bz2'
    res1 = set([fn])
    for k in range(1,15):
        fn2 = '%s_%d.tar.bz2'%(fn[:-8],k)
        index2[fn2] = index[fn]
        res1.add(fn2)
    r = Resolve(index2)
    res = r.solve(['pandas', 'python 2.7*', 'numpy 1.6*'], returnall=True)
    res = set([y for x in res for y in x if y.startswith('pandas')])
    assert res <= res1

def test_broken_install():
    installed = r.install(['pandas', 'python 2.7*', 'numpy 1.6*'])
    assert installed == [
        'dateutil-2.1-py27_1.tar.bz2',
        'numpy-1.6.2-py27_4.tar.bz2',
        'openssl-1.0.1c-0.tar.bz2',
        'pandas-0.11.0-np16py27_1.tar.bz2',
        'python-2.7.5-0.tar.bz2',
        'pytz-2013b-py27_0.tar.bz2',
        'readline-6.2-0.tar.bz2',
        'scipy-0.12.0-np16py27_0.tar.bz2',
        'six-1.3.0-py27_0.tar.bz2',
        'sqlite-3.7.13-0.tar.bz2',
        'system-5.8-1.tar.bz2',
        'tk-8.5.13-0.tar.bz2',
        'zlib-1.2.7-0.tar.bz2']
    installed[1] = 'numpy-1.7.1-py33_p0.tar.bz2'
    installed.append('notarealpackage-2.0-0.tar.bz2')
    assert r.install([], installed) == installed
    installed2 = r.install(['numpy'], installed)
    installed3 = r.remove(['pandas'], installed)
    assert set(installed3) == set(installed[:3] + installed[4:])

def test_remove():
    installed = r.install(['pandas', 'python 2.7*'])
    assert installed == [
        'dateutil-2.1-py27_1.tar.bz2',
        'numpy-1.7.1-py27_0.tar.bz2',
        'openssl-1.0.1c-0.tar.bz2',
        'pandas-0.11.0-np17py27_1.tar.bz2',
        'python-2.7.5-0.tar.bz2',
        'pytz-2013b-py27_0.tar.bz2',
        'readline-6.2-0.tar.bz2',
        'scipy-0.12.0-np17py27_0.tar.bz2',
        'six-1.3.0-py27_0.tar.bz2',
        'sqlite-3.7.13-0.tar.bz2',
        'system-5.8-1.tar.bz2',
        'tk-8.5.13-0.tar.bz2',
        'zlib-1.2.7-0.tar.bz2']

    assert r.remove(['pandas'], installed=installed) == [
        'dateutil-2.1-py27_1.tar.bz2',
        'numpy-1.7.1-py27_0.tar.bz2',
        'openssl-1.0.1c-0.tar.bz2',
        'python-2.7.5-0.tar.bz2',
        'pytz-2013b-py27_0.tar.bz2',
        'readline-6.2-0.tar.bz2',
        'scipy-0.12.0-np17py27_0.tar.bz2',
        'six-1.3.0-py27_0.tar.bz2',
        'sqlite-3.7.13-0.tar.bz2',
        'system-5.8-1.tar.bz2',
        'tk-8.5.13-0.tar.bz2',
        'zlib-1.2.7-0.tar.bz2']

    # Pandas requires numpy
    assert r.remove(['numpy'], installed=installed) == [
        'dateutil-2.1-py27_1.tar.bz2',
        'openssl-1.0.1c-0.tar.bz2',
        'python-2.7.5-0.tar.bz2',
        'pytz-2013b-py27_0.tar.bz2',
        'readline-6.2-0.tar.bz2',
        'six-1.3.0-py27_0.tar.bz2',
        'sqlite-3.7.13-0.tar.bz2',
        'system-5.8-1.tar.bz2',
        'tk-8.5.13-0.tar.bz2',
        'zlib-1.2.7-0.tar.bz2']

def test_channel_priority():
    fn1 = 'pandas-0.10.1-np17py27_0.tar.bz2'
    fn2 = 'other::' + fn1
    spec = ['pandas', 'python 2.7*']
    index2 = index.copy()
    index2[fn2] = index2[fn1].copy()
    r2 = Resolve(index2)
    rec = r2.index[fn2]
    config.channel_priority = True
    rec['priority'] = 0
    # Should select the "other", older package because it
    # has a lower channel priority number
    installed1 = r2.install(spec)
    # Should select the newer package because now the "other"
    # package has a higher priority number
    rec['priority'] = 2
    installed2 = r2.install(spec)
    # Should also select the newer package because we have
    # turned off channel priority altogether
    config.channel_priority = False
    rec['priority'] = 0
    installed3 = r2.install(spec)
    assert installed1 != installed2
    assert installed1 != installed3
    assert installed2 == installed3

def test_dependency_sort():
    specs = ['pandas','python 2.7*','numpy 1.6*']
    installed = r.install(specs)
    must_have = {r.package_name(pkg):pkg[:-8] for pkg in installed}
    installed = r.dependency_sort(must_have)
    assert installed == [
        'openssl-1.0.1c-0',
        'readline-6.2-0',
        'sqlite-3.7.13-0',
        'system-5.8-1',
        'tk-8.5.13-0',
        'zlib-1.2.7-0',
        'python-2.7.5-0',
        'numpy-1.6.2-py27_4',
        'pytz-2013b-py27_0',
        'six-1.3.0-py27_0',
        'dateutil-2.1-py27_1',
        'scipy-0.12.0-np16py27_0',
        'pandas-0.11.0-np16py27_1']

def test_update_deps():
    installed = r.install(['python 2.7*', 'numpy 1.6*', 'pandas 0.10.1'])
    assert installed == [
        'dateutil-2.1-py27_1.tar.bz2',
        'numpy-1.6.2-py27_4.tar.bz2',
        'openssl-1.0.1c-0.tar.bz2',
        'pandas-0.10.1-np16py27_0.tar.bz2',
        'python-2.7.5-0.tar.bz2',
        'readline-6.2-0.tar.bz2',
        'scipy-0.11.0-np16py27_3.tar.bz2',
        'six-1.3.0-py27_0.tar.bz2',
        'sqlite-3.7.13-0.tar.bz2',
        'system-5.8-1.tar.bz2',
        'tk-8.5.13-0.tar.bz2',
        'zlib-1.2.7-0.tar.bz2',
    ]

    # scipy, and pandas should all be updated here. pytz is a new
    # dependency of pandas. But numpy does not _need_ to be updated
    # to get the latest version of pandas, so it stays put.
    assert r.install(['pandas', 'python 2.7*'], installed=installed,
        update_deps=True, returnall=True) == [[
        'dateutil-2.1-py27_1.tar.bz2',
        'numpy-1.6.2-py27_4.tar.bz2',
        'openssl-1.0.1c-0.tar.bz2',
        'pandas-0.11.0-np16py27_1.tar.bz2',
        'python-2.7.5-0.tar.bz2',
        'pytz-2013b-py27_0.tar.bz2',
        'readline-6.2-0.tar.bz2',
        'scipy-0.12.0-np16py27_0.tar.bz2',
        'six-1.3.0-py27_0.tar.bz2',
        'sqlite-3.7.13-0.tar.bz2',
        'system-5.8-1.tar.bz2',
        'tk-8.5.13-0.tar.bz2',
        'zlib-1.2.7-0.tar.bz2']]

    # pandas should be updated here. However, it's going to try to not update
    # scipy, so it won't be updated to the latest version (0.11.0).
    assert r.install(['pandas', 'python 2.7*'], installed=installed,
        update_deps=False, returnall=True) == [[
        'dateutil-2.1-py27_1.tar.bz2',
        'numpy-1.6.2-py27_4.tar.bz2',
        'openssl-1.0.1c-0.tar.bz2',
        'pandas-0.10.1-np16py27_0.tar.bz2',
        'python-2.7.5-0.tar.bz2',
        'readline-6.2-0.tar.bz2',
        'scipy-0.11.0-np16py27_3.tar.bz2',
        'six-1.3.0-py27_0.tar.bz2',
        'sqlite-3.7.13-0.tar.bz2',
        'system-5.8-1.tar.bz2',
        'tk-8.5.13-0.tar.bz2',
        'zlib-1.2.7-0.tar.bz2',
    ]]
