from __future__ import print_function, division, absolute_import

import logging
from collections import defaultdict
from itertools import chain

from conda.compat import iterkeys, itervalues, iteritems, string_types
from conda.logic import minimal_unsatisfiable_subset, Clauses
from conda.version import VersionSpec, normalized_version
from conda.console import setup_handlers
from conda import config
from conda.toposort import toposort

log = logging.getLogger(__name__)
dotlog = logging.getLogger('dotupdate')
stdoutlog = logging.getLogger('stdoutlog')
stderrlog = logging.getLogger('stderrlog')
setup_handlers()


def dashlist(iter):
    return ''.join('\n  - ' + str(x) for x in iter)


class Unsatisfiable(RuntimeError):
    '''An exception to report unsatisfiable dependencies.

    Args:
        bad_deps: a list of tuples of objects (likely MatchSpecs).
        chains: (optional) if True, the tuples are interpreted as chains
            of dependencies, from top level to bottom. If False, the tuples
            are interpreted as simple lists of conflicting specs.

    Returns:
        Raises an exception with a formatted message detailing the
        unsatisfiable specifications.
    '''
    def __init__(self, bad_deps):
        deps = set(q[-1] for q in bad_deps)
        msg = '''The following specifications were found to be in conflict:%s
Use "conda info <package>" to see the dependencies for each package.'''
        msg = msg % dashlist(' -> '.join(map(str, q)) for q in bad_deps)
        super(Unsatisfiable, self).__init__(msg)


class NoPackagesFound(RuntimeError):
    '''An exception to report that requested packages are missing.

    Args:
        bad_deps: a list of tuples of MatchSpecs, assumed to be dependency
        chains, from top level to bottom.

    Returns:
        Raises an exception with a formatted message detailing the
        missing packages and/or dependencies.
    '''
    def __init__(self, bad_deps):
        deps = set(q[-1] for q in bad_deps)
        if all(len(q) > 1 for q in bad_deps):
            what = "Dependencies" if len(bad_deps) > 1 else "Dependency"
        elif all(len(q) == 1 for q in bad_deps):
            what = "Packages" if len(bad_deps) > 1 else "Package"
        else:
            what = "Packages/dependencies"
        bad_deps = dashlist(' -> '.join(map(str, q)) for q in bad_deps)
        msg = '%s missing in current %s channels: %s' % (what, config.subdir, bad_deps)
        super(NoPackagesFound, self).__init__(msg)
        self.pkgs = deps


class MatchSpec(object):
    def __new__(cls, spec, target=None, optional=None):
        if isinstance(spec, cls):
            return spec
        self = object.__new__(cls)
        spec, _, oparts = spec.partition('(')
        self.spec = spec.strip()
        if oparts and oparts.strip()[-1] != ')':
            raise ValueError("Invalid MatchSpec: %s" % spec)
        parts = spec.split()
        self.strictness = len(parts)
        assert 1 <= self.strictness <= 3, repr(spec)
        self.name = parts[0]
        if self.strictness == 2:
            self.vspecs = VersionSpec(parts[1])
        elif self.strictness == 3:
            self.ver_build = tuple(parts[1:3])
        self.target = target
        self.optional = optional
        if oparts:
            for opart in oparts.strip()[:-1].split(','):
                if opart == 'optional':
                    self.optional = True
                elif opart.startswith('target='):
                    self.target = opart.split('=')[1].strip()
                else:
                    raise ValueError("Invalid MatchSpec: %s" % spec)
        if self.optional is None:
            self.optional = False
        return self

    def match_fast(self, version, build):
        if self.strictness == 1:
            return True
        elif self.strictness == 2:
            return self.vspecs.match(version)
        else:
            return bool((version, build) == self.ver_build)

    def match(self, info):
        if isinstance(info, string_types):
            name, version, build = info[:-8].rsplit('-', 2)
        else:
            name = info.get('name')
            version = info.get('version')
            build = info.get('build')
        if name != self.name:
            return False
        return self.match_fast(version, build)

    def to_filename(self):
        if self.strictness == 3 and not self.optional:
            return self.name + '-%s-%s.tar.bz2' % self.ver_build
        else:
            return None

    def __eq__(self, other):
        return (type(other) is MatchSpec and
                (self.spec, self.optional, self.target) ==
                (other.spec, other.optional, other.target))

    def __hash__(self):
        return hash(self.spec)

    def __repr__(self):
        return "MatchSpec('%s')" % self.__str__()

    def __str__(self):
        res = self.spec
        if self.optional or self.target:
            args = []
            if self.optional:
                args.append('optional')
            if self.target:
                args.append('target='+self.target)
            res = '%s (%s)' % (res, ','.join(args))
        return res


class Package(object):
    """
    The only purpose of this class is to provide package objects which
    are sortable.
    """
    def __init__(self, fn, info):
        self.fn = fn
        self.name = info.get('name')
        self.version = info.get('version')
        self.build = info.get('build')
        self.build_number = info.get('build_number')
        self.channel = info.get('channel')
        self.schannel = info.get('schannel')
        if self.schannel is None:
            self.schannel = config.canonical_channel_name(self.channel)
        try:
            self.norm_version = normalized_version(self.version)
        except ValueError:
            stderrlog.error("\nThe following stack trace is in reference to "
                            "package:\n\n\t%s\n\n" % fn)
            raise
        self.info = info

    def _asdict(self):
        result = self.info.copy()
        result['fn'] = self.fn
        result['norm_version'] = str(self.norm_version)
        return result

    def __lt__(self, other):
        if self.name != other.name:
            raise TypeError('cannot compare packages with different '
                            'names: %r %r' % (self.fn, other.fn))
        return ((self.norm_version, self.build_number, self.build) <
                (other.norm_version, other.build_number, other.build))

    def __eq__(self, other):
        if not isinstance(other, Package):
            return False
        if self.name != other.name:
            return False
        return ((self.norm_version, self.build_number, self.build) ==
                (other.norm_version, other.build_number, other.build))

    def __ne__(self, other):
        return not self == other

    def __gt__(self, other):
        return other < self

    def __le__(self, other):
        return not (other < self)

    def __ge__(self, other):
        return not (self < other)


class Resolve(object):
    def __init__(self, index, sort=False, processed=False):
        if not processed:
            for fkey, info in iteritems(index.copy()):
                for fstr in iterkeys(info.get('with_features_depends', {})):
                    index['%s[%s]' % (fkey, fstr)] = info

        groups = {}
        trackers = {}
        installed = set()
        for fkey, info in iteritems(index):
            groups.setdefault(info['name'], []).append(fkey)
            for feat in info.get('track_features', '').split():
                trackers.setdefault(feat, []).append(fkey)
            if 'link' in info:
                installed.add(fkey)

        self.index = index
        self.groups = groups
        self.installed = installed
        self.trackers = trackers
        self.find_matches_ = {}
        self.ms_depends_ = {}

        if sort:
            for name, group in iteritems(groups):
                groups[name] = sorted(group, key=self.version_key, reverse=True)

    def valid(self, spec, filter=None):
        """Tests if a MatchSpec is satisfiable, ignoring cyclic dependencies.

        Args:
            ms: a MatchSpec object or string
            filter: a dictionary of (fkey,valid) pairs, used to consider a subset
                of dependencies, and to eliminate repeated searches.

        Returns:
            True if the full set of dependencies can be satisfied; False otherwise.
            If filter is supplied and update is True, it will be updated with the
            search results.
        """
        if filter is None:
            filter = {}

        def v_ms_(ms):
            return ms.optional or any(v_fkey_(fkey) for fkey in self.find_matches(ms))

        def v_fkey_(fkey):
            val = filter.get(fkey)
            if val is None:
                filter[fkey] = True
                val = filter[fkey] = all(v_ms_(ms) for ms in self.ms_depends(fkey))
            return val

        return v_ms_(spec) if isinstance(spec, MatchSpec) else v_fkey_(spec)

    def touch(self, specs, filter=None):
        """Determines a conservative set of packages to be considered given a
           MatchSpec. Cyclic dependencies are not solved, so there is no
           guarantee a solution exists.

        Args:
            spec: a MatchSpec or a list of MatchSpecs
            filter: a dictionary of (fkey, valid) pairs to be used when
                testing for package validity.

        This function works in two passes. First, it verifies that the package has
        satisfiable dependencies from among the filtered packages. If not, then it
        is _not_ touched, nor are its dependencies. If so, then it is marked as
        touched, and any of its valid dependencies are as well.
        """
        if filter is None:
            filter = {}
        if isinstance(specs, MatchSpec):
            specs = [specs]
        touched = {}
        while specs:
            spec = specs.pop()
            for fkey in self.find_matches(spec):
                val = touched.get(fkey)
                if val is None:
                    val = touched[fkey] = self.valid(fkey, filter)
                    if val:
                        specs.extend(self.ms_depends(fkey))
        return touched

    def invalid_chains(self, spec, filter):
        """Constructs a set of 'dependency chains' for invalid specs.

        A dependency chain is a tuple of MatchSpec objects, starting with
        the requested spec, proceeding down the dependency tree, ending at
        a specification that cannot be satisfied. Uses self.valid_ as a
        filter, both to prevent chains and to allow other routines to
        prune the list of valid packages with additional criteria.

        Args:
            spec: a package key or MatchSpec
            filter: a dictionary of (fkey, valid) pairs to be used when
                testing for package validity.

        Returns:
            A list of tuples, or an empty list if the MatchSpec is valid.
        """
        snames = set()

        def chains_(slist, nover=False, group=None):
            sname = next(_ for _ in slist).name
            if sname in snames or any(self.valid(spec, filter) for spec in slist):
                return []
            snames.add(sname)
            groups = {}
            for spec in slist:
                for fkey in self.find_matches(spec):
                    groups.setdefault(self.package_name(fkey), []).append(fkey)
            subchains = set()
            sname = spec.name
            for name, fgroup in iteritems(groups):
                deps = {}
                for fkey in fgroup:
                    filter[fkey] = True
                for fkey in fgroup:
                    for m2 in self.ms_depends(fkey):
                        deps.setdefault(m2.name, set()).add(m2)
                for dname, dspecs in iteritems(deps):
                    res = chains_(dspecs, nover=True)
                    if sname[0] == '@':
                        res = [(name,) + r for r in res]
                    subchains.update(res)
                for fkey in fgroup:
                    filter[fkey] = False
            if sname[0] == '@':
                sname = '[feature:%s]' % (sname[1:])
            if subchains:
                return [(sname,) + x for x in subchains]
            elif sname[0] == '[':
                return [(sname,)]
            else:
                return [(s.spec,) for s in slist]
        cdict = {}
        for chain in chains_([spec]):
            cdict.setdefault(chain[-1], []).append(chain)
        cdict2 = {}
        for csuff, cset in iteritems(cdict):
            cset = sorted(cset, key=len)
            cname, _, cver = csuff.partition(' ')
            if len(cset[0]) <= 2:
                chain = cset[0]
            elif len(cset[0]) == 3:
                mids = set(c[1] for c in cset if len(c) == 3)
                chain = (cset[0][0], ','.join(sorted(mids)), csuff)
            else:
                mids = set(c[1] for c in cset)
                chain = (cset[0][0], ','.join(sorted(mids)), '...', csuff)
            cname, _, cver = csuff.partition(' ')
            chain = chain[:-1] + (cname,)
            cdict2.setdefault(chain, set()).add(cver)
        res = []
        for chain, cvers in iteritems(cdict2):
            cvers = '' if '' in cvers else ' ' + '|'.join(sorted(cvers))
            res.append(chain[:-1] + (chain[-1] + cvers,))
        return sorted(res)

    def verify_specs(self, specs, unsat=False, target=None):
        """Perform a quick verification that specs and dependencies are reasonable.

        Args:
            specs: An iterable of strings or MatchSpec objects to be tested.

        Returns:
            Nothing, but if there is a conflict, an error is thrown.

        Note that this does not attempt to resolve circular dependencies.
        """
        filter = {}
        bad_deps = []
        specs = list(map(MatchSpec, specs))
        for ms in specs:
            if not ms.optional:
                bad_deps.extend(self.invalid_chains(ms, filter))
        if not bad_deps:
            return specs
        if not unsat:
            raise NoPackagesFound(bad_deps)
        if target:
            bad_deps2 = [c for c in bad_deps if c[-1].split(' ', 1)[0] in target]
            bad_deps = bad_deps2 or bad_deps
        raise Unsatisfiable(bad_deps)

    def get_dists(self, specs, full=False):
        log.debug('Retrieving packages for: %s' % (specs,))

        specs = self.verify_specs(specs)
        filter = {}
        snames = set()

        class BadPrune:
            def __init__(self, dep):
                self.dep = dep

        def filter_group(matches):
            # If we are here, then this dependency is mandatory,
            # so add it to the master list. That way it is still
            # participates in the pruning even if one of its
            # parents is pruned away
            match1 = next(ms for ms in matches)
            isopt = all(ms.optional for ms in matches)
            name = match1.name
            isfeat = name[0] == '@'
            first = name not in snames

            if isfeat:
                assert len(matches) == 1 and match1.strictness == 1
                group = self.trackers.get(name[1:], [])
            else:
                group = self.groups.get(name, [])

            # Prune packages that don't match any of the patterns
            # or which have unsatisfiable dependencies
            nold = nnew = 0
            for fkey in group:
                if filter.setdefault(fkey, True):
                    nold += 1
                    sat = isfeat or self.match_any(matches, fkey)
                    sat = sat and all(any(filter.get(f2, True) for f2 in self.find_matches(ms))
                                      for ms in self.ms_depends(fkey))
                    filter[fkey] = sat
                    nnew += sat

            # Quick exit if we detect unsatisfiability
            reduced = nnew < nold
            if reduced:
                log.debug('%s: pruned from %d -> %d' % (name, nold, nnew))
            if nnew == 0:
                if name in snames:
                    snames.remove(name)
                if not isopt:
                    raise BadPrune(name)
                return nnew != 0
            if not reduced and not first or isopt or isfeat:
                return reduced

            # Perform the same filtering steps on any dependencies shared across
            # *all* packages in the group. Even if just one of the packages does
            # not have a particular dependency, it must be ignored in this pass.
            if first:
                snames.add(name)
            cdeps = defaultdict(list)
            for fkey in group:
                if filter[fkey]:
                    for m2 in self.ms_depends(fkey):
                        if m2.name[0] != '@' and not m2.optional:
                            cdeps[m2.name].append(m2)
            cdeps = {mname: set(deps) for mname, deps in iteritems(cdeps) if len(deps) >= nnew}
            if cdeps:
                matches = [(ms,) for ms in matches]
                if sum(filter_group(deps) for deps in itervalues(cdeps)):
                    reduced = True

            return reduced

        # Iterate in the filtering process until no more progress is made
        feats = set(self.trackers.keys())
        slist = specs
        onames = set(s.name for s in specs)
        new_specs = []
        for iter in range(10):
            first = True
            try:
                unsat = None
                while sum(filter_group([s]) for s in slist):
                    new_specs = [MatchSpec(n) for n in snames - onames]
                    slist = specs + new_specs
                    first = False
            except BadPrune as unsat:
                new_specs = None
                unsat = unsat.dep
            if not unsat and first and iter:
                break
            touched = self.touch(specs, {} if unsat else filter)
            if unsat:
                break
            nfeats = set()
            for fkey, val in iteritems(touched):
                if val:
                    nfeats.update(self.track_features(fkey))
            if len(nfeats) >= len(feats):
                break
            pruned = False
            for feat in feats - nfeats:
                feats.remove(feat)
                for fkey in self.trackers[feat]:
                    if filter.get(fkey, True):
                        filter[fkey] = False
                        pruned = True
            if not pruned:
                break

        dists = {fkey: self.index[fkey] for fkey, val in iteritems(touched) if val}
        if full:
            return dists, new_specs, unsat
        return dists

    def match_any(self, mss, fkey):
        rec = self.index[fkey]
        n, v, b = rec['name'], rec['version'], rec['build']
        return any(n == ms.name and ms.match_fast(v, b) for ms in mss)

    def match(self, ms, fkey):
        return MatchSpec(ms).match(self.index[fkey])

    def match_fast(self, ms, fkey):
        rec = self.index[fkey]
        return ms.match_fast(rec['version'], rec['build'])

    def find_matches(self, ms):
        ms = MatchSpec(ms)
        res = self.find_matches_.get(ms, None)
        if res is None:
            if ms.name[0] == '@':
                res = self.trackers.get(ms.name[1:], [])
            else:
                res = self.groups.get(ms.name, [])
                res = [p for p in res if self.match_fast(ms, p)]
            self.find_matches_[ms] = res
        return res

    def ms_depends(self, fkey):
        deps = self.ms_depends_.get(fkey, None)
        if deps is None:
            rec = self.index[fkey]
            if fkey.endswith(']'):
                f2, fstr = fkey.rsplit('[', 1)
                fdeps = {d.name: d for d in self.ms_depends(f2)}
                for dep in rec['with_features_depends'][fstr[:-1]]:
                    dep = MatchSpec(dep)
                    fdeps[dep.name] = dep
                deps = list(fdeps.values())
            else:
                deps = [MatchSpec(d) for d in rec.get('depends', [])]
            deps.extend(MatchSpec('@'+feat) for feat in self.features(fkey))
            self.ms_depends_[fkey] = deps
        return deps

    def version_key(self, fkey, vtype=None):
        rec = self.index[fkey]
        cpri = -rec.get('priority', 1)
        ver = normalized_version(rec.get('version', ''))
        bld = rec.get('build_number', 0)
        return (cpri, ver, bld) if config.channel_priority else (ver, cpri, bld)

    def features(self, fkey):
        return set(self.index[fkey].get('features', '').split())

    def track_features(self, fkey):
        return set(self.index[fkey].get('track_features', '').split())

    def package_triple(self, fkey):
        rec = self.index.get(fkey, None)
        if rec is None:
            fkey = fkey.rsplit('[', 1)[0].rsplit('/', 1)[-1]
            if fkey.endswith('.tar.bz2'):
                fkey = fkey[:-8]
            return fkey.rsplit('-', 2)
        return (rec['name'], rec['version'], rec['build'])

    def package_name(self, fkey):
        return self.package_triple(fkey)[0]

    def get_pkgs(self, ms, emptyok=False):
        ms = MatchSpec(ms)
        pkgs = [Package(fkey, self.index[fkey]) for fkey in self.find_matches(ms)]
        if not pkgs and not emptyok:
            raise NoPackagesFound([(ms,)])
        return pkgs

    def push_MatchSpec(self, C, ms, numeric=False):
        ms = MatchSpec(ms)
        name = '@s@' + ms.spec + ('?' if ms.optional else '')
        m = C.from_name(name)
        if m is not None:
            return m if numeric else name
        if m is None:
            if ms.name[0] == '@':
                assert ms.strictness == 1
                libs = [] if ms.optional else self.trackers.get(ms.name[1:], [])
            else:
                target = not ms.optional
                tgroup = self.groups.get(ms.name, [])
                libs = [fkey for fkey in tgroup if self.match_fast(ms, fkey) == target]
                if ms.spec != ms.name and len(libs) == len(tgroup):
                    m = self.push_MatchSpec(C, ms.name, numeric=True)
        if m is None:
            m = C.Any(libs)
        if ms.optional:
            m = C.Not(m)
        C.name_var(m, name)
        return m if numeric else name

    def gen_clauses(self):
        C = Clauses()

        # Creates a variable that represents the proposition:
        #     Does the package set include package "fn"?
        for name, group in iteritems(self.groups):
            for fkey in group:
                C.new_var(fkey)
            # Install no more than one version of each package
            C.Require(C.AtMostOne, group)
            # Create an on/off variable for the entire group
            self.push_MatchSpec(C, name)

        # Create propositions that assert:
        #     If package "fn" is installed, its dependencie must be satisfied
        for group in itervalues(self.groups):
            for fkey in group:
                nkey = C.Not(fkey)
                for ms in self.ms_depends(fkey):
                    if not ms.optional:
                        C.Require(C.Or, nkey, self.push_MatchSpec(C, ms))
        return C

    def generate_spec_constraints(self, C, specs):
        return [(self.push_MatchSpec(C, ms),) for ms in specs]

    def generate_feature_count(self, C):
        return {self.push_MatchSpec(C, '@' + name): 1 for name in iterkeys(self.trackers)}

    def generate_feature_metric(self, C):
        eq = {}
        total = 0
        for name, group in iteritems(self.groups):
            nf = [len(self.features(fkey)) for fkey in group]
            maxf = max(nf)
            eq.update({fn: maxf-fc for fn, fc in zip(group, nf) if fc < maxf})
            total += maxf
        return eq, total

    def generate_removal_count(self, C, specs):
        return {'!' + self.push_MatchSpec(C, ms.name): 1 for ms in specs}

    def generate_package_count(self, C, missing):
        return {self.push_MatchSpec(C, nm): 1 for nm in missing}

    def generate_version_metrics(self, C, specs, include0=False):
        eqv = {}
        eqb = {}
        sdict = {}
        for s in specs:
            s = MatchSpec(s)  # needed for testing
            sdict.setdefault(s.name, []).append(s)
        for name, mss in iteritems(sdict):
            pkgs = [(self.version_key(p), p) for p in self.groups.get(name, [])]
            # If the "target" field in the MatchSpec is supplied, that means we want
            # to minimize the changes to the currently installed package. We prefer
            # any upgrade over any downgrade, but beyond that we want minimal change.
            targets = [ms.target for ms in mss if ms.target and ms.target in self.index]
            if targets:
                v1 = [(self.version_key(p), p) for p in targets]
                tver = max(v1)
                v2 = [p for p in pkgs if p > tver]
                v3 = list(reversed([p for p in pkgs if p <= tver and p not in v1]))
                pkgs = v1 + v2 + v3
            pkey = None
            for nkey, npkg in pkgs:
                if pkey is None:
                    iv = ib = 0
                elif pkey[0] != nkey[0] or pkey[1] != nkey[1]:
                    iv += 1
                    ib = 0
                elif pkey[2] != nkey[2]:
                    ib += 1
                if iv or include0:
                    eqv[npkg] = iv
                if ib or include0:
                    eqb[npkg] = ib
                pkey = nkey
        return eqv, eqb

    def dependency_sort(self, must_have):
        def lookup(value):
            return set(ms.name for ms in self.ms_depends(value + '.tar.bz2'))
        digraph = {}
        if not isinstance(must_have, dict):
            must_have = {self.package_name(dist): dist for dist in must_have}
        for key, value in iteritems(must_have):
            depends = lookup(value)
            digraph[key] = depends
        sorted_keys = toposort(digraph)
        must_have = must_have.copy()
        # Take all of the items in the sorted keys
        # Don't fail if the key does not exist
        result = [must_have.pop(key) for key in sorted_keys if key in must_have]
        # Take any key that were not sorted
        result.extend(must_have.values())
        return result

    def explicit(self, specs):
        """
        Given the specifications, return:
          A. if one explicit specification (strictness=3) is given, and
             all dependencies of this package are explicit as well ->
             return the filenames of those dependencies (as well as the
             explicit specification)
          B. if not one explicit specifications are given ->
             return the filenames of those (not thier dependencies)
          C. None in all other cases
        """
        specs = list(map(MatchSpec, specs))
        if len(specs) == 1:
            ms = MatchSpec(specs[0])
            fn = ms.to_filename()
            if fn is None:
                return None
            if fn not in self.index:
                return None
            res = [ms2.to_filename() for ms2 in self.ms_depends(fn)]
            res.append(fn)
        else:
            res = [spec.to_filename() for spec in specs if str(spec) != 'conda']

        if None in res:
            return None
        res.sort()
        dotlog.debug('explicit(%r) finished' % specs)
        return res

    def sum_matches(self, fn1, fn2):
        return sum(self.match(ms, fn2) for ms in self.ms_depends(fn1))

    def find_substitute(self, installed, features, fn):
        """
        Find a substitute package for `fn` (given `installed` packages)
        which does *NOT* have `features`.  If found, the substitute will
        have the same package name and version and its dependencies will
        match the installed packages as closely as possible.
        If no substitute is found, None is returned.
        """
        name, version, unused_build = fn.rsplit('-', 2)
        candidates = {}
        for pkg in self.get_pkgs(MatchSpec(name + ' ' + version)):
            fn1 = pkg.fn
            if self.features(fn1).intersection(features):
                continue
            key = sum(self.sum_matches(fn1, fn2) for fn2 in installed)
            candidates[key] = fn1

        if candidates:
            maxkey = max(candidates)
            return candidates[maxkey]
        else:
            return None

    def bad_installed(self, installed, new_specs):
        log.debug('Checking if the current environment is consistent')
        if not installed:
            return None, []
        xtra = []
        dists = {}
        specs = []
        for fn in installed:
            rec = self.index.get(fn)
            if rec is None:
                xtra.append(fn)
            else:
                dists[fn] = rec
                specs.append(MatchSpec(' '.join(self.package_triple(fn))))
        if xtra:
            log.debug('Packages missing from index: %s' % ', '.join(xtra))
        r2 = Resolve(dists, True, True)
        C = r2.gen_clauses()
        constraints = r2.generate_spec_constraints(C, specs)
        try:
            solution = C.sat(constraints)
        except TypeError:
            log.debug('Package set caused an unexpected error, assuming a conflict')
            solution = None
        limit = None
        if not solution or xtra:
            def get_(name, snames):
                if name not in snames:
                    snames.add(name)
                    for fn in self.groups.get(name, []):
                        for ms in self.ms_depends(fn):
                            get_(ms.name, snames)
            snames = set()
            for spec in new_specs:
                get_(MatchSpec(spec).name, snames)
            xtra = [x for x in xtra if x not in snames]
            if xtra or not (solution or all(s.name in snames for s in specs)):
                limit = set(s.name for s in specs if s.name in snames)
                xtra = [fn for fn in installed if self.package_name(fn) not in snames]
                log.debug(
                    'Limiting solver to the following packages: %s' %
                    ', '.join(limit))
        if xtra:
            log.debug('Packages to be preserved: %s' % ', '.join(xtra))
        return limit, xtra

    def restore_bad(self, pkgs, preserve):
        if preserve:
            sdict = {self.package_name(pkg): pkg for pkg in pkgs}
            pkgs.extend(p for p in preserve if self.package_name(p) not in sdict)

    def install_specs(self, specs, installed, update_deps=True):
        specs = list(map(MatchSpec, specs))
        snames = {s.name for s in specs}
        log.debug('Checking satisfiability of current install')
        limit, preserve = self.bad_installed(installed, specs)
        for pkg in installed:
            if pkg not in self.index:
                continue
            name, version, build = self.package_triple(pkg)
            if name in snames or limit is not None and name not in limit:
                continue
            # If update_deps=True, set the target package in MatchSpec so that
            # the solver can minimize the version change. If update_deps=False,
            # fix the version and build so that no change is possible.
            if update_deps:
                spec = MatchSpec('%s (target=%s)' % (name, pkg))
            else:
                spec = MatchSpec('%s %s %s' % (name, version, build))
            specs.append(spec)
        return specs, preserve

    def install(self, specs, installed=None, update_deps=True, returnall=False):
        len0 = len(specs)
        specs, preserve = self.install_specs(specs, installed or [], update_deps)
        pkgs = self.solve(specs, len0=len0, returnall=returnall)
        self.restore_bad(pkgs, preserve)
        return pkgs

    def remove_specs(self, specs, installed):
        # These never match true version/build combos so it forces removal
        specs = [MatchSpec('%s @ @' % s, optional=True) for s in specs]
        snames = {s.name for s in specs}
        limit, _ = self.bad_installed(installed, specs)
        preserve = []
        for pkg in installed:
            nm, ver, build = self.package_triple(pkg)
            if nm in snames:
                continue
            elif limit is not None:
                preserve.append(pkg)
            elif ver:
                specs.append(MatchSpec('%s >=%s' % (nm, ver), optional=True, target=pkg))
            else:
                specs.append(MatchSpec(nm, optional=True, target=pkg))
        return specs, preserve

    def remove(self, specs, installed):
        specs, preserve = self.remove_specs(specs, installed)
        pkgs = self.solve(specs)
        self.restore_bad(pkgs, preserve)
        return pkgs

    def solve(self, specs, len0=None, returnall=False):
        try:
            stdoutlog.info("Solving package specifications ...")
            dotlog.debug("Solving for %s" % (specs,))

            # Find the compliant packages
            specs = list(map(MatchSpec, specs))
            if len0 is None:
                len0 = len(specs)
            dists, new_specs, unsat = self.get_dists(specs, True)
            if not dists and not unsat:
                return False if dists is None else ([[]] if returnall else [])

            # Check if satisfiable
            dotlog.debug('Checking satisfiability')
            r2 = Resolve(dists, True, True)
            C = r2.gen_clauses()
            constraints = r2.generate_spec_constraints(C, specs)
            solution = C.sat(constraints, True)
            if not solution:
                def mysat(specs):
                    constraints = r2.generate_spec_constraints(C, specs)
                    res = C.sat(constraints, False) is not None
                    return res
                stdoutlog.info("\nUnsatisfiable specifications detected; generating hint ...")
                hint = minimal_unsatisfiable_subset(specs, sat=mysat, log=False)
                hnames = set(h.name for h in hint)
                if unsat:
                    if unsat[0] == '@' and unsat[1:] in r2.trackers:
                        del r2.trackers[unsat]
                    elif unsat in r2.groups:
                        del r2.groups[unsat]
                hnames.add(unsat)
                r2.verify_specs(hint, unsat=True, target=hnames)
                raise Unsatisfiable([(h, ) for h in map(str, hint)])

            speco = []  # optional packages
            specr = []  # requested packages
            speca = []  # all other packages
            specm = set(r2.groups)  # missing from specs
            for k, s in enumerate(chain(specs, new_specs)):
                if s.name in specm:
                    specm.remove(s.name)
                if not s.optional:
                    (specr if k < len0 else speca).append(s)
                elif any(r2.find_matches(s)):
                    s = MatchSpec(s.name, optional=True, target=s.target)
                    speco.append(s)
                    speca.append(s)
            speca.extend(MatchSpec(s) for s in specm)

            # Removed packages: minimize count
            eq_optional_c = r2.generate_removal_count(C, speco)
            solution, obj7 = C.minimize(eq_optional_c, solution)
            dotlog.debug('Package removal metric: %d' % obj7)

            # Requested packages: maximize versions, then builds
            eq_req_v, eq_req_b = r2.generate_version_metrics(C, specr)
            solution, obj3 = C.minimize(eq_req_v, solution)
            solution, obj4 = C.minimize(eq_req_b, solution)
            dotlog.debug('Initial package version/build metrics: %d/%d' % (obj3, obj4))

            # Track features: minimize feature count
            eq_feature_count = r2.generate_feature_count(C)
            solution, obj1 = C.minimize(eq_feature_count, solution)
            dotlog.debug('Track feature count: %d' % obj1)

            # Featured packages: maximize featured package count
            eq_feature_metric, ftotal = r2.generate_feature_metric(C)
            solution, obj2 = C.minimize(eq_feature_metric, solution)
            obj2 = ftotal - obj2
            dotlog.debug('Package feature count: %d' % obj2)

            # Remaining packages: maximize versions, then builds, then count
            eq_v, eq_b = r2.generate_version_metrics(C, speca)
            solution, obj5 = C.minimize(eq_v, solution)
            solution, obj6 = C.minimize(eq_b, solution)
            dotlog.debug('Additional package version/build metrics: %d/%d' % (obj5, obj6))

            # Prune unnecessary packages
            eq_c = r2.generate_package_count(C, specm)
            solution, obj7 = C.minimize(eq_c, solution, trymax=True)
            dotlog.debug('Weak dependency count: %d' % obj7)

            def clean(sol):
                return [q for q in (C.from_index(s) for s in sol)
                        if q and q[0] != '!' and '@' not in q]
            dotlog.debug('Looking for alternate solutions')
            nsol = 1
            psolutions = []
            psolution = clean(solution)
            psolutions.append(psolution)
            while True:
                nclause = tuple(C.Not(C.from_name(q)) for q in psolution)
                solution = C.sat((nclause,), True)
                if solution is None:
                    break
                nsol += 1
                if nsol > 10:
                    dotlog.debug('Too many solutions; terminating')
                    break
                psolution = clean(solution)
                psolutions.append(psolution)

            if nsol > 1:
                psols2 = list(map(set, psolutions))
                common = set.intersection(*psols2)
                diffs = [sorted(set(sol) - common) for sol in psols2]
                stdoutlog.info(
                    '\nWarning: %s possible package resolutions '
                    '(only showing differing packages):%s%s' %
                    ('>10' if nsol > 10 else nsol,
                     dashlist(', '.join(diff) for diff in diffs),
                     '\n  ... and others' if nsol > 10 else ''))

            def stripfeat(sol):
                return sol.split('[')[0]
            stdoutlog.info('\n')
            if returnall:
                return [sorted(map(stripfeat, psol)) for psol in psolutions]
            else:
                return sorted(map(stripfeat, psolutions[0]))
        except:
            stdoutlog.info('\n')
            raise
