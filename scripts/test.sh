#!/usr/bin/env bash
# The repo's own checks: the unit tests, the shell scripts' syntax, the Python of this repo (its files, and the Python
# its shell scripts run, as far as the Python step reads them; what it cannot read fails), the systemd units and their
# drop-ins (systemd-analyze verify) and every program they run.  Run it before every commit.
#   scripts/test.sh              on this machine, without the stub harnesses that start fake engines
#                                (tests/ab_engine_stubs.sh, ab_model_run_stubs.sh, switch_model_stubs.sh) and the
#                                test of scripts/deploy.sh that starts what strata-preflight.sh takes for an engine:
#                                while one runs, a start of strata-q3 is refused
#   scripts/test.sh --harnesses  with them; here only while strata-q3 is stopped and no experiment runs (otherwise
#                                tests/harness_run.py fails them)
#   scripts/test.sh --repo-only  no file outside the repo: the units' programs outside it are not checked, and the tests
#                                that need this machine's files are skipped.  CI runs `--repo-only --harnesses
#                                --jobs 6` (.github/workflows/test.yml), and shellcheck must be installed then.
#   scripts/test.sh --jobs N     the unit tests in N worker processes, N from 1 to 16 (1 without it, as here, where the
#                                service runs beside them); the checks below see the tests of all of them.  With
#                                --harnesses here, keep 1: a test of tests/harness_run.py runs a stand-in for an
#                                experiment driver, and a harness in another worker then refuses to run
# A run fails when a test fails or does not finish, when no test ran (every test was skipped, by itself or with its
# class or module; a test whose subtests were skipped ran), when a .py file under tests/ is not a test module the runner
# loads (tests/test_*.py) or a helper of theirs, is no regular file, or loads no test (a test counts for the module that
# loads it: also a doctest of another module, a FunctionTestCase, the skip of a whole module at its import), when a test
# never runs (a test method that no loaded test calls and whose code never ran: defined twice, in a class that is no
# TestCase the runner loads nor a base of one, also under if, try, with or match, bound to something else later, or
# replaced by a decorator; a function outside a class whose name starts with test, whose code never ran, that is no
# FunctionTestCase's function and that no code of its module that may run reads as a global: a function whose code ran,
# the function of a loaded test (also of a skipped one) and one that a loaded test holds (in its closure or as a
# default), and in turn the module's functions that those read (and the module's methods that those read: as self.NAME,
# cls.NAME or super().NAME, those of the reader's class, the classes it derives from and those that derive from it; as
# CLASS.NAME, those of CLASS and the classes it derives from), make it a helper (not the module's top level, as under if
# __name__ == "__main__", a function that never runs, nor a local variable of that name; a load_tests that ran and reads
# it under a condition that is false does; two lambdas that start on one line are one to this check, so what one that
# never runs reads counts when the other ran); none of a module skipped at its import is checked, and in a module that
# turns the profiler off at its import (also by importing one that does) a function that its code names anywhere is not;
# a generator, an async generator, or an async one in a TestCase that does not await it, also behind a functools.wraps
# decorator; a loaded test that never starts, as in a class that overrides run(), but with a class or module whose
# setUpClass or setUpModule raised; a test that starts but whose function's body does not run, as behind a decorator
# that returns without calling it: cProfile sees which code runs in the runner's thread, from the loading of the tests
# on, so a test whose function runs in another thread fails, one that turns the profiler off is not checked, one that
# sets it back, or calls it from a profile function of its own, keeps it, and one that expects none (sys.getprofile() is
# None) fails; a test behind a decorator without functools.wraps runs when its function ran, or when its test was
# skipped or never started and the decorator holds the function, in its closure or as an argument's default), when a
# test returns something (a coroutine it returns never runs), when a test expects to fail, or when a test is skipped for
# another reason than those two ("harness:" without --harnesses, "machine:" with --repo-only), and when the tests leave
# something in the TMPDIR they get (a directory of this run's).  Code review has to see what runs but checks nothing,
# as a test that returns early from its own body.
# (The machine's state is checked by check-drift.sh and check-deployment.sh, not here.)
# Usage: scripts/test.sh [--repo-only] [--harnesses] [--jobs N]
set -u -o pipefail
D=$(cd "$(dirname "$0")/.." && pwd)
cd "$D" || exit 1
LIVE=/home/thc1006/dev/p100-q3-deployment     # the checkout the units name: in their copies here, the tested tree
REPO_ONLY=0; HARNESSES=0; JOBS=1
usage() { echo "usage: scripts/test.sh [--repo-only] [--harnesses] [--jobs N]  (N: 1 to 16)" >&2; exit 2; }
while [ $# -gt 0 ]; do
  case $1 in
    --repo-only) REPO_ONLY=1 ;;
    --harnesses) HARNESSES=1 ;;
    --jobs) [ $# -ge 2 ] && [[ $2 =~ ^([1-9]|1[0-6])$ ]] || usage; JOBS=$2; shift ;;
    *) usage ;;
  esac
  shift
done
fail=0
step() { printf '\n== %s\n' "$1"; }
tmp=$(mktemp -d) || { echo "cannot create a temporary directory"; exit 1; }
trap 'rm -rf -- "$tmp"' EXIT
# its path without symlinks and "..": systemd-analyze names the files in it so, and the step that runs it looks for that
tmp=$(cd -- "$tmp" && pwd -P) || { echo "cannot enter the temporary directory"; exit 1; }

step "unit tests (tests/)"
# the runner writes its verdict last: a test that ends the process (os._exit) leaves none.  The tests get a TMPDIR of
# their own, which must be empty when they have ended, and the lock of tests/harness_run.py is next to it
mkdir "$tmp/tests" || { echo "cannot create the tests' temporary directory"; exit 1; }
TMPDIR=$tmp/tests STRATA_TEST_ENGINES_LOCK=$tmp/engines.lock TEST_REPO_ONLY=$REPO_ONLY \
  STRATA_TEST_HARNESSES=$HARNESSES TEST_JOBS=$JOBS python3 -B - "$tmp/verdict" <<'EOF' || fail=1
import ast, atexit, cProfile, ctypes, functools, inspect, json, os, pathlib, signal, stat, symtable, sys, threading, \
    time, traceback, types, unittest, warnings, weakref
for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(errors="backslashreplace")        # a lone surrogate in a name prints, as \udcxx
repo_only = os.environ["TEST_REPO_ONLY"] == "1"
harnesses = os.environ["STRATA_TEST_HARNESSES"] == "1"
helpers = {"harness_run.py", "fake_strata.py"}            # imported by the tests, not tests themselves
PATTERN = "test_*.py"
bad, defined, free = [], [], []  # defined: (file, class, method, line of its def, its first line) of each test method;
                                 # free: (file, name, line, first line) of each test function outside a class (one that
                                 # code that may run reads is a helper, as one that a skipped test calls: see below)
FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


class Profile(cProfile.Profile):
    """cProfile's profiler, which also takes the place of a profile function: a test that saves sys.getprofile() and
    gives it back to sys.setprofile() hands it the profiler, whose first call then turns it on again (not when a hook
    of a test's calls it in turn).  The function of that call's frame counts as run: cProfile counts a call when it sees
    it return, and it does not see that one."""
    def __call__(self, frame, event, arg):
        self.entered.add(frame.f_code)
        if sys.getprofile() is self:
            self.enable()


profile = Profile(builtins=False, subcalls=False)    # which code runs: the body of each test should, and a helper whose
profile.entered = set()                              # name starts with test may (also at import)


def blocks(node):
    """The statement lists of an if, for, while, try, with or match statement (their bodies, else and finally parts,
    and cases)."""
    for field in ("body", "orelse", "finalbody"):
        stmts = getattr(node, field, None)
        if isinstance(stmts, list) and stmts and isinstance(stmts[0], ast.stmt):
            yield stmts
    for handler in getattr(node, "handlers", ()):
        yield handler.body
    for case in getattr(node, "cases", ()):
        yield case.body


def scan(p, stmts, owner, in_function):
    """The test methods and classes of a statement list (owner: the class whose body it is part of): a class, or a test
    method of one class, defined twice in it; and the test methods and the test functions outside a class, which must
    run (see below)."""
    classes, methods = {}, {}
    for node in stmts:
        if isinstance(node, ast.ClassDef):
            if node.name in classes:
                bad.append(f"{p}:{node.lineno}: class {node.name} is defined again: the first one never runs")
            classes.setdefault(node.name, node.lineno)
            scan(p, node.body, node, in_function)
        elif isinstance(node, FUNCS):
            first = min([d.lineno for d in node.decorator_list] + [node.lineno])   # its code's first line
            if node.name.startswith("test") and owner is not None:
                if node.name in methods:
                    bad.append(f"{p}:{node.lineno}: {owner.name}.{node.name} is defined again (first at line "
                               f"{methods[node.name]}): the first one never runs")
                methods.setdefault(node.name, node.lineno)
                defined.append((p, owner.name, node.name, node.lineno, first))
            elif node.name.startswith("test") and not in_function:
                free.append((p, node.name, node.lineno, first))
            scan(p, node.body, None, True)
        else:
            for sub in blocks(node):
                scan(p, sub, owner, in_function)


COMPREHENSIONS = {"listcomp", "setcomp", "dictcomp", "genexpr"}
TOPS = (ast.Module, ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler) + \
    ((ast.Match, ast.match_case) if hasattr(ast, "Match") else ())    # statement lists at the module's top
fns, named = {}, {}     # per test module (its real path): its Fn of each function that may run on its own; the names
                        # that any of its code reads (also at the top level)
classes = {}            # per test module: its classes by name, with the names of their bases


class Fn:
    """A def outside a function (at the module's top, in a block there, or a method) or a lambda outside a function, and
    what its code reads as globals (also in the functions, lambdas and comprehensions it holds); binding: the module's
    name for it (a def at the top, or NAME = lambda there), through which code that reads the name runs it; method:
    the name of the class in whose body it is a def, or None; attrs: what its code reads as an attribute, (owner,
    NAME): ("self", NAME) for self.NAME, cls.NAME or super().NAME, (CLASS, NAME) for CLASS.NAME."""
    def __init__(self, name, line, first, binding, method=None, attrs=()):
        self.name, self.line, self.first, self.binding, self.reads = name, line, first, binding, set()
        self.method, self.attrs = method, set(attrs)

    def key(self):
        """(its first line, its name) as a code object of it gives them (see where())."""
        return self.first, "<lambda>" if self.name == "lambda" else self.name


def globals_read(table):
    """The names that the code of a symbol table and of the tables it holds reads as globals."""
    names = {x.get_name() for x in table.get_symbols() if x.is_referenced() and x.is_global()}
    for t in table.get_children():
        names |= globals_read(t)
    return names


def functions_of(tree, table):
    """The Fn of a module's functions that may run on their own (see Fn), with what each reads (a function's own name
    among those makes no helper of it: what is read counts only when it ran, is a test's or is read itself)."""
    found = {}                                 # (name, or "lambda", and the line of its def): [Fn]

    def walk(node, top, inside, binding=None, method=None):
        """top: at the module's top (also in its blocks), where a def is bound to its name; inside: in a function;
        method: the class in whose body it is."""
        if isinstance(node, FUNCS + (ast.Lambda,)):
            args = node.args                   # what runs where the def stands: its decorators, its defaults
            for x in getattr(node, "decorator_list", []) + args.defaults + [d for d in args.kw_defaults if d]:
                walk(x, False, inside)
            if not inside:
                first = min([d.lineno for d in getattr(node, "decorator_list", [])] + [node.lineno])
                name = node.name if isinstance(node, FUNCS) else "lambda"
                attrs = {("self" if n.value.id in ("self", "cls") else n.value.id, n.attr) for n in ast.walk(node)
                         if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
                attrs |= {("self", n.attr) for n in ast.walk(node) if isinstance(n, ast.Attribute) and
                          isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name) and
                          n.value.func.id == "super"}
                found.setdefault((name, node.lineno), []).append(
                    Fn(name, node.lineno, first, node.name if isinstance(node, FUNCS) and top else binding,
                       method if isinstance(node, FUNCS) else None, attrs))
            for x in (node.body if isinstance(node.body, list) else [node.body]):
                walk(x, False, True)
            return
        lam = None                             # NAME = lambda ... at the top: the lambda runs where NAME is called
        if top and isinstance(node, ast.Assign) and isinstance(node.value, ast.Lambda) and len(node.targets) == 1 and \
                isinstance(node.targets[0], ast.Name):
            lam = node.targets[0].id
        keep = top and isinstance(node, TOPS) and not isinstance(node, ast.ClassDef)
        for x in ast.iter_child_nodes(node):
            walk(x, keep, inside, lam if lam and x is node.value else None,
                 node.name if isinstance(node, ast.ClassDef) else None)

    def tables(t, inside):
        for c in t.get_children():
            if c.get_type() == "function" and c.get_name() not in COMPREHENSIONS and not inside:
                for fn in found.get((c.get_name(), c.get_lineno()), []):
                    fn.reads |= globals_read(c)
                tables(c, True)
            else:
                tables(c, inside)

    walk(tree, True, False)
    tables(table, False)
    return [fn for group in found.values() for fn in group]


for top, dirs, names in os.walk("tests"):
    dirs[:] = sorted(d for d in dirs if d != "__pycache__")
    for name in sorted(names):
        p = pathlib.Path(top, name)
        if p.suffix != ".py" or (p.parent == pathlib.Path("tests") and name in helpers):
            continue
        if p.parent != pathlib.Path("tests"):
            bad.append(f"{p} is in a directory under tests/, which the runner does not load")
        elif not p.match(PATTERN):
            bad.append(f"{p} is not named {PATTERN}, so it is never loaded")
        else:                                  # what defining a test can lose without an error
            try:
                if not stat.S_ISREG(os.stat(p).st_mode):        # a FIFO would block the read
                    bad.append(f"{p} is not a regular file, which the runner does not read")
                    continue
                source = p.read_bytes()
                tree = ast.parse(source, str(p))
                fns[os.path.realpath(p)] = functions_of(tree, symtable.symtable(source, str(p), "exec"))
                classes[os.path.realpath(p)] = {n.name: [b.id for b in n.bases if isinstance(b, ast.Name)]
                                                for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
            except OSError as e:
                bad.append(f"cannot read {p}: {e.strerror}")
                continue
            except (SyntaxError, ValueError) as e:
                bad.append(f"{p} does not compile: {e}")
                continue
            named[os.path.realpath(p)] = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and
                                          isinstance(n.ctx, ast.Load)}
            scan(p, tree.body, None, False)


def tests_of(s):
    for t in s:
        yield from tests_of(t) if isinstance(t, unittest.TestSuite) else [t]


def tid(t):
    """A test's id; a FunctionTestCase of a callable without a name (functools.partial) has none: its repr then."""
    try:
        return t.id()
    except AttributeError:
        return repr(t)


origin, dark = {}, set()        # id() of each loaded test: the test module that loaded it; dark: the test modules that
                                # turned the profiler off at their import (what ran of their code then is not known)


class Loader(unittest.TestLoader):
    """unittest's loader, which notes the test module of each test it loads (as one that its load_tests adds, a doctest
    of another module or a FunctionTestCase): a module has tests when it loads some.  It turns the profiler on again
    for the next module's import."""
    def loadTestsFromModule(self, module, *args, **kwargs):
        if sys.getprofile() is not profile:
            dark.add(module.__name__)
        suite = super().loadTestsFromModule(module, *args, **kwargs)
        for t in tests_of(suite):
            origin.setdefault(id(t), module.__name__)
        profile.enable()
        return suite


profile.enable()
suite = Loader().discover("tests", pattern=PATTERN)


def unwrapped(fn):
    """The function a functools.wraps decorator (or several) wraps, or fn."""
    try:
        return inspect.unwrap(fn) if callable(fn) else fn
    except ValueError:                         # a __wrapped__ cycle
        return fn


def at(code):
    """Where a function's code is: (its file, its first line), as the scan above finds a def."""
    return os.path.realpath(code.co_filename), code.co_firstlineno


def where(code):
    """(its file, its first line, its name): a code object as an Fn's key() names it."""
    return os.path.realpath(code.co_filename), code.co_firstlineno, code.co_name


def test_function(t):
    """The code of the function that a FunctionTestCase runs (through functools.partial and functools.wraps), or
    None."""
    f = getattr(t, "_testFunc", None)
    while isinstance(f, functools.partial):
        f = f.func
    return getattr(unwrapped(f), "__code__", None)


def inner(fn):
    """The code of the functions that fn holds, in its closure or as the default of an argument, and that those hold:
    what a decorator without functools.wraps wraps."""
    seen, todo = set(), [fn]
    while todo:
        f, held = todo.pop(), []
        for attr in ("__defaults__", "__closure__"):
            if isinstance(getattr(f, attr, None), tuple):
                held += getattr(f, attr)
        if isinstance(getattr(f, "__kwdefaults__", None), dict):
            held += f.__kwdefaults__.values()
        for g in held:
            if isinstance(g, types.CellType):
                try:
                    g = g.cell_contents
                except ValueError:             # an empty cell
                    continue
            if isinstance(getattr(g, "__code__", None), types.CodeType) and id(g) not in seen:
                seen.add(id(g))
                todo.append(g)
                yield g.__code__


loaded, runs, bodies = {}, set(), []   # runs: (file, first line) of the test function that each loaded test calls;
whole = set()                          # bodies: (test, the code of that function or None, what its class holds);
for t in tests_of(suite):              # whole: the modules skipped at their import (none of their code after it ran)
    method = getattr(t, "_testMethodName", "")
    module = origin.get(id(t)) or (method if type(t).__module__ == "unittest.loader" else tid(t).split(".")[0])
    loaded[module] = loaded.get(module, 0) + 1
    if type(t).__module__ == "unittest.loader" and type(t).__name__ == "ModuleSkipped":
        whole.add(module)
    code = held_fn = None
    for klass in type(t).__mro__:              # the one that runs: in the first class along the MRO that has it
        if method in vars(klass):
            held_fn = getattr(vars(klass)[method], "__func__", vars(klass)[method])
            code = getattr(unwrapped(held_fn), "__code__", None)
            if code is not None:
                runs.add(at(code))
            break
    bodies.append((t, code, held_fn))
    fn = getattr(type(t), method, None)
    for f in (fn, unwrapped(fn)):
        if inspect.isgeneratorfunction(f):
            bad.append(f"{tid(t)} is a generator: its body never runs")
        elif inspect.isasyncgenfunction(f):
            bad.append(f"{tid(t)} is an async generator: its body never runs")
        elif inspect.iscoroutinefunction(f) and not isinstance(t, unittest.IsolatedAsyncioTestCase):
            bad.append(f"{tid(t)} is async in a TestCase that does not await it: its body never runs")
        else:
            continue
        break
bad += [f"tests/{p.name} has no test" for p in sorted(pathlib.Path("tests").glob(PATTERN)) if not loaded.get(p.stem)]


class Result(unittest.TextTestResult):
    """Also the ids of the tests that started: a test skipped with its class (setUpClass) or module never starts."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started = set()

    def startTest(self, test):
        super().startTest(test)
        self.started.add(tid(test))
        profile.enable()                       # again, in case a test turned it off

    def getDescription(self, test):
        """unittest's, or the test's id where that fails (str() of a FunctionTestCase of functools.partial)."""
        try:
            return super().getDescription(test)
        except AttributeError:
            return tid(test)


returned, unknown = {}, set()   # test id: (the unit it ran in, the type of what the test returned) (a test returns
                                # None); the tests that turned the profiler off (sys.setprofile, a profiler of their
                                # own): what ran is not known
now = -1                        # the unit (below) that runs


def call(self, method):
    value = method()
    if sys.getprofile() is not profile:
        unknown.add(tid(self))
    if value is not None:
        returned[tid(self)] = (now, type(value).__name__)


if not hasattr(unittest.TestCase, "_callTestMethod"):
    bad.append("this Python's unittest has no TestCase._callTestMethod, through which the runner sees what a test returns")
unittest.TestCase._callTestMethod = call       # (IsolatedAsyncioTestCase has its own, which awaits the test)

# The tests run in JOBS workers (--jobs; one by default), processes forked from this one once the tests are loaded, so
# that each starts with what the loading ran.  A worker takes the next unit from a file that all of them read through
# one open file description (each read moves its offset on), runs it, and writes down what the checks below need; this
# process then merges that, prints unittest's report and runs the checks.  A unit is a test, or a suite other than a
# plain unittest.TestSuite (its own run() may do what a plain one does not); the plain suites of the loader are cut into
# their tests, which unittest's fixtures then see in the same order, so a worker sets a class or module up (setUpClass,
# setUpModule) and tears it down again for each run of its tests that it takes.  (Test hook: with TEST_JOBS_DEAL=1,
# worker k takes the units k, k + JOBS, k + 2 JOBS ... instead.)
# Signals reach the tests as when they ran in this process: the workers stay in the process group of scripts/test.sh,
# so that a signal to that group (a Ctrl-C, a hang-up, kill -- -PGID, deploy.sh's timeout) reaches them and what their
# tests start, and a Ctrl-Z stops them.  While they run, this process is in a group of its own, which such a signal does
# not reach; one sent to it alone (SIGINT, SIGTERM, SIGHUP) goes on to each worker.  So no worker gets a signal twice.
# Until this process has left the group the workers hold these signals, and one that it got by then goes on to each
# worker that does not hold it.  A worker runs the atexit handlers that its tests registered (the loading's run here,
# once) and ends as Python ends; after a KeyboardInterrupt, of SIGINT.  When a worker ends without its record (a test
# that ends the process, a signal) the run has no verdict, and when one died of a signal this process then dies of the
# same signal, also of SIGKILL (the OOM killer's), as the process of the tests did before, so that after a Ctrl-C bash
# stops too.  A worker gets SIGKILL when this process ends before it (PR_SET_PDEATHSIG).
jobs = int(os.environ["TEST_JOBS"])
units, spans = [], []           # spans: (first, end) of each unit's tests in bodies


def cut(s):
    for t in s:
        if type(t) is unittest.TestSuite:
            cut(t)
        else:
            first = spans[-1][1] if spans else 0
            units.append(t)
            spans.append((first, first + (len(list(tests_of(t))) if isinstance(t, unittest.TestSuite) else 1)))


cut(suite)
if [tid(t) for u in units for t in (tests_of(u) if isinstance(u, unittest.TestSuite) else [u])] != \
        [tid(t) for t, _, _ in bodies]:
    bad.append("the units the workers take are not the loaded tests, in their order")
LISTS = ("errors", "failures", "skipped", "expectedFailures", "unexpectedSuccesses")
QUEUES = [f"{sys.argv[1]}.units.{k}" for k in range(jobs)] if os.environ.get("TEST_JOBS_DEAL") == "1" else \
    [sys.argv[1] + ".units"]
for n, path in enumerate(QUEUES):
    pathlib.Path(path).write_bytes(b"".join(i.to_bytes(4, "big") for i in range(n, len(units), len(QUEUES))))
queues = [os.open(path, os.O_RDONLY) for path in QUEUES]
runner = unittest.TextTestRunner(stream=sys.stdout, verbosity=1, resultclass=Result)
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
parent = os.getpid()


def no_more_units():
    for q in queues:
        os.lseek(q, 0, os.SEEK_END)


class Queue(unittest.TestSuite):
    """A worker's units, as it takes them; marks: (unit, the length of each of the result's lists) as each starts."""
    def __init__(self, result, queue):
        super().__init__()
        self.result, self.queue, self.took, self.marks = result, queue, [], []

    def __iter__(self):
        global now
        while True:
            b = os.read(self.queue, 4)
            if len(b) != 4:
                return
            now = int.from_bytes(b, "big")
            self.took.append(now)
            self.marks.append((now, {name: len(getattr(self.result, name)) for name in LISTS}))
            yield units[now]

    def _removeTestAtIndex(self, index):
        pass


def record(k, result, q):
    """What the checks need of worker k's run, written to <verdict>.<k>."""
    ran = {e.code for e in profile.getstats()} | profile.entered
    ran = {c for c in ran if hasattr(c, "co_filename")}
    lists = {}
    for name in LISTS:
        out, m = [], -1
        for j, e in enumerate(getattr(result, name)):
            while m + 1 < len(q.marks) and q.marks[m + 1][1][name] <= j:
                m += 1
            t, text = (e, "") if name == "unexpectedSuccesses" else e
            desc = result.getDescription(t) if name in ("errors", "failures") else ""
            out.append([q.marks[m][0] if m >= 0 else -1, tid(t), isinstance(t, unittest.TestCase), desc, text])
        lists[name] = out
    own = []
    for u in q.took:
        for i in range(*spans[u]):
            t, _, fn = bodies[i]
            f = test_function(t)
            own.append([i, [where(c) for g in (fn, unwrapped(fn)) for c in inner(g)], [at(c) for c in inner(fn)],
                        where(f) if f is not None else None])
    pathlib.Path(f"{sys.argv[1]}.{k}").write_text(json.dumps({
        "took": q.took, "run": result.testsRun, "started": sorted(result.started), "lists": lists,
        "returned": [[u, t, kind] for t, (u, kind) in returned.items()], "unknown": sorted(unknown),
        "ran": [where(c) for c in ran], "own": own,
        "body_ran": [i for i, (_, code, _) in enumerate(bodies) if code is not None and code in ran]}))


def finish():
    """What Python does as it ends, before the end itself: it waits for the threads, runs the atexit handlers and
    flushes the streams."""
    for end in (threading._shutdown, atexit._run_exitfuncs):
        try:
            end()
        except BaseException:
            traceback.print_exc()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except BaseException:
            pass


def die(signum):
    """End of signum, as its default action does."""
    if signum not in (signal.SIGKILL, signal.SIGSTOP):      # (whose action cannot be changed)
        signal.signal(signum, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signum})
    os.kill(os.getpid(), signum)


def work(k, saved, mask, gate, report):
    """Worker k: it runs the units it takes and records its run; it ends as Python ends, but with os._exit, so that
    nothing of this process's exit (the loading's atexit handlers, buffers, the verdict) happens twice."""
    status, interrupted = 1, False
    try:
        try:
            atexit._clear()                     # the loading's handlers run in this process's parent
            if weakref.finalize._registered_with_atexit:     # and so do the loading's finalizers; this one's here
                for f in list(weakref.finalize._registry):
                    f.atexit = False
                atexit.register(weakref.finalize._exitfunc)
            for s, handler in saved.items():
                if handler is not None:         # (None: a handler that Python did not set, which stays)
                    signal.signal(s, handler)
            if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL) != 0:       # PR_SET_PDEATHSIG
                raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
            if os.getppid() != parent:
                raise RuntimeError("the runner ended while the worker started")
            while os.read(gate, 1):             # until the runner has left the group (the end of the pipe)
                pass
            os.write(report, f"{k} {' '.join(str(int(s)) for s in signal.sigpending())}\n".encode())
            os.close(report)
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)          # what it holds acts now
            result = runner._makeResult()
            unittest.registerResult(result)
            result.failfast, result.buffer, result.tb_locals = runner.failfast, runner.buffer, runner.tb_locals
            q = Queue(result, queues[k % len(queues)])
            with warnings.catch_warnings():             # as TextTestRunner.run()
                if runner.warnings:
                    warnings.simplefilter(runner.warnings)
                    if runner.warnings in ("default", "always"):
                        warnings.filterwarnings("module", category=DeprecationWarning,
                                                message=r"Please use assert\w+ instead.")
                result.startTestRun()
                try:
                    q(result)
                finally:
                    result.stopTestRun()
            profile.disable()
            record(k, result, q)
            status = 0
        except KeyboardInterrupt:
            traceback.print_exc()
            interrupted = True
        except BaseException:
            traceback.print_exc()
        finish()
        if interrupted:
            die(signal.SIGINT)
    finally:
        os._exit(status)


saved = {s: signal.getsignal(s) for s in SIGNALS}
home = os.getpgrp()             # scripts/test.sh's group
alive, came = {}, []            # pid: worker; the signals that went on to the workers


def forward(signum, frame):
    """A signal sent to this process alone, while it is out of the group: to each worker; when none runs any more, it
    acts as it would have without the workers."""
    if not alive:
        signal.signal(signum, saved[signum])
        signal.raise_signal(signum)
        return
    came.append(signum)
    for pid in list(alive):
        try:
            os.kill(pid, signum)
        except OSError:
            pass


sys.stdout.flush()
sys.stderr.flush()
start = time.perf_counter()
mask = signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
gate, gate_w = os.pipe()
report_r, report = os.pipe()
for k in range(jobs):
    pid = os.fork()
    if pid == 0:
        try:
            os.close(gate_w)
            os.close(report_r)
            work(k, saved, mask, gate, report)
        finally:
            os._exit(1)
    alive[pid] = k
os.close(gate)
os.close(report)
os.setpgid(0, 0)                # out of the group: what is sent to the group now reaches the workers only
os.close(gate_w)
pending = {}                    # worker: the signals it holds, once this process is out of the group
with os.fdopen(report_r, "rb") as f:
    for line in f:
        k, *signums = line.split()
        pending[int(k)] = {int(s) for s in signums}
mine = []                       # what this process got until it left the group, to this process or to the group
while (info := signal.sigtimedwait(SIGNALS, 0)) is not None:
    mine.append(info.si_signo)
for s in mine:
    if saved[s] is not signal.SIG_IGN:
        came.append(s)
        for pid, k in alive.items():
            if s not in pending.get(k, ()):
                try:
                    os.kill(pid, s)
                except OSError:
                    pass
for s, handler in saved.items():
    if handler is not signal.SIG_IGN and handler is not None:   # what is ignored stays so, in the workers too
        signal.signal(s, forward)
signal.pthread_sigmask(signal.SIG_SETMASK, mask)
profile.disable()
records, lost, died = {}, [], []
while alive:
    try:
        pid, status = os.wait()
    except ChildProcessError:
        break
    k = alive.pop(pid, None)
    if k is None:
        continue
    try:
        records[k] = json.loads(pathlib.Path(f"{sys.argv[1]}.{k}").read_text())
    except (OSError, ValueError):
        pass
    if status != 0 or k not in records:
        lost.append(f"worker {k + 1} of {jobs} ended with {os.waitstatus_to_exitcode(status)}" +
                    ("" if k in records else ", without its record"))
        if os.WIFSIGNALED(status):
            died.append(os.WTERMSIG(status))
        no_more_units()
taken = time.perf_counter() - start
for s, handler in saved.items():                # a signal from now on acts as it did before the workers
    if handler is not None:
        signal.signal(s, handler)
try:
    os.setpgid(0, home)                         # back, before this process writes again
except OSError:                                 # (the group has ended: scripts/test.sh and the workers)
    pass
if lost:
    try:
        sys.stdout.flush()
        for why in [f"stopped by {signal.Signals(s).name}" for s in dict.fromkeys(came)] + lost:
            print(f"FAIL: the unit tests did not finish (no verdict): {why}")
        sys.stdout.flush()
    except OSError:                             # (a terminal that has hung up)
        pass
    if died:
        if died[0] == signal.SIGINT:            # as Python ends at a KeyboardInterrupt
            finish()
        die(died[0])
    sys.exit(1)                                 # no verdict
took = sorted(u for r in records.values() for u in r["took"])
if took != list(range(len(units))):
    bad.append(f"the workers did not run each unit once: {len(took)} runs of {len(set(took))} of {len(units)} units")
# the result of the run: each list in the order of the units (one worker's entries for a unit, in their order)
lists = {name: sorted((e for k in sorted(records) for e in records[k]["lists"][name]), key=lambda e: e[0])
         for name in LISTS}
started = {t for r in records.values() for t in r["started"]}
unknown = {t for r in records.values() for t in r["unknown"]}
returned = {t: kind for _, t, kind in sorted((e for k in sorted(records) for e in records[k]["returned"]),
                                             key=lambda e: e[0])}
ran = {tuple(w) for r in records.values() for w in r["ran"]}      # where() of the code that ran
body_ran = {i for r in records.values() for i in r["body_ran"]}
view = {i: (h, a, f) for r in records.values() for i, h, a, f in r["own"]}   # as the worker that ran its test saw it
for i, (t, _, fn) in enumerate(bodies):         # (a test that no worker ran, when the check above fails)
    if i not in view:
        f = test_function(t)
        view[i] = ([where(c) for g in (fn, unwrapped(fn)) for c in inner(g)], [at(c) for c in inner(fn)],
                   where(f) if f is not None else None)
out = unittest.runner._WritelnDecorator(sys.stdout)       # unittest's report, as TextTestRunner.run() prints it
out.writeln()
for flavour, name in (("ERROR", "errors"), ("FAIL", "failures")):
    for _, _, _, desc, text in lists[name]:
        out.writeln(unittest.TextTestResult.separator1)
        out.writeln(f"{flavour}: {desc}")
        out.writeln(unittest.TextTestResult.separator2)
        out.writeln(text)
out.writeln(unittest.TextTestResult.separator2)
run = sum(r["run"] for r in records.values())
out.writeln("Ran %d test%s in %.3fs" % (run, run != 1 and "s" or "", taken))
out.writeln()
success = not (lists["failures"] or lists["errors"] or lists["unexpectedSuccesses"])
infos = [] if success else [f"{what}={len(lists[name])}" for what, name in (("failures", "failures"),
                                                                             ("errors", "errors")) if lists[name]]
infos += [f"{what}={len(lists[name])}" for what, name in (("skipped", "skipped"), ("expected failures",
          "expectedFailures"), ("unexpected successes", "unexpectedSuccesses")) if lists[name]]
out.write("OK" if success else "FAILED")
out.writeln(f" ({', '.join(infos)})" if infos else "")
out.flush()
# where the code of the defs that ran is: not a module's, a lambda's or a comprehension's (<module>, <lambda> ...),
# which may be on the line where a def starts (line 1, a decorator)
ran_at = {(p, line) for p, line, name in ran if not name.startswith("<")}
# a helper: a function that code that may run reads, transitively: code that ran, the function of a loaded test (also
# one that was skipped), and a function that a loaded test holds (in its closure or as a default), and what those read
holds = {tuple(w) for h, _, _ in view.values() for w in h}
tests = {tuple(f) for _, _, f in view.values() if f is not None}     # FunctionTestCases'
reach = ran | holds | tests | {where(c) for c in (code for _, code, _ in bodies) if c is not None}


def lineage(rp, c):
    """The class c of the module rp and the classes of the module it derives from (by their names)."""
    out, todo = set(), [c]
    while todo:
        k = todo.pop()
        if k not in out and k in classes.get(rp, {}):
            out.add(k)
            todo += classes[rp][k]
    return out


def owners(rp, fn, owner):
    """The classes of the module whose method NAME a function's owner.NAME may be: for self.NAME (cls., super().), its
    own class, the classes it derives from and those that derive from it; for CLASS.NAME, CLASS and the classes it
    derives from (Base.check(self)); none for another name, or self.NAME outside a class."""
    if owner == "self":
        return set() if fn.method is None else \
            lineage(rp, fn.method) | {k for k in classes.get(rp, {}) if fn.method in lineage(rp, k)}
    return lineage(rp, owner)


helps = {}                              # per module: the names that such code reads
for rp, group in fns.items():
    live = [fn for fn in group if (rp,) + fn.key() in reach]
    bound, methods = {}, {}             # the module's functions by the name it binds them to; its methods by
                                        # (class, name)
    for fn in group:
        if fn.binding:
            bound.setdefault(fn.binding, []).append(fn)
        if fn.method:
            methods.setdefault((fn.method, fn.name), []).append(fn)
    seen, names = {id(fn) for fn in live}, set()
    while live:
        fn = live.pop()
        for g in [g for name in fn.reads - names for g in bound.get(name, ())] + \
                [g for owner, attr in fn.attrs for c in owners(rp, fn, owner) for g in methods.get((c, attr), ())]:
            if id(g) not in seen:
                seen.add(id(g))
                live.append(g)
        names |= fn.reads
    helps[rp] = names
# a test runs when it starts, and its body runs then, unless it was skipped or failed first; a class or module whose
# setUpClass or setUpModule raised (skipped or an error) starts none of its tests (lists: [unit, id, whether it is a
# TestCase, description, text]; what is not a TestCase stands for a setUpClass or setUpModule)
held = {t for _, t, case, _, _ in lists["skipped"] + lists["errors"] if not case}
done = {e[1] for name in ("skipped", "errors", "failures", "expectedFailures") for e in lists[name]} | unknown
# a test function runs when a loaded test calls it or when it ran (behind a decorator without functools.wraps, a helper
# named test...); one that a decorator without functools.wraps holds counts as called when its test did not run its body
known = runs | ran_at
spared = known | {tuple(a) for i, (t, _, _) in enumerate(bodies) if tid(t) not in started or tid(t) in done
                  for a in view[i][1]}
bad += [f"{p}:{line}: {c}.{f} never runs: no loaded test calls it (its class is no TestCase the runner loads nor a base "
        "of one, the name is bound to something else later, or a decorator replaced it)"
        for p, c, f, line, first in defined
        if loaded.get(p.stem) and p.stem not in whole and (os.path.realpath(p), first) not in spared]
for p, name, line, first in free:      # (in a module that turned the profiler off, code of its top may have called
    rp = os.path.realpath(p)           # a function unseen: one that its code names anywhere is not checked)
    if p.stem not in whole and (rp, first) not in known and (rp, first, name) not in holds | tests and \
            name not in helps.get(rp, ()) and not (p.stem in dark and name in named.get(rp, ())):
        bad.append(f"{p}:{line}: {name} is not in a TestCase, so it never runs")
for i, (t, code, _) in enumerate(bodies):
    k = type(t)
    if tid(t) not in started:
        if not {f"setUpClass ({k.__module__}.{k.__qualname__})", f"setUpModule ({k.__module__})"} & held:
            bad.append(f"{tid(t)} never started (its class overrides run(), or something else kept the runner from it)")
    elif code is not None and i not in body_ran and tid(t) not in done:
        bad.append(f"{tid(t)} started, but its body never ran (a decorator that returns without calling it?)")
bad += [f"{t} returns a value ({kind}), which the runner drops: a test returns None (a coroutine it returns never runs)"
        for t, kind in returned.items()]
allowed = (() if harnesses else ("harness:",)) + (("machine:",) if repo_only else ())
for prefix in ("harness:", "machine:"):
    n = sum(1 for e in lists["skipped"] if e[4].startswith(prefix))
    if n:
        print(f"skipped ({prefix[:-1]}): {n}")
bad += [f"{t} was skipped: {why}" for _, t, _, _, why in lists["skipped"] if not why.startswith(allowed)]
bad += [f"{e[1]} expects to fail (expectedFailure): its failure passes the run" for e in lists["expectedFailures"]]
# a test ran when it started and was not skipped as a whole (a subtest's skip has an id of its own)
if not started - {e[1] for e in lists["skipped"]}:
    bad.append("no test ran" + (" (every one was skipped)" if lists["skipped"] else ""))
for b in bad:
    print(f"FAIL: {b}")
ok = success and not bad
pathlib.Path(sys.argv[1]).write_text("pass\n" if ok else "fail\n")
sys.exit(0 if ok else 1)
EOF
[ "$(cat "$tmp/verdict" 2>/dev/null)" = pass ] || { [ $fail = 1 ] || echo "FAIL: the unit tests did not finish (no verdict)"; fail=1; }
if ! left=$(ls -A -b -- "$tmp/tests"); then
  echo "FAIL: cannot read the tests' TMPDIR"; fail=1
elif [ -n "$left" ]; then
  echo "FAIL: the tests left this in their TMPDIR: $(head -n 20 <<< "$left" | tr '\n' ' ')"; fail=1
fi

step "shell syntax (bash -n; it does not parse \$( ) and \$(( )) bodies, so the tests run the scripts)"
sh_files=(); for f in scripts/*.sh bench/*.sh tests/*.sh; do   # not a glob that matches nothing; not a FIFO or a
  [ -e "$f" ] || [ -L "$f" ] || continue                        # device (the next step names it, unread)
  [ -e "$f" ] && [ ! -f "$f" ] && [ ! -d "$f" ] || sh_files+=("$f")
done
# a script that the steps here do not read, anywhere in the repo but results/ (raw measurements) and dot directories:
# in another directory, or a program without .sh or .py (by the interpreter its #! line names: its first word, or
# with env the first word after env's options and NAME=VALUE words; .bash is a shell script too); and a file that is
# not a regular one (a FIFO, a socket, a device), which no step opens.  Not seen: shell code in a file with neither (a
# library that a script sources) and in the workflow's YAML
python3 - "${sh_files[@]}" <<'EOF' || fail=1
import os, stat, sys
for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(errors="backslashreplace")        # a file name that is not UTF-8 prints, as \udcxx
read, bad = set(sys.argv[1:]), 0


def interpreter(head):
    """The program a #! line runs (its basename), or b"" for a file without one."""
    if not head.startswith(b"#!"):
        return b""
    words = head[2:].split()
    prog, rest = (os.path.basename(words[0]), words[1:]) if words else (b"", [])
    if prog == b"env":
        prog = b""
        while rest:
            w, rest = rest[0], rest[1:]
            if w in (b"-u", b"--unset", b"-C", b"--chdir"):
                rest = rest[1:]                       # and its value
            elif w.startswith(b"-S") and len(w) > 2:
                rest = [w[2:]] + rest                 # -Sbash -e: the string's words
            elif w.startswith(b"--split-string="):
                rest = [w.split(b"=", 1)[1]] + rest
            elif not (w.startswith(b"-") or b"=" in w):
                prog = os.path.basename(w)
                break
    return prog


def unreadable(e):
    global bad
    print(f"cannot read {e.filename}: {e.strerror}"); bad = 1


for d, dirs, files in os.walk(".", onerror=unreadable):
    dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x != "__pycache__" and (d, x) != (".", "results"))
    for f in sorted(files):
        p = os.path.join(d, f)[2:]
        try:
            if not stat.S_ISREG(os.stat(p).st_mode):     # a FIFO would block the read, a device not end it
                print(f"{p}: not a regular file (a FIFO, a socket or a device), which the steps do not read"); bad = 1
                continue
            with open(p, "rb") as fh:
                head = fh.readline(4096)                 # (the kernel reads 256 bytes of a #! line)
        except OSError as e:
            unreadable(e)
            continue
        prog = interpreter(head)
        python = b"python" in prog
        shell = p.endswith((".sh", ".bash")) or prog in (b"sh", b"bash", b"dash")
        if shell and p not in read:
            print(f"{p}: a shell script that bash -n, shellcheck and the Python step do not read (they read "
                  "scripts/*.sh, bench/*.sh and tests/*.sh)"); bad = 1
        elif python and not p.endswith(".py"):
            print(f"{p}: Python without .py, which the Python step does not compile"); bad = 1
sys.exit(bad)
EOF
for f in "${sh_files[@]}"; do bash -n "$f" || { echo "syntax error: $f"; fail=1; }; done
echo "checked ${#sh_files[@]} files"
SHELLCHECK=${TEST_SHELLCHECK:-shellcheck}         # (test hook: another command, or one that is not there)
if command -v "$SHELLCHECK" > /dev/null; then
  "$SHELLCHECK" -S error "${sh_files[@]}" && echo "shellcheck: no errors" || fail=1
elif [ $REPO_ONLY = 1 ]; then
  echo "shellcheck is not installed (CI installs it)"; fail=1
else
  echo "shellcheck is not installed here: CI runs it"
fi

step "Python (compiled, as the interpreter would: the repo's files and the Python its shell scripts run)"
python3 - "${sh_files[@]}" <<'EOF' || fail=1
import bisect, functools, os, re, stat, sys
for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(errors="backslashreplace")        # a lone surrogate ($'\ud800') prints, as \ud800
bad = n = 0
PH = "\ue000"       # in shell code that a word gives a shell (bash -c "...$x..."), an expansion of that word
TOP = os.path.realpath(".")     # the repository's top
compiled, shelled = {}, {}      # per script: what was compiled and read as shell (once)
read_once, current = set(), ""  # per script: (a shell's scope, a file of the repository it sources), read once; the
                                # script
written, unread = {}, {}        # per script: the files it wrote (name: text, where, key), and from what this check
                                # cannot read (name: where)
printed_once = {}               # per script: id() of a command's words: (those words, what printed() gave)
ssh_reads = {}                  # per script: the text of each of ssh's commands: what reads_input() found
pending, raws = {}, {}          # per script: what >> added to a file it wrote, until it is read (see wrote()); the
                                # texts of each file it wrote, as written (an unquoted heredoc's expansions as they are)
calls = 0           # per script: the calls of its functions followed, with the commands and words read for them


class Stop(Exception):
    """Ends the reading of a script."""


def check(src, where, key=None):
    """Compile src as the interpreter would (in text, each expansion stands for a name); key: its source in a script."""
    global bad, n
    if key is not None:
        if id(key) in compiled:
            return
        compiled[id(key)] = key
    n += 1
    try:
        compile(src.replace(PH, "X") if isinstance(src, str) else src, where, "exec", dont_inherit=True)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as e:
        print(f"syntax error: {where}: {e.__class__.__name__ if isinstance(e, (RecursionError, MemoryError)) else e}")
        bad = 1
def cannot(where, why):
    global bad
    print(f"cannot read: {where}: {why.replace(PH, '$...')}"); bad = 1

def unreadable(e):
    global bad
    print(f"cannot read {e.filename}: {e.strerror}"); bad = 1
for top, dirs, names in os.walk(".", onerror=unreadable):
    dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__pycache__")
    for name in sorted(names):
        if name.endswith(".py"):
            p = os.path.join(top, name)[2:]
            try:
                if not stat.S_ISREG(os.stat(p).st_mode):     # a FIFO would block the read
                    cannot(p, "it is not a regular file (a FIFO, a socket or a device)")
                    continue
                with open(p, "rb") as f:
                    check(f.read(), p)
            except OSError as e:
                unreadable(e)
# Any other place where python runs, with a program this check cannot read, fails: give it in a form read here.
# The Python a shell script runs is found by reading the script as bash does, as far as that goes:
# - its words (quotes, escapes, $'...', $"...", $( ), ` `, ${ }, $(( )), extglob groups), operators, redirections,
#   comments, heredocs (a body without a quoted delimiter is expanded, so its $( ) and ` ` run too), case patterns,
#   and the commands of subshells (also after time [-p] and coproc [NAME]), { } groups (their redirections are their
#   commands'), pipelines, functions, and command and process substitutions, also inside [[ ]], (( )) and $(( ))
#   ("$( (" and "(( (" whose first ")" that closes nothing is not followed by another are a subshell, as bash reads
#   them);
# - python's program: -c CODE; a heredoc with a quoted delimiter or a here-string without an expansion on the file
#   descriptor it reads ("-", /dev/stdin, /dev/fd/N; the last redirection of it counts, but < /dev/stdin, which gives it
#   what it had), also through a pipe from cat; and a file the script wrote before, or one of the repository by a
#   literal path from its top (< tools/x), compiled (a .py one with the others), or /dev/null.  python is a word python,
#   python2, python3 or python3.N, pypy, pypy3, ipython or ipython3, by a path, through a variable named like PY
#   (whatever the script sets it to) or a default ${X:-python3}, or an array of the script's (a=(python3 -I);
#   "${a[@]}"), after keywords, assignments, a variable, and the wrappers of WRAPPERS (env, nice, timeout, sudo, runuser
#   -u, setsid, coproc ...; sudo -s or -i, doas -s and runuser -u USER with no command run a shell, which reads its
#   standard input).  A variable that the script sets once, at its top level (not in a function, an if, a case or a
#   loop, also after time or !, nor after && or || or in a pipeline), alone, to a word without blanks (JQ=jq; not a name
#   reference, declare -n J=K), and names nowhere else but as $JQ or ${JQ}, is that word as a program, also in the code
#   that the script gives a shell (bash -c "$SUDO make" after SUDO=sudo is sudo make), until code that source or trap
#   runs in this shell names it;
# - shell code, read in turn: of sh, bash, dash, zsh, ksh, mksh or ash -c (or a shell's heredoc or here-string, or a
#   file the script wrote that it runs), su -c, runuser -c and script -c (the option anywhere among their words), flock
#   -c (also --command=), env -S, trap, watch, find -exec, sg ([-c] COMMAND, or a shell that reads its standard input),
#   and of source or . with a file the script wrote, a stdin path, or a file of the repository by a literal path from
#   the script's directory or from the top (read once in each shell, in that shell: its functions are that shell's; one
#   that the script sources at its top, not in a function nor after && or ||, is read before the script's commands, so
#   that its functions are known to the bodies of the script's own); a command of sh -c CODE ARG0 ARGS that holds "$@",
#   $*, or $0 to $9, is read with those words in their place; in code an expansion of the word that gave it is not
#   known: a command that is only such an expansion, or starts with one, unquoted, before more words (bash -c "$cmd -v";
#   an array's words are no command), and a program, python -c code or a heredoc that is only one, cannot be read (the
#   shell reads what it holds as code);
# - a file that cat or tee writes from a heredoc, a here-string or a pipe from cat, or printf (a literal format and no
#   argument) or echo (literal words) writes, with >> and tee -a after what the script wrote there before: Python when
#   its first line names python or the file's name ends in .py, shell code when its first line names a shell, each
#   text once it is whole (when it runs, is overwritten, or at the end of the script); a later python or shell that
#   runs that file (by the same name; ./ aside) reads it then;
# - a function of the script: its body is a { } group or a ( ) subshell, read where it stands, but a command of it that
#   holds the function's arguments ("$@", "${@}", "${@:N}", $*, and $1 to $9, also ${1:-x} and ${1:?}, when every shift
#   of the body is counted, as below), or that reads a standard input that the body does not give it (no redirection of
#   it but < /dev/stdin, and no pipe into it) as python's or a shell's program (also su's, runuser's or sg's without
#   -c), with a program that may run it (ssh, mosh, parallel, pdsh, clush, at, batch; sudo -s or -i and doas -s with no
#   command; xargs with python or a shell among its words) or in a call of a function whose body does, is read at each
#   call instead, with the call's words in its place (less the words that the shifts before it drop: shift or shift N
#   alone at the body's top level, before any if, while, until, for, case or select there and not after && or || nor in
#   a pipeline; a later shift is not followed, so the words it drops are read too), and the call's redirections and the
#   pipe into it are its own too (a function that is never called runs none of them; a call of it in another function's
#   body that reads the call's input is read at each call of that body, with that call's input);
# - any other program is one this check does not follow (ssh, at, batch, docker, a terminal, the interpreter of another
#   language ...): it may run what it is given, which this check does not read.  A word given to it that names python
#   (as \b(i?python|pypy) finds it: python3, /usr/bin/python3, "python3 x.py" in one word, a message "python3 failed",
#   also in a command substitution in it: "$(command -v python3)"; not a variable's name: "$ipython_dir"), an expansion
#   in its words or in its input, as written, of a variable that is python's (named for python: a part of its name
#   between "_" starts with py, as PY, PYBIN, PYEXE, VENV_PY, but Python's own variables, as PYTHONPATH; or one that an
#   assignment of the script sets, anywhere and however often, to a word that names python other than as the program
#   that a command substitution in it runs (INTERP=$(command -v python3), RUN="$VENV/bin/python3 -u", local b=python3;
#   not PORT=$(python3 -c ...) nor N=$(curl ... | timeout 5 python3 -c ...), python's output, where python is only the
#   program of the substitution's commands; also : "${INTERP:=python3}"; and in the code of a new shell that the script
#   starts, which may get it exported: export INTERP; bash -c '... "$INTERP" ...'); or an array that holds such a word
#   (RUN=(python3 -u), RUN+=(...), local -a): "${RUN[@]}"), a standard input that the script gives it and that names
#   python (a heredoc, a here-string, a pipe from echo or printf of literal text, also through cat), and a file the
#   script wrote that names python, given to a program that may run it or send it on (one of CMDLINE below, scp, rsync,
#   sftp, or one with a shell among its words), are refused; so is an expansion ($X, $1, "$@", $( ) ...) among the words
#   of a program that runs a word or its standard input as a command line (CMDLINE: ssh, mosh, tmux, screen, byobu,
#   parallel, xterm, at, batch, pdsh, clush, ansible, entr, and csh, tcsh and fish, whose code this check does not
#   read), but as the value of an option of ssh other than -o and -F (ssh -i "$KEY" host), or in the input that the
#   script gives one as text, unless it reads none (ssh -n); one after such a program's name or after a shell that runs
#   code (-c) among the words of another program (sshpass -e ssh host "$CMD", docker run img bash -c "$CMD": such a
#   program counts there by its name or an absolute path, with its own rules, as ssh's options, but not as the value of
#   an option that names a program as data: rsync -e ssh, useradd, usermod, chsh and getopt -s /bin/bash, journalctl -u
#   ssh, also as the last of a group of short options: rsync -avze ssh, journalctl -xeu ssh; after a shell, an expansion
#   counts from its code on: docker exec ctr bash -c make "$X" is refused, docker exec -i ctr bash -s -- "$X" is not);
#   and an input that this check does not see (a program's output, a process substitution, a file that the script did
#   not write and that is not one of the repository), or that holds an expansion, given to a program that runs its
#   standard input as code: ssh with no command or with one that runs a shell or python that reads it (ssh host bash -s;
#   sudo -s or -i and doas -s with no command; a shell behind a pipe from cat or tee: cat | bash; xargs with a shell or
#   python among its words: ssh's command is read as OpenSSH reads its words, and -N, -n, -f, -s, -W, -O, -G, -V and -Q
#   run none), mosh, pdsh, clush, csh, tcsh and fish with -s or no operand and no -c, and a shell among another
#   program's words with -s, "-" or no operand (docker exec -i ctr bash; one that is docker's --entrypoint runs code
#   when a later word is an option with c, and an expansion counts from the word after that option on (docker run
#   --entrypoint bash "$IMG" -c make passes), and else may read its standard input; a shell there with an operand runs a
#   file: strace -f bash "$script", and a process substitution there is refused); and so are at and batch unless this
#   check sees their job (a heredoc, a here-string, a pipe from echo or printf of literal text, -f with a file the
#   script wrote or one of the repository), but with -l, -d, -r, -c or -V, which run none.  Not refused: the words of
#   echo, printf, logger and the other programs that only print or match text (grep, sed, awk, ls, test, declare, read,
#   hash ... and chmod), of a call of a function of the script (its body is read at the call, with the call's words),
#   and of a for, a select, a case, [[ ]], (( )) or an array, which are data (an array runs nothing, but python's -c
#   code or file in it is compiled).  So a message or an argument that names python fails when a program this check does
#   not follow is given it (mail -s "python3 failed", journalctl -g python3, perl -e 's/python3/x/', make
#   PYTHON=python3, killall python3): reword it, or give it to echo, printf or logger (also through a function: its
#   words are read at its calls); so does a variable named so (py_files, PYTORCH_CUDA_ALLOC_CONF); an expansion given to
#   ssh or tmux (ssh "$HOST" uptime, tmux new-window "$1"), or after the name of such a program that another program is
#   given as data (systemctl restart ssh "$UNIT", mv parallel "$d", gzip -c at "$x", mkdir -p batch "$OUT"); an input
#   with an expansion given to ssh with a command that only stores it (ssh host 'cat > f' <<EOF with $X in it); and an
#   input from a program's output given to ssh, or to a shell behind another program (./make_job.sh | ssh host).
# It cannot read (and so refuses): eval; aliases (shopt -s expand_aliases); python's or a shell's code from a variable
# or a command; a program from a pipe or the terminal, an unquoted heredoc, an expanded here-string, a process
# substitution, a copy of another file descriptor, or an array as python's program, and as its standard input a file
# that is neither one the script wrote nor one of the repository by a literal path (python3 < /etc/job.py); the script's
# arguments as the program at a script's top ("$@" -c ...); xargs's -I string in the code of the python -c or sh -c that
# it runs; a process substitution as the code of a shell or source; a variable as the program with -c, -m or - among its
# options; a .py file that printf or echo writes from expansions, arguments or directives, and another such file that a
# shell, source or python runs; what a program this check does not follow is given, as above; a call, in a function's
# body above the line, of a function of a file that the script sources in a function or after && or || ([ -f lib.sh ] &&
# . ./lib.sh), which is a program this check does not follow there; a function body that is no { } or ( ); more than
# 20000 of the calls of a script's functions, the commands and words read for them, the lines of files written in parts
# read again, and each 80 characters of sh -c code read again with its arguments, of watch's code read again at a call
# and of each new text of ssh's commands, and the words of the script's arrays put in place of each of their expansions;
# what this lexer cannot follow (a quote, parenthesis, brace or heredoc with no end, a heredoc in $( ) or <( ) whose ")"
# comes before its body, a ")" with no "(", nesting deeper than Python's recursion); a script that is not UTF-8, starts
# with a byte order mark, or holds U+E000, which this check uses itself (also from $'\ue000'), and a file of the
# repository that source reads and that holds U+E000.
# It does not see what code review has to: a function that is defined only after a command of its name (that command
# runs the program); a variable as the program without -c, -m or -, and one used before the assignment that makes it a
# word, or set again by a file that the script sources and this check does not read (one the script did not write, nor
# one of the repository by a literal path); a shift in a subshell, or by more words than the call has (the words it
# would drop are dropped all the same); an expansion of the word that gave shell code inside a word or after the program
# (bash -c "echo $x" with x='; python3 ...'; bash -c "strace -f $PYBIN -c ..." and su app -c "numactl -N0 $PYTHON ...",
# though the variable is python's); python that a program this check does not follow runs from what this check does not
# see there: a variable whose name is not python's and that no assignment of the script sets to a word that names python
# (docker run img "$X" -c ..., "$VENVPY", read -ra RUN <<< "python3 -u", local b=$1 in a function called with python3),
# a positional parameter of a function whose shifts this check does not count, the input of a call that the body gives a
# program not read at the call (f() { mail -s x root; }; f <<'EOF'), what ssh's command writes from an input without an
# expansion and runs later (./make_job.sh | ssh host 'cat > f && bash f'), or what the program fetches; a program in a
# file that python or a shell names and that the script did not write (python3 /etc/job.py, bash "$JOB", bash <
# /etc/job.sh; the repository's .py and .sh files are read on their own); what awk's system() or sed's e command runs; a
# function that runs other than by its call (export -f f; bash -c f); a line of a .py file that an unquoted heredoc
# writes and that is only an expansion (it compiles as a name); a file that the script copies from another (cat FILE >
# x.py) or writes from a program's output, also over a text it wrote before, and then runs.
def ansi_c(s):
    """The text of $'...' (its inside s), as bash decodes it."""
    simple = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
              "\\": "\\", "'": "'", '"': '"', "?": "?"}
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c != "\\" or i + 1 >= len(s):
            out.append(c); i += 1; continue
        e = s[i + 1]
        if e in simple:
            out.append(simple[e]); i += 2
        elif e in "01234567":
            m = re.match(r"[0-7]{1,3}", s[i + 1:])
            out.append(chr(int(m.group(0), 8) & 0xFF)); i += 1 + len(m.group(0))
        elif e == "x" and re.match(r"[0-9A-Fa-f]", s[i + 2:i + 3]):
            m = re.match(r"[0-9A-Fa-f]{1,2}", s[i + 2:])
            out.append(chr(int(m.group(0), 16))); i += 2 + len(m.group(0))
        elif e in "uU" and re.match(r"[0-9A-Fa-f]", s[i + 2:i + 3]):
            m = re.match(r"[0-9A-Fa-f]{1,%d}" % (4 if e == "u" else 8), s[i + 2:])
            out.append(chr(min(int(m.group(0), 16), 0x10FFFF))); i += 2 + len(m.group(0))
        elif e == "c" and i + 2 < len(s):
            out.append(chr(ord(s[i + 2]) & 0x1F)); i += 3
        else:
            out.append(c + e); i += 2
    return "".join(out)
class Word:
    def __init__(self, line):
        self.text, self.code, self.lit, self.line = [], [], [], line    # expansions as written / as PH / literal
        self.names = []                 # what each PH of the code stands for: the expansion as written, or None
        self.literal = True             # no expansion and no substitution in it
        self.subst = self.procsub = self.quoted = self.array = False
        self.key = self                 # the word it comes from (an option's value cut from it): its code is read once
        self.nhead, self.open = 0, True     # the length of its text up to its first quote or expansion (assigns())
        self.pyword = self.bname = self.nname = None    # names_python(), its base name and its normal name (cached)
        self.subs = []                  # the text of each command substitution in it, as written
    def done(self):
        self.text, self.code = "".join(self.text), "".join(self.code)
        if PH in self.text:             # an expansion of the word that gave this shell code
            self.literal = False
        self.only_expansion = not self.literal and not "".join(self.lit).replace(PH, "").strip()
        return self
    def add(self, s, literal=True, code=None):
        self.open = self.open and literal and not self.quoted
        self.nhead += len(s) if self.open else 0
        self.text.append(s)
        self.code.append(s if code is None else code)
        if PH in self.code[-1]:
            self.names += [s if code == PH else None] * self.code[-1].count(PH)
        if literal:
            self.lit.append(s)
def tail(w, k):
    """The word w without its first k characters (an option it starts with: -cCODE, -SSTRING, --command=CODE)."""
    v = Word(w.line)
    v.text, v.code, v.literal, v.subst, v.quoted, v.key = w.text[k:], w.code[k:], w.literal, w.subst, w.quoted, w.key
    v.names = w.names[w.code[:k].count(PH):]
    v.only_expansion = not v.literal and not v.code.replace(PH, "").strip()
    return v
def resolved(w, scope):
    """The code of w, shell code that a shell runs, with each expansion of a variable that the shell that gives it sets
    once ($X or ${X}: see values()) as its value: bash -c "$SUDO make" after SUDO=sudo is sudo make."""
    parts, out = w.code.split(PH), []
    if scope is None or len(parts) != len(w.names) + 1:
        return w.code
    for part, name in zip(parts, w.names):
        m = VALUE.fullmatch(name or "")
        out += [part, scope.values.get(m.group(1) or m.group(2), PH) if m else PH]
    return "".join(out + parts[-1:])
class Command:
    def __init__(self, line):
        self.words, self.heredocs, self.redirs, self.line = [], [], [], line    # redirs: (op, word or heredoc, fd)
        self.array = False              # an array's words (a=(...)): no command of their own
        self.pipe_from = None           # the command whose output this one reads (... | this)
        self.group = None               # after a { } or ( ): the commands its redirections apply to
        self.in_body = False            # in a function's body
        self.at_call = False            # read at each call of the function instead (see prepare())
        self.depth = 0                  # how deep in ( ), { }, $( ) ... it is
        self.unsure = False             # it may not run in this shell: after && or ||, or in a pipeline or background
class Heredoc:
    def __init__(self, delim, quoted, strip, line, fd):
        self.delim, self.quoted, self.strip, self.line, self.fd = delim, quoted, strip, line, fd
        self.body = self.code = ""
        self.body_line = line
        self.given = None               # the text that a program reads from it (see text_in()), once
class Scope:
    """What one shell knows of itself: its functions (name: the Body of each definition), its literal arrays (name:
    words), the variables it sets once to a literal word (values, name: the word; not those named like PY, which are
    python whatever they are set to), and those that an assignment or an array sets to a word that names python
    (pynames); pyx: what python_expansion() found in each text, and file_says() for each file."""
    def __init__(self):
        self.functions, self.arrays, self.values, self.pynames, self.pyx = {}, {}, {}, set(), {}
class Body:
    """A function's body: its commands, and those of them read at each call (with the words that the body's shifts drop
    before each), from prepare(); feeds: whether one of those reads the call's input; positional: whether $1 to $9
    are read at a call (every shift of the body is counted)."""
    def __init__(self, commands):
        self.commands, self.at_calls, self.drops, self.feeds, self.positional = commands, None, {}, False, False
class Lexer:
    OPERATORS = ("&&", "||", ";;&", ";;", ";&", "|&", "|", "&", ";")
    REDIR = re.compile(r"(\d+|&)?(<<<|<<-|<<|<>|>>|>&|<&|>\||<|>)")
    NAME = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-])")
    def __init__(self, text, where, line=1, commands=None, scope=None):
        self.t, self.i, self.where, self.base = text, 0, where, line
        self.starts = [0] + [m.end() for m in re.finditer(r"\n", text)]
        self.commands = [] if commands is None else commands
        self.scope = Scope() if scope is None else scope
        self.pending = []
        self.depth = 0
    def sub(self, text, line, cls=None):
        lx = (cls or Lexer)(text, self.where, line, self.commands, self.scope)
        lx.depth = self.depth
        return lx
    def line(self, pos=None):
        return self.base + bisect.bisect_right(self.starts, self.i if pos is None else pos) - 1
    def fail(self, why):
        raise SyntaxError(f"{self.where}:{self.line()}: {why}")
    def blanks(self):
        while self.i < len(self.t):
            if self.t[self.i] in " \t":
                self.i += 1
            elif self.t.startswith("\\\n", self.i):
                self.i += 2
            else:
                break
    def bodies(self):
        """The bodies of the heredocs of the line that just ended, in their order."""
        for h in self.pending:
            h.body_line, body = self.line(), []
            while True:
                if self.i >= len(self.t):
                    raise SyntaxError(f"{self.where}:{h.line}: the heredoc {h.delim} has no end")
                end = self.t.find("\n", self.i)
                line, self.i = (self.t[self.i:], len(self.t)) if end < 0 else (self.t[self.i:end], end + 1)
                line = line.lstrip("\t") if h.strip else line
                if line == h.delim:
                    break
                body.append(line)
            h.body = h.code = "\n".join(body)
            if not h.quoted:                    # expanded: its substitutions run, and each expansion is a name
                h.code = self.sub(h.body, h.body_line, Expanded).scan()
        self.pending = []
    def keywords_only(self, cmd):
        """Whether cmd's words are keywords, then perhaps time [-p] or coproc [NAME] (before a ( or a { } group)."""
        k = 0
        while k < len(cmd.words) and cmd.words[k].literal and cmd.words[k].text in KEYWORDS:
            k += 1
        rest = [w.text if w.literal and not w.quoted else None for w in cmd.words[k:k + 3]]
        return rest in ([], ["time"], ["time", "-p"], ["time", "!"], ["time", "-p", "!"], ["coproc"]) or \
            len(rest) == 2 and rest[0] == "coproc" and rest[1] is not None
    def at_start(self, cmd):
        return cmd.group is None and self.keywords_only(cmd)
    def run(self, stop=None, subst=False):
        """The commands to the end, or to stop: the ")" of a substitution (subst: $( ), <( ) or >( )), a subshell or a
        process substitution, or the "}" of a { } group."""
        cmd = Command(self.line())
        cases, pattern = 0, False       # the case ... esac this level is in; a case pattern next
        self.depth += 1
        pending = len(self.pending)
        while True:
            self.blanks()
            if self.i >= len(self.t):
                if stop:
                    self.fail(f'a "{OPENS[stop]}" with no "{stop}"')
                self.end(cmd)
                if self.pending:
                    raise SyntaxError(f"{self.where}:{self.pending[0].line}: the heredoc {self.pending[0].delim} has no end")
                self.depth -= 1
                return
            c, start = self.t[self.i], self.i
            if c == "#":
                nl = self.t.find("\n", self.i)
                self.i = len(self.t) if nl < 0 else nl
            elif c == "\n" and len(leading(cmd.words)) == 2 and leading(cmd.words)[0].literal and \
                    leading(cmd.words)[0].text == "case":      # case WORD, a newline, in
                self.i += 1
                self.bodies()
            elif c == "\n":
                self.end(cmd)
                self.i += 1
                self.bodies()
                cmd = Command(self.line())
            elif pattern and not cmd.words and not re.match(r"esac(?![^\s;&|()<>])", self.t[self.i:self.i + 5]):
                self.pattern()
                pattern = False
            elif c == ")":
                if stop != ")":
                    self.fail('a ")" with no "("')
                if subst and len(self.pending) > pending:     # bash gives such a heredoc no body, and runs its lines
                    self.fail(f"the heredoc {self.pending[-1].delim} in $( ) or <( ), whose \")\" comes before its body")
                self.end(cmd)
                self.i += 1
                self.depth -= 1
                return
            elif self.t.startswith("((", self.i) and (self.at_start(cmd) or [w.text for w in cmd.words] == ["for"]):
                line, inner = self.line(), self.arith()
                if inner is None:                # "( (": a subshell in a subshell
                    cmd = self.subshell(cmd)
                else:                            # arithmetic, also for (( ; ; )): its $( ) run
                    self.sub(inner[2:-2], line, Expanded).scan()
                    w = Word(line); w.add(inner); cmd.words.append(w.done())
            elif c == "(":
                if cmd.words and cmd.words[-1].text.endswith("=") and not cmd.words[-1].quoted:   # an array, a=(x y)
                    self.array(cmd)
                elif cmd.words and not self.keywords_only(cmd):    # name() and the body of a function
                    name = cmd.words[-1].text
                    self.i += 1
                    self.blanks()
                    if not self.t.startswith(")", self.i):
                        self.fail(f'"{name} (" that is no function definition')
                    self.i += 1
                    cmd = self.function(name, cmd)
                else:                            # a subshell
                    cmd = self.subshell(cmd)
            elif self.t.startswith(("<(", ">("), self.i):     # a process substitution: its commands run
                w = Word(self.line())
                self.i += 2
                self.run(stop=")", subst=True)
                w.add("<(...)", literal=False, code=PH)
                w.procsub, w.literal = True, False
                cmd.words.append(w.done())
            elif self.t.startswith("&>", self.i) or (self.REDIR.match(self.t, self.i) and c != "&"):
                m = re.compile(r"&>>?").match(self.t, self.i) or self.REDIR.match(self.t, self.i)
                fd = int(m.group(1)) if m.re is self.REDIR and m.group(1) and m.group(1).isdigit() else None
                op = m.group(0).lstrip("0123456789&") or m.group(0)
                self.i = m.end()
                self.blanks()
                if op in ("<<", "<<-"):
                    w = self.word()
                    h = Heredoc(w.text, w.quoted, op == "<<-", self.line(), 0 if fd is None else fd)
                    cmd.heredocs.append(h)
                    cmd.redirs.append((op, h, h.fd))
                    self.pending.append(h)
                elif op == "<<<":
                    cmd.redirs.append((op, self.word(), 0 if fd is None else fd))
                elif self.t.startswith(("<(", ">("), self.i):     # < <(cmd): a process substitution as the file
                    w = Word(self.line())
                    self.i += 2
                    self.run(stop=")", subst=True)
                    w.add("<(...)", literal=False, code=PH)
                    w.procsub, w.literal = True, False
                    cmd.redirs.append((op, w.done(), (0 if op.startswith("<") else 1) if fd is None else fd))
                else:
                    cmd.redirs.append((op, self.word(), (0 if op.startswith("<") else 1) if fd is None else fd))
            elif any(self.t.startswith(o, self.i) for o in self.OPERATORS):
                op = next(o for o in self.OPERATORS if self.t.startswith(o, self.i))
                cmd.unsure = cmd.unsure or op in ("|", "|&", "&")
                self.end(cmd)
                self.i += len(op)
                piped = cmd if op in ("|", "|&") and cmd.words else None
                cmd = Command(self.line())
                cmd.pipe_from, cmd.unsure = piped, op in ("&&", "||", "|", "|&")
                pattern = pattern or (op in (";;", ";&", ";;&") and cases > 0)
            elif self.t.startswith("[[", self.i) and self.t[self.i + 2:self.i + 3] in (" ", "\t", "\n") and \
                    self.at_start(cmd):     # its words have their $( ) run
                w = Word(self.line())
                self.i += 2
                self.cond()
                w.add(self.t[start:self.i]); cmd.words.append(w.done())
            elif self.t.startswith("!(", self.i) and self.at_start(cmd):     # ! and a subshell (extglob is off)
                w = Word(self.line()); w.add("!"); cmd.words.append(w.done())
                self.i += 1
            else:
                w = self.word()
                plain = w.literal and not w.quoted
                if cmd.group is not None:        # after a { } or ( ): a reserved word (then, do, done, fi ...)
                    self.end(cmd)
                    cmd = Command(w.line)
                if plain and w.text == "}" and stop == "}" and (self.at_start(cmd) or len(cmd.words) == 1 and
                                                                cmd.words[0].literal and cmd.words[0].text in CLOSERS):
                    self.end(cmd)
                    self.depth -= 1
                    return
                if plain and self.at_start(cmd) and w.text == "{":        # a { } group
                    cmd = self.group(cmd)
                    continue
                if plain and self.at_start(cmd) and w.text == "function":   # function NAME [()] and its body
                    self.blanks()
                    name = self.word()
                    self.blanks()
                    if self.t.startswith("(", self.i):
                        self.i += 1
                        self.blanks()
                        if not self.t.startswith(")", self.i):
                            self.fail(f'"function {name.text} (" with no ")"')
                        self.i += 1
                    cmd = self.function(name.text, cmd)
                    continue
                k = 0
                while k < len(cmd.words) and cmd.words[k].literal and cmd.words[k].text in KEYWORDS:
                    k += 1
                if plain and w.text == "esac" and cases and self.at_start(cmd):
                    cases, pattern = cases - 1, False
                elif plain and w.text == "in" and len(cmd.words) == k + 2 and cmd.words[k].literal and \
                        cmd.words[k].text == "case":                      # case WORD in: a pattern next
                    cmd.words.append(w)
                    self.end(cmd)
                    cmd = Command(self.line())
                    cases, pattern = cases + 1, True
                    continue
                cmd.words.append(w)
            if self.i == start:
                self.i += 1                      # a character this lexer has no rule for: on past it
    def pattern(self):
        """A case pattern, to its ")": its words are no command (their $( ) run)."""
        if self.t.startswith("(", self.i):
            self.i += 1
        while True:
            self.blanks()
            start = self.i
            self.word()
            self.blanks()
            if self.i > start and self.t.startswith("|", self.i):
                self.i += 1
            elif self.i > start and self.t.startswith(")", self.i):
                self.i += 1
                return
            else:
                self.fail("a case pattern that this check cannot read")
    def group(self, cmd):
        """A { } group, its "{" read: its commands; the command after it holds its redirections."""
        self.end(cmd)                            # the keywords before it, if any
        first = len(self.commands)
        self.run(stop="}")
        after = Command(self.line())
        after.group = self.commands[first:]
        return after
    def subshell(self, cmd):
        """A ( ) subshell at its "(": as a group."""
        self.i += 1
        self.end(cmd)
        first = len(self.commands)
        self.run(stop=")")
        after = Command(self.line())
        after.group = self.commands[first:]
        return after
    def function(self, name, cmd):
        """The body of the function name, after blanks, newlines and comments: a { } group or a ( ) subshell, kept in
        the scope's functions."""
        while True:
            self.blanks()
            if self.t.startswith("#", self.i):
                nl = self.t.find("\n", self.i)
                self.i = len(self.t) if nl < 0 else nl
            elif self.t.startswith("\n", self.i):
                self.i += 1
                self.bodies()
            else:
                break
        cmd.words = []
        if self.t.startswith("{", self.i) and self.t[self.i + 1:self.i + 2] in (" ", "\t", "\n"):
            self.i += 1
            after = self.group(cmd)
        elif self.t.startswith("(", self.i):
            after = self.subshell(cmd)
        else:
            self.fail(f"the body of the function {name} is not {{ }} or ( ), which this check reads")
        for c in after.group:
            c.in_body = True
        self.scope.functions.setdefault(name, []).append(Body(after.group))
        return after
    def array(self, cmd):
        """a=(x y), at its "(": the words read as a command (an array can be one's start); kept when they are
        literal."""
        line = self.line()
        inner = self.matching("(", ")")[1:-1]
        sub = Lexer(inner, self.where, line, [], self.scope)
        sub.run()
        for a in sub.commands:
            a.array = True
        self.commands.extend(sub.commands)
        name = cmd.words[-1].text[:-1]
        add = name.endswith("+")                # a=(...) or a+=(...): its words after those it has
        name = name[:-1] if add else name
        if ASSIGN.fullmatch(name + "="):
            if len(sub.commands) == 1 and all(x.literal for x in sub.commands[0].words) and not sub.commands[0].redirs:
                self.scope.arrays[name] = (self.scope.arrays.get(name, []) if add else []) + sub.commands[0].words
            else:                               # (words this check does not know: no program substitution)
                self.scope.arrays.pop(name, None)
            if any(says_program(x) for c in sub.commands for x in c.words):
                self.scope.pynames.add(name)
        cmd.words[-1].text += "(...)"
    def cond(self):
        """The words of a [[ ]] after its "[[", to the word "]]" (not a "]]" inside a word, as in [[:space:]]): their
        substitutions run; ( ) | & ! < > and newlines are operators here (also in a regex after =~)."""
        while True:
            self.blanks()
            if self.i >= len(self.t):
                self.fail("a [[ with no ]]")
            if self.t.startswith("]]", self.i) and self.t[self.i + 2:self.i + 3] in ("", " ", "\t", "\n", ";", "&", "|", ")"):
                self.i += 2
                return
            if self.t[self.i] in "()|&!<>\n":
                self.i += 1
            else:
                start = self.i
                self.word()
                if self.i == start:
                    self.i += 1
    def end(self, cmd):
        if cmd.group is not None:                # a { } or ( ): its redirections are its commands'
            for c in cmd.group:
                c.redirs = cmd.redirs + c.redirs
                c.heredocs = cmd.heredocs + c.heredocs
        elif cmd.words or cmd.redirs:
            cmd.depth = self.depth
            self.commands.append(cmd)
    def matching(self, open_, close):
        """The raw text from open_ to the close that ends it (quotes, comments and nesting followed)."""
        start, depth = self.i, 0
        while self.i < len(self.t):
            if self.t.startswith(open_, self.i):
                depth += 1; self.i += len(open_); continue
            if self.t.startswith(close, self.i):
                depth -= 1; self.i += len(close)
                if depth == 0:
                    return self.t[start:self.i]
                continue
            c = self.t[self.i]
            if c == "\\":
                self.i += 2; continue
            if c == "'" or c == "`":
                j = self.t.find(c, self.i + 1)
                if j < 0:
                    self.fail(f"a {c} with no end")
                self.i = j + 1; continue
            if c == '"':
                j = self.i + 1
                while j < len(self.t) and self.t[j] != '"':
                    j += 2 if self.t[j] == "\\" else 1
                if j >= len(self.t):
                    self.fail('a " with no end')
                self.i = j + 1; continue
            if c == "#" and (self.i == 0 or self.t[self.i - 1] in " \t\n"):     # a comment, as in an array
                nl = self.t.find("\n", self.i)
                self.i = len(self.t) if nl < 0 else nl
                continue
            self.i += 1
        self.fail(f"a {open_} with no {close}")
    def arith(self):
        """At "((": the text to the "))" that ends it; or None when the first ")" that closes no "(" of its own is not
        followed by another: bash then reads "(" and a subshell."""
        t, i, depth = self.t, self.i + 2, 0
        while i < len(t):
            c = t[i]
            if c == "\\":
                i += 2; continue
            if c in "'`":
                j = t.find(c, i + 1)
                if j < 0:
                    self.fail(f"a {c} with no end")
                i = j + 1; continue
            if c == '"':
                j = i + 1
                while j < len(t) and t[j] != '"':
                    j += 2 if t[j] == "\\" else 1
                if j >= len(t):
                    self.fail('a " with no end')
                i = j + 1; continue
            if c == "(":
                depth += 1
            elif c == ")" and depth:
                depth -= 1
            elif c == ")":
                if not t.startswith("))", i):
                    return None
                start, self.i = self.i, i + 2
                return t[start:self.i]
            i += 1
        self.fail("a (( with no ))")
    def backquoted(self, w, dq=False):
        """A ` ` command substitution: its text with the backslashes of \\`, \\\\ and \\$ (and \\" in "...") dropped,
        read as commands."""
        line, j, out = self.line(), self.i + 1, []
        while True:
            if j >= len(self.t):
                self.fail("a ` with no end")
            c = self.t[j]
            if c == "\\" and self.t[j + 1:j + 2] in ("`", "\\", "$") + (('"',) if dq else ()):
                out.append(self.t[j + 1]); j += 2; continue
            if c == "`":
                break
            out.append(c); j += 1
        self.i = j + 1
        self.sub("".join(out), line).run()
        w.add("`...`", literal=False, code=PH)
        w.literal, w.subst = False, True
        w.subs.append("".join(out))
    def dollar(self, w, ansi=True):
        """$( ), $(( )), ${ }, $'...' and $"..." (ansi: in an unquoted word only), $name or $1, at self.i: a "$"."""
        t, i = self.t, self.i
        if t.startswith("$((", i):
            self.i += 1
            line, inner = self.line(), self.arith()
            if inner is not None:
                self.sub(inner[2:-2], line, Expanded).scan()       # its $( ) run
                w.add("$" + inner, literal=False, code="0")
                w.literal = False
                return
            self.i = i                           # "$( (": a command substitution with a subshell in it
        if t.startswith("$(", i):
            self.i += 2
            self.run(stop=")", subst=True)
            w.add("$(...)", literal=False, code=PH)
            w.literal, w.subst = False, True
            w.subs.append(t[i + 2:self.i - 1])
        elif t.startswith("${", i):
            self.brace()
            w.add(t[i:self.i], literal=False, code=PH)
            w.literal = False
            if re.match(r"\$\{[A-Za-z_]\w*\[[@*]\]\}", t[i:self.i]):
                w.array = True
        elif ansi and t.startswith("$'", i):
            j = i + 2
            while j < len(t) and t[j] != "'":
                j += 2 if t[j] == "\\" else 1
            if j >= len(t):
                self.fail("a $' with no end")
            if PH in ansi_c(t[i + 2:j]):
                self.fail("a $'...' that gives the character U+E000, which this check uses itself")
            w.quoted = True
            w.add(ansi_c(t[i + 2:j]))
            self.i = j + 1
        elif ansi and t.startswith('$"', i):    # $"...": as "..." (its translation)
            self.i += 2
            w.quoted = True
            self.double(w)
        else:
            m = self.NAME.match(t, i)
            if m:
                w.add(m.group(0), literal=False, code=PH)
                w.literal = False
                self.i = m.end()
            else:
                w.add("$"); self.i += 1
    def brace(self):
        """A ${ } at self.i: the words in it (${1:-$(pwd)}, ${x:+"$(f)"}) have their $( ) and ` ` run."""
        self.i += 2
        scratch = Word(self.line())
        while self.i < len(self.t):
            c = self.t[self.i]
            if c == "}":
                self.i += 1; return
            if c == "\\":
                self.i += 2
            elif c == "'":
                j = self.t.find("'", self.i + 1)
                if j < 0:
                    self.fail("a ' with no end")
                self.i = j + 1
            elif c == '"':
                self.i += 1
                self.double(scratch)
            elif c == "$":
                self.dollar(scratch)
            elif c == "`":
                self.backquoted(scratch)
            else:
                self.i += 1
        self.fail("a ${ with no }")
    def word(self):
        w = Word(self.line())
        while self.i < len(self.t):
            c = self.t[self.i]
            if c in "!@+*?" and self.t[self.i + 1:self.i + 2] == "(":       # an extglob group: !(*.txt)
                self.i += 1
                w.add(c + self.matching("(", ")"), literal=False, code=PH)
                w.literal = False
                continue
            if c in " \t\n;&|()<>":
                break
            if c == "\\":
                if not self.t.startswith("\\\n", self.i):
                    w.quoted = True; w.add(self.t[self.i + 1:self.i + 2])
                self.i += 2
            elif c == "'":
                j = self.t.find("'", self.i + 1)
                if j < 0:
                    self.fail("a ' with no end")
                w.quoted = True; w.add(self.t[self.i + 1:j])
                self.i = j + 1
            elif c == '"':
                self.i += 1
                w.quoted = True
                self.double(w)
            elif c == "$":
                self.dollar(w)
            elif c == "`":
                self.backquoted(w)
            else:
                w.add(c); self.i += 1
        return w.done()
    def double(self, w):
        """The inside of "..." from self.i, to its closing quote."""
        while True:
            if self.i >= len(self.t):
                self.fail('a " with no end')
            c = self.t[self.i]
            if c == '"':
                self.i += 1; return
            if c == "\\" and self.t[self.i + 1:self.i + 2] in ('"', "\\", "$", "`", "\n"):
                if self.t[self.i + 1] != "\n":
                    w.add(self.t[self.i + 1])
                self.i += 2
            elif c == "$":
                self.dollar(w, ansi=False)
            elif c == "`":
                self.backquoted(w, dq=True)
            else:
                w.add(c); self.i += 1
class Expanded(Lexer):
    """An expanded text (a heredoc's body, an arithmetic expression): its $( ) and ` ` run; the code of it, with each
    expansion as PH."""
    def scan(self):
        w = Word(self.line())
        while self.i < len(self.t):
            c = self.t[self.i]
            if c == "\\" and self.t[self.i + 1:self.i + 2] in ("\\", "$", "`", "\n"):
                if self.t[self.i + 1] != "\n":
                    w.add(self.t[self.i + 1])
                self.i += 2
            elif c == "$":
                self.dollar(w, ansi=False)
            elif c == "`":
                self.backquoted(w)
            else:
                w.add(c); self.i += 1
        return "".join(w.code)

# Which of the commands run Python, and with which program: python, python3 or python3.N (also by a path through a
# variable, "$VENV/bin/python3", or a variable named like PY, or a default ${PY:-python3}), after the words of nice,
# env, timeout and other programs that run the command after their own options
PYNAME = re.compile(r"(?:python|pypy)(?:[23](?:\.\d+)?)?|ipython3?")
PYVAR = re.compile(r"\$\{?[A-Za-z_]*(?:PY|py|Py)\w*(?:\[[@*0-9]\])?(?::?[-=+?][^}]*)?\}?")
PYDEFAULT = re.compile(r"\$\{\w+:?[-=](?:\S*/)?python(?:3(?:\.\d+)?)?\}")
SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "mksh", "ash"}     # read as bash reads them
FOREIGN = {"csh", "tcsh", "fish"}           # shells whose code this check does not read (they are in CMDLINE)
STDIN = re.compile(r"-|/dev/stdin|/dev/fd/(\d+)|/proc/self/fd/(\d+)")     # a program read from a file descriptor
KEYWORDS = {"if", "then", "elif", "else", "do", "while", "until", "!", "{"}
CLOSERS = {"fi", "done", "esac"}            # after these, "}" ends a group
OPENS = {")": "(", "}": "{"}
# programs whose arguments are text, which run nothing
DATA = {"grep", "egrep", "fgrep", "sed", "awk", "ps", "pgrep", "pkill", "pidof", "apt", "apt-get", "dpkg", "which",
        "type", "man", "ls", "test", "[", "local", "export", "declare", "readonly", "read", "logger", "tr", "cut",
        "sort", "head", "tail", "wc", "print", "true", ":", "typeset", "chmod", "hash"}   # (chmod: the stubs give it
                                                                                            # python's path)
WRAPPERS = {                    # program: (its options that take a value, the words after its options, before the command)
    "env": ({"-u", "--unset", "-C", "--chdir"}, 0), "nice": ({"-n", "--adjustment"}, 0),
    "ionice": ({"-c", "--class", "-n", "--classdata"}, 0), "taskset": (set(), 1),
    "timeout": ({"-k", "--kill-after", "-s", "--signal"}, 1), "setsid": (set(), 0), "exec": ({"-a"}, 0),
    "command": (set(), 0), "builtin": (set(), 0), "nohup": (set(), 0), "stdbuf": ({"-i", "-o", "-e"}, 0),
    "chrt": (set(), 1), "flock": ({"-w", "--timeout", "-E", "--conflict-exit-code"}, 1),
    "sudo": ({"-u", "--user", "-g", "--group", "-C", "-D", "-h", "-p", "-r", "-t", "-U"}, 0), "doas": ({"-u", "-C"}, 0),
    "runuser": ({"-u", "--user", "-g", "--group", "-G", "--supp-group", "-w", "--whitelist-environment"}, 0),
    "setpriv": ({"--reuid", "--regid", "--groups", "--inh-caps", "--ambient-caps", "--bounding-set", "--securebits",
                 "--pdeathsig"}, 0),
    "prlimit": (set(), 0), "choom": ({"-n", "--adjust"}, 0), "xargs": ({"-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s"}, 0),
    "time": ({"-f", "--format", "-o", "--output"}, 0), "coproc": (set(), 0),
    "systemd-run": ({"-p", "--property", "-E", "--setenv", "-u", "--unit", "-H", "--host", "-M", "--machine",
                     "--description", "--slice", "--uid", "--gid", "--nice", "--working-directory", "--on-active",
                     "--on-boot", "--on-startup", "--on-unit-active", "--on-unit-inactive", "--on-calendar",
                     "--timer-property", "--path-property", "--socket-property"}, 0)}
# programs that run shell code given with -c (anywhere among their words), with the letters and words of their options
# that take a value; without -c su and runuser run a shell that reads its standard input
RUNS_C = {"su": ("sgGw", {"-s", "--shell", "-g", "--group", "-G", "--supp-group", "-w", "--whitelist-environment"}),
          "runuser": ("sgGw", {"-s", "--shell", "-g", "--group", "-G", "--supp-group", "-w", "--whitelist-environment"}),
          "script": ("EBIOTmo", {"-E", "--echo", "-B", "--log-io", "-I", "--log-in", "-O", "--log-out", "-T",
                                 "--log-timing", "-m", "--logging-format", "-o", "--output-limit"})}
# programs that run a word they are given, or their standard input, as a command line (an expansion given to one, or
# to a shell among the words of a program this check does not follow, is refused), and those whose job is their
# standard input or -f FILE
CMDLINE = {"ssh", "mosh", "tmux", "screen", "byobu", "parallel", "xterm", "at", "batch", "pdsh", "clush", "ansible",
           "entr"} | FOREIGN
# the options of a program (by its name) whose value names a program as data, not one that runs: rsync -e ssh
NAMES_PROGRAM = {"rsync": {"-e", "--rsh"}, "useradd": {"-s", "--shell"}, "usermod": {"-s", "--shell"},
                 "chsh": {"-s", "--shell"}, "getopt": {"-s", "--shell"}, "journalctl": {"-u", "--unit", "--user-unit"}}
JOBS = {"at", "batch"}
REMOTE = {"ssh", "mosh", "pdsh", "clush"}   # their standard input goes to another host's shell (ssh: see ssh_layout())
SENDS = {"scp", "rsync", "sftp"}            # programs that send a file the script wrote to another host
SSH_VALUED = set("BbcDEeFIiJLlmOopQRSWw")   # ssh's options that take a value; with these no shell on the other host
SSH_QUIET = set("NnfsWOGVQ")                # reads ssh's standard input; and these may run a command (-o ProxyCommand=,
SSH_UNSAFE = set("oF")                      # -F a file of options), so an expansion as their value is refused
READS_STDIN = {"su", "runuser", "sg", "ssh", "mosh", "parallel", "pdsh", "clush", "at", "batch"}  # also a shell's code
PYMENTION = re.compile(r"\b(?:i?python|pypy)")     # a text that names python (one pass over the text: linear)
ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^]]*\])?\+?=")
ARGS = re.compile(r"\$(?:[@*]|\{[@*](?::(\d+)(?::(\d+))?)?\})")     # a function's arguments, all or from the Nth
ZERO = re.compile(r"\$(?:([0-9])|\{([0-9])\})")                      # and $0 to $9 of sh -c CODE ARG0 ARGS
USES_ARGS = re.compile(r"\$(?:[@*0-9]|\{[@*0-9])")
POS = re.compile(r"\$(?:([1-9])|\{([1-9])(:?[-?][^}]*)?\})")          # $1 to $9 of a function (${1:-x}, ${1:?})
ARRAY = re.compile(r"\$\{([A-Za-z_]\w*)\[[@*]\]\}")
VALUE = re.compile(r"\$(?:([A-Za-z_]\w*)|\{([A-Za-z_]\w*)\})")         # $NAME or ${NAME}
COMPOUND = {"if", "while", "until", "for", "case", "select"}
def literal(text, line):
    """A word with this text and no expansion."""
    w = Word(line); w.add(text)
    return w.done()
SUBSCRIPT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\[[^]]*")     # NAME[ and what follows of a subscript that goes on
ASSIGN_SUB = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\[(?:[^][]|\[[^][]*\])*\]\+?=")    # NAME[...]=, a [ ] in it too
def assigns(w):
    """Whether the word w is an assignment, as bash takes one: NAME= (NAME[...]=, NAME+=) before any quote, escape or
    expansion in it (X='a b' and X=$(f) are; "X=1" and X\\=1 are no assignment, but a command of that name); in an
    array's subscript, quotes and expansions may stand (cmds[$i]=..., m["$k"]=...)."""
    return bool(ASSIGN.match(w.text, 0, w.nhead) or SUBSCRIPT.fullmatch(w.text, 0, w.nhead) and
                ASSIGN_SUB.match(w.text))
def norm(text):
    """The name of a file the script writes or runs, without ./ and doubled /: the key of written and unread."""
    return os.path.normpath(text) if text else text
def named(commands):
    """The names that the words of the commands hold other than as $NAME or ${NAME} (NAME=, a read, a for, ${NAME:=x},
    unset ...): name -> how often."""
    seen = {}
    for c in commands:
        for w in c.words:
            for name in re.findall(r"[A-Za-z_]\w*", VALUE.sub(" ", w.text)):
                seen[name] = seen.get(name, 0) + 1
    return seen
def values(commands):
    """The variables that the commands set once, in a command of its own at the top level (not in a function, an if, a
    case or a loop, nor after && or || or in a pipeline: NAME=word, also after export, declare, readonly or typeset), to
    a literal word without blanks, and that no other word names but as $NAME or ${NAME}: name -> word, without those
    named like PY, which are python whatever they are set to."""
    seen, found, level = named(commands), {}, 0
    top = min((c.depth for c in commands if not c.array), default=0)
    for c in commands:
        if c.array or c.depth != top:          # (a function's body is deeper)
            continue
        starts, k = False, 0                    # with keywords: in an if, a loop or a case (level: how deep)
        if c.words and c.words[0].literal and not c.words[0].quoted and c.words[0].text == "time":
            starts, k = True, 2 if len(c.words) > 1 and c.words[1].text == "-p" else 1     # time [-p] while ...
        for w in c.words[k:]:
            if not (w.literal and not w.quoted and w.text in KEYWORDS | COMPOUND | CLOSERS):
                break
            starts, level = True, level + (w.text in COMPOUND) - (w.text in CLOSERS)
        ws = c.words
        if starts or level or c.unsure:
            continue
        if ws and ws[0].literal and ws[0].text in ("local", "export", "declare", "readonly", "typeset"):
            if any(w.text[:1] in "-+" and "n" in w.text[1:] for w in ws[1:]):
                continue                        # a name reference (declare -n J=K): another variable's value
            ws = [w for w in ws[1:] if not w.text.startswith("-")]
        if ws and all(assigns(w) for w in ws):
            for w in ws:
                m = re.fullmatch(r"([A-Za-z_]\w*)=([\w./:@%+-]+)", w.text)
                if m and w.literal:
                    found.setdefault(m.group(1), []).append(m.group(2))
    return {k: v[0] for k, v in found.items() if seen.get(k) == 1 and not PYVAR.fullmatch("$" + k)}
def python_names(commands):
    """The variables that an assignment among the commands sets to a word that names python, anywhere and however often
    (INTERP=$(command -v python3), RUN="$VENV/bin/python3 -u", local b=python3; arrays: see Lexer.array())."""
    found = set()
    for c in commands:
        ws = leading(c.words)
        if ws and ws[0].literal and ws[0].text in ("local", "export", "declare", "readonly", "typeset"):
            ws = [w for w in ws[1:] if not w.text.startswith(("-", "+"))]
        for w in ws:
            if not assigns(w):
                break
            name = re.match(r"[A-Za-z_]\w*", w.text).group(0)
            if says_program(w, w.text.index("=") + 1):  # (its value: ipython_dir=/tmp/x names no python)
                found.add(name)
        for w in c.words:                       # : "${INTERP:=python3}" sets it too
            found.update(m.group(1) for m in ASSIGNS_DEFAULT.finditer(w.text) if PYMENTION.search(m.group(2)))
    return found
ASSIGNS_DEFAULT = re.compile(r"\$\{([A-Za-z_]\w*):?=([^}]*)\}")
def says_program(w, start=0):
    """Whether the word w names python other than as the program of the commands of a command substitution in it (their
    output is no program: PORT=$(python3 -c ...), N=$(curl ... | python3 -c ...)): in its text (where a substitution
    is $(...)), from its character start on, or in a substitution that names it otherwise ($(command -v python3),
    `which python3`)."""
    return bool(PYMENTION.search(unnamed(w.text[start:]))) or any(PYMENTION.search(x) and not python_output(x) for x in w.subs)
@functools.lru_cache(maxsize=256)
def python_output(code):
    """Whether shell code (a command substitution's) names python only as the program of its commands, after keywords,
    assignments and the wrappers of WRAPPERS (timeout 5 python3 ..., cd /tmp && python3 ..., ... | python3 -c ...): what
    it gives is then python's output, no program; code this check cannot read is not."""
    try:
        lx = Lexer(code, "(a command substitution)")
        lx.run()
    except (SyntaxError, RecursionError):
        return False
    for c in lx.commands:
        ws = leading(c.words)
        p = program_at(ws)
        if any(names_python(w) for n, w in enumerate(ws) if n != p):
            return False
    return True
def program_at(ws):
    """The place of the program among a command's words (after the keywords), past assignments, a variable before more
    words (as unwrap() takes it: "${as_user[@]}" python3) and the wrappers of WRAPPERS with their options; -1 when there
    is none (command -v NAME looks a name up)."""
    i = 0
    while i < len(ws):
        if assigns(ws[i]) or ws[i].only_expansion and not is_python(ws[i]) and i + 1 < len(ws):
            i += 1
            continue
        name = base(ws[i])
        if not ws[i].literal or name not in WRAPPERS:
            return i
        if name == "command" and i + 1 < len(ws) and ws[i + 1].text in ("-v", "-V"):
            return -1
        i += option_end(ws[i:], WRAPPERS[name][0])
        while name == "env" and i < len(ws) and ASSIGN.match(ws[i].text):
            i += 1
        i += WRAPPERS[name][1]
    return -1
def unnamed(text):
    """A text as written without the names of the variables it expands ($ipython_dir, ${ipython_dir:-x}: a name is
    no mention of python; a variable named for python is found by its name: see python_variable())."""
    return re.sub(r"(\$\{?)[A-Za-z_]\w*", r"\1", text) if "$" in text else text
def is_python(w):
    return bool(PYNAME.fullmatch(os.path.basename(w.text)) or (not w.literal and (PYVAR.fullmatch(w.text) or
                                                                                    PYDEFAULT.fullmatch(w.text))))
def read_fd(text):
    """The file descriptor that a program named "-", /dev/stdin, /dev/fd/N or /proc/self/fd/N is read from, or None."""
    m = STDIN.fullmatch(text)
    return None if m is None else int(m.group(1) or m.group(2) or 0)
FLAGS = set("abBCdeEfhHiIklnpPqrRsStTuvVx")     # python's and sh's options without a value
def code_option(words):
    """The first of the leading options in words with which python or a shell runs code given here: -c or -m (also in a
    group of their short options: -Ic, -ec), or "-"; None when there is none, or a word is no option of theirs."""
    i = 0
    while i < len(words):
        t = words[i].text
        if t == "-":
            return t
        if not t.startswith("-") or t == "--" or t.startswith("--"):
            return None
        i += 1
        for j, ch in enumerate(t[1:], 1):
            if ch in "cm":
                return t
            if ch in "WXoO":                     # a value: the rest of the word, or the next one (-O then -c: a flag)
                if j == len(t) - 1 and not (ch == "O" and i < len(words) and words[i].text.startswith("-")):
                    i += 1
                break
            if ch not in FLAGS:
                return None
    return None
def sudo_shell(options, name="sudo"):
    """Whether sudo's option words hold -s or -i (--shell, --login), or doas's -s: with no command, they run a shell
    then."""
    for w in options:
        t = w.text
        if t in ("--shell", "--login") and name == "sudo":
            return True
        if t.startswith("-") and not t.startswith("--"):
            for ch in t[1:]:
                if ch in ("si" if name == "sudo" else "s"):
                    return True
                if "-" + ch in WRAPPERS[name][0]:
                    break                       # the rest of the word is that option's value
    return False
def option_end(words, valued):
    """The index of the first word after a wrapper's options (words[0] is the wrapper; valued: its options that take a
    value, also as the last letter of a group: -iu USER)."""
    i = 1
    while i < len(words) and words[i].text.startswith("-") and words[i].text != "-":
        t = words[i].text
        if t == "--":
            return i + 1
        if t in valued or not t.startswith("--") and \
                next((j for j, ch in enumerate(t[1:], 1) if "-" + ch in valued), 0) == len(t) - 1:
            i += 2
        else:
            i += 1
    return i
def bare_shell(words):
    """Whether sudo or doas (words[0]) runs a shell that reads its standard input: -s or -i and no command."""
    name = base(words[0])
    i = option_end(words, WRAPPERS[name][0])
    return i >= len(words) and sudo_shell(words[1:i], name)
def unwrap(words, where_of, scope, array=False):
    """The words of the command that the command's wrappers run, and the code of flock -c or env -S (or None), with
    the wrapper's name.  sudo -s or -i, doas -s and runuser -u USER with no command run a shell that reads its standard
    input: the words are then sh.  array: the words of an array (a=(...)), which run nothing themselves."""
    while True:
        # keywords, assignments, an array of the script's, a variable that the script sets once to a word (as that
        # word), and a variable before a program (a wrapper, or nothing: "${as_user[@]}" python3); a variable followed
        # by -c, -m or -: the program is in a variable.  In shell code that a word gives, an expansion of that word
        # alone, or unquoted before more words, is code that the shell reads ("$cmd -v"): it cannot be read
        while words:
            w = words[0]
            m = None if w.literal else ARRAY.fullmatch(w.text)
            if m and m.group(1) in scope.arrays:     # (its words counted: see charge())
                charge(len(scope.arrays[m.group(1)]), where_of(w.line),
                       "words of arrays put in place of their expansions")
                words = scope.arrays[m.group(1)] + words[1:]
                continue
            m = None if w.literal else VALUE.fullmatch(w.text)
            if m and (m.group(1) or m.group(2)) in scope.values:
                words = [literal(scope.values[m.group(1) or m.group(2)], w.line)] + words[1:]
                continue
            if w.only_expansion and PH in w.text and (len(words) == 1 or not w.quoted) and not array:
                cannot(where_of(w.line), "a command that is only an expansion of the word that gave this shell its "
                                         "code, or starts with one: the shell reads what it holds as code, which this "
                                         "check cannot read")
                return [], None, None
            if len(words) < 2:
                break
            if w.literal and w.text in KEYWORDS or assigns(w):
                words = words[1:]
            elif w.only_expansion and not is_python(w) and not ARGS.fullmatch(w.text):
                opt = code_option(words[1:])
                if opt:
                    cannot(where_of(w.line), f"{w.text} {opt}: the program is in a variable, which this check cannot read")
                    return [], None, None
                words = words[1:]
            else:
                break
        if not (words and words[0].literal and os.path.basename(words[0].text) in WRAPPERS):
            return words, None, None
        name = os.path.basename(words[0].text)
        if name == "runuser" and not any(x.text in ("-u", "--user") or x.text.startswith("--user=") for x in words):
            return words, None, None             # runuser USER -c CODE: as su
        valued, positional = WRAPPERS[name]
        i = 1
        while i < len(words) and words[i].text.startswith("-") and words[i].text != "-":
            t = words[i].text
            if t == "--":
                i += 1; break
            if name == "flock" and t in ("-c", "--command"):
                return [], (words[i + 1] if i + 1 < len(words) else None), "flock -c"
            if name == "flock" and t.startswith("--command="):
                return [], tail(words[i], len("--command=")), "flock -c"
            m = re.fullmatch(r"-([0iv]*)S(.*)", t, re.S) if name == "env" else None
            if m and m.group(2) or name == "env" and t.startswith("--split-string="):     # env -SSTRING, -vSSTRING
                return [], tail(words[i], len(m.group(1)) + 2 if m else len("--split-string=")), "env -S"
            if m or name == "env" and t == "--split-string":   # env -S STRING: the string is a command line
                return [], (words[i + 1] if i + 1 < len(words) else None), "env -S"
            if name == "command" and t in ("-v", "-V"):
                return [], None, None           # says where a program is: runs none
            if t in valued or not t.startswith("--") and \
                    next((j for j, ch in enumerate(t[1:], 1) if "-" + ch in valued), 0) == len(t) - 1:
                i += 2                          # also -iu USER: options whose last one takes the next word
            else:
                i += 1
        if name == "env":
            while i < len(words) and ASSIGN.match(words[i].text):
                i += 1
        if name == "flock" and i + 1 < len(words) and words[i + 1].text in ("-c", "--command"):
            return [], (words[i + 2] if i + 2 < len(words) else None), "flock -c"    # flock FILE -c CODE
        if name == "flock" and i + 1 < len(words) and words[i + 1].text.startswith("--command="):
            return [], tail(words[i + 1], len("--command=")), "flock -c"
        if i + positional >= len(words) and (name == "runuser" or name in ("sudo", "doas") and
                                             sudo_shell(words[1:i], name)):
            return [literal("sh", words[0].line)], None, None
        r = into_code(words[1:i], words[i + positional:]) if name == "xargs" else None
        if r:
            cannot(where_of(words[0].line), f"xargs puts what it reads ({r}) into the code that python or a shell "
                                            "runs, which this check cannot read")
            return [], None, None
        words = words[i + positional:]
def into_code(options, rest):
    """The string that xargs's options (-I R, -i, --replace) replace in its command's words when it is in the code of
    the python -c or sh -c that it runs (rest), or None."""
    r = None
    for n, x in enumerate(options):
        t = x.text
        if t == "-I" and n + 1 < len(options):
            r = options[n + 1].text
        elif t.startswith("-I") and len(t) > 2:
            r = t[2:]
        elif t in ("-i", "--replace"):
            r = "{}"
        elif t.startswith(("-i", "--replace=")):
            r = t[2:] if t.startswith("-i") else t[len("--replace="):]
    if not r or not rest:
        return None
    if is_python(rest[0]):
        kind, w = python_program(rest)
        return r if kind == "c" and w is not None and r in w.text else None
    if rest[0].literal and base(rest[0]) in SHELLS:
        i, dash_c, _ = shell_options(rest, 1)
        return r if dash_c and i < len(rest) and r in rest[i].text else None
    return None
def python_program(words):
    """What a python command runs: ("c", word), ("m", word), ("file", word), ("stdin", fd) for a program read from
    a file descriptor ("-", /dev/stdin, /dev/fd/N), ("end", 0) when it names none (it reads its standard input), or
    ("none", None) for --version and --help."""
    i = 1
    while i < len(words):
        w = words[i].text
        if w in ("-V", "--version", "-h", "--help", "-?"):
            return "none", None
        if w == "--":
            if i + 1 >= len(words):
                return "end", 0
            fd = read_fd(words[i + 1].text)
            return ("file", words[i + 1]) if fd is None else ("stdin", fd)
        if w.startswith("--"):
            i += 2 if w == "--check-hash-based-pycs" else 1
            continue
        if w.startswith("-") and len(w) > 1:
            for j, ch in enumerate(w[1:], 1):
                if ch in "cmWX":
                    rest = w[j + 1:]
                    if ch in "cm":                  # -cCODE: the code is the rest of the word
                        return ch, (tail(words[i], j + 1) if rest else words[i + 1] if i + 1 < len(words) else None)
                    i += 1 if rest else 2
                    break
            else:
                i += 1
            continue
        fd = read_fd(w)
        return ("file", words[i]) if fd is None else ("stdin", fd)
    return "end", 0
def last_input(cmd, fd):
    """The last redirection of the file descriptor fd of cmd: (op, word or heredoc), or None; < /dev/stdin (or
    /dev/fd/0, /proc/self/fd/0) gives fd 0 what it had: it is no redirection of it."""
    found = None
    for op, obj, n in cmd.redirs:
        if n == fd and not (fd == 0 and op == "<" and isinstance(obj, Word) and read_fd(obj.text) == 0):
            found = (op, obj)
    return found
def leading(words):
    """The words after the keywords a command starts with."""
    k = 0
    while k < len(words) and words[k].literal and words[k].text in KEYWORDS:
        k += 1
    return words[k:]
def cat_only(cmd):
    """Whether cmd is a cat that copies its standard input to its standard output (to a pipe)."""
    words = leading(cmd.words)
    return bool(words) and words[0].literal and os.path.basename(words[0].text) == "cat" and \
        all(w.text in ("-", "--", "-u") for w in words[1:]) and last_input(cmd, 1) is None
def program_input(cmd, fd):
    """The program a command reads on fd: ("code", text, line, source) for a heredoc with a quoted delimiter or a
    here-string without an expansion, also through a pipe from cat; ("file", word) for a file; (None, why) else."""
    last = last_input(cmd, fd)
    if last is None:
        if fd == 0 and cmd.pipe_from is not None and cat_only(cmd.pipe_from):
            return program_input(cmd.pipe_from, 0)
        if fd == 0:
            return None, ("its standard input (a pipe, or the terminal): give it in a heredoc with a quoted delimiter, "
                          "with -c, or in a file")
        return None, f"file descriptor {fd}, which no redirection of the command gives"
    op, obj = last
    if op in ("<<", "<<-"):
        if not obj.quoted:
            return None, "a heredoc whose delimiter is not quoted, which the shell expands first: quote it (<<'EOF')"
        if PH in obj.body and not obj.body.replace(PH, "").strip():
            return None, "a heredoc that is an expansion of the word that gave this code"
        return "code", obj.body, obj.body_line, obj
    if op == "<<<":
        if not obj.literal:
            return None, "a here-string with an expansion in it"
        return "code", obj.code, obj.line, obj
    if op in ("<", "<>"):
        return (None, "a process substitution") if obj.procsub else ("file", obj)
    return None, f"{op}{obj.text}, which this check does not follow"
def text_in(cmd, where_of):
    """What a command reads on its standard input as the script gives it in full (a heredoc, expanded when its delimiter
    is not quoted, each expansion as a name; a here-string; a file the script wrote; a pipe from cat, printf or echo):
    (text, where, source, [its texts as written]), and for a file the script wrote, its name too; or None."""
    last = last_input(cmd, 0)
    if last is None:
        p = cmd.pipe_from
        if p is None:
            return None
        return text_in(p, where_of) if cat_only(p) else printed(p.words, where_of)
    op, obj = last
    if op in ("<<", "<<-"):                     # each of its lines ends with a newline
        if obj.given is None:
            obj.given = (obj.body if obj.quoted else obj.code) + "\n" if obj.body else ""
        return obj.given, f"{where_of(obj.body_line)} (a file this heredoc writes)", obj, [obj.body]
    if op == "<<<":
        return obj.code + "\n", f"{where_of(obj.line)} (a file this here-string writes)", obj, [obj.text]
    if op in ("<", "<>") and not obj.procsub and normed(obj) in written:
        return wrote(normed(obj)) + (raws[normed(obj)], normed(obj))
    return None
def printed(words, where_of):
    """What printf FORMAT (a literal format, no directive but %%, no argument) or echo [-n] with literal words writes:
    (text, where, source, [that text]), or None; once for each command (a function's body is read again at its
    calls)."""
    hit = printed_once.get(id(words))
    if hit is None or hit[0] is not words:
        hit = printed_once[id(words)] = words, print_of(words, where_of)
    return hit[1]
def print_of(words, where_of):
    words = leading(words)
    if not words or not words[0].literal:
        return None
    name, args = os.path.basename(words[0].text), words[1:]
    if name == "printf":
        args = args[1:] if args and args[0].text == "--" else args
        if len(args) == 1 and args[0].literal and "%" not in args[0].text.replace("%%", "") and \
                PH not in ansi_c(args[0].text):
            text = ansi_c(args[0].text).replace("%%", "%")
            return text, f"{where_of(args[0].line)} (a file printf writes)", args[0], [text]
    elif name == "echo":
        newline = not (args and args[0].text == "-n")
        args = args[1:] if not newline else args
        if all(a.literal for a in args) and not (args and args[0].text.startswith("-")):
            text = " ".join(a.text for a in args) + "\n" * newline
            return text, f"{where_of(words[0].line)} (a file echo writes)", words[0], [text]
    return None
def writes(cmd, words, where_of):
    """cat or tee from a heredoc, a here-string or a pipe, printf or echo, to a file: kept for a python or shell that
    runs it, and read by settle(); >> (and tee -a) adds to the text kept for the file."""
    name = os.path.basename(words[0].text)
    targets = [(w, op == ">>") for op, w, fd in cmd.redirs if fd == 1 and op in (">", ">>", ">|") and isinstance(w, Word)]
    if name == "tee":
        add = any(x.text == "--append" or re.fullmatch(r"-[a-z]*a[a-z]*", x.text) for x in words[1:])
        targets += [(x, add) for x in words[1:] if not x.text.startswith("-")]
    if not targets or name == "cat" and not all(x.text in ("-", "--", "-u") for x in words[1:]):
        return                                   # (cat FILE, cat -n: not the text given here)
    got = text_in(cmd, where_of) if name in ("cat", "tee") else printed(words, where_of)
    if got is None:
        if name in ("printf", "echo") and any(t.text.endswith(".py") for t, _ in targets):
            cannot(where_of(words[0].line), f"{name} writes a .py file from words this check cannot read (an expansion, "
                                            "or printf's directives or arguments)")
        elif name in ("printf", "echo"):        # what a shell or python that runs it would read is not known
            for t, _ in targets:
                if normed(t) in written:
                    settle(normed(t))
                written.pop(normed(t), None); pending.pop(normed(t), None); raws.pop(normed(t), None)
                unread[normed(t)] = where_of(words[0].line)
        return
    for t, add in targets:
        f = normed(t)
        if add and f in written:                 # after what the script wrote there before: read as a whole
            text, what, key = written[f]
            pending.setdefault(f, []).append(got[0])
            raws[f].extend(got[3])
            written[f] = (text, what, (f, len(raws[f])))      # (a key of its own for each text: read again)
        elif f not in unread or not add:
            if f in written:
                settle(f)                        # what it held until now
            written[f], raws[f] = got[:3], list(got[3])
            pending.pop(f, None)
            unread.pop(f, None)
def settle(f):
    """The text the script wrote to the file f: compiled when its first line names python or f ends in .py, read as
    shell when its first line names a shell (each text once)."""
    text, what, key = wrote(f)
    first = text.split("\n", 1)[0]
    if first.startswith("#!") and PYMENTION.search(first):
        read = check
    elif first.startswith("#!") and re.search(r"\b(?:ba|da)?sh\b", first):
        read = shell
    elif f.endswith(".py"):
        read = check
    else:
        return
    parts(f, what)
    read(text, what, key)
def run_written(w, how, where, scope=None, python=False, outer=None):
    """A file that python or a shell runs (or source reads): when the script wrote it, its text is compiled or read,
    and True is returned; it cannot be read when the script wrote it from words this check cannot read."""
    f = norm(w.text)
    if f in unread:
        cannot(where, f"{how} runs {w.text}, which the script wrote at {unread[f]} from words this check cannot read")
    elif f in written:
        parts(f, where)
        if python:
            check(*wrote(f))
        else:
            shell(*wrote(f), scope=scope, outer=outer)
    else:
        return False
    return True
def sourced(w, how, where, scope):
    """A file of the repository that source or . reads, by a literal path from the script's directory or from the
    repository's top: read in this shell, once for each shell (the script's, or one that sh -c code runs), so that its
    functions are that shell's."""
    for top in (os.path.dirname(current), "."):
        p = os.path.normpath(os.path.join(top, w.text))
        if w.literal and os.path.isfile(p) and os.path.realpath(p).startswith(TOP + os.sep):
            if (scope, os.path.realpath(p)) not in read_once:
                read_once.add((scope, os.path.realpath(p)))
                text = repo_text(os.path.realpath(p), where)
                if text is not None and PH in text:
                    cannot(where, f"{p}, which {how} reads, holds the character U+E000, which this check uses itself")
                elif text is not None:
                    shell(text, f"{p} ({how})", None, scope)
            return
def program_file(w, how, where):
    """A file that python reads its program from (< FILE): the text the script wrote there, read; a file of the
    repository by a literal path from its top, compiled, or passed when the walk of the .py files compiles it; /dev/null,
    no program.  False when it is none of these."""
    if run_written(w, how, where, python=True) or w.literal and w.text == "/dev/null":
        return True
    if not w.literal or not os.path.isfile(w.text) or not os.path.realpath(w.text).startswith(TOP + os.sep):
        return False
    rel = os.path.relpath(os.path.realpath(w.text), TOP)
    if not rel.endswith(".py") or any(x.startswith(".") or x == "__pycache__" for x in rel.split(os.sep)[:-1]):
        with open(w.text, "rb") as f:
            check(f.read(), rel)
    return True
def shell_input(cmd, how, fd, where, where_of, scope=None, outer=None):
    """A shell (or source) that reads its code from the file descriptor fd of cmd: that code read."""
    got = program_input(cmd, fd)
    if got[0] == "code":
        shell(got[1], f"{where_of(got[2])} ({how} heredoc)", got[3], scope, outer=outer)
    elif got[0] == "file":
        run_written(got[1], how, where, scope, outer=outer)
    else:
        cannot(where, f"{how} reads its code from {got[1]}")
def shell(code, where, key=None, scope=None, args=None, call=None, outer=None):
    """Shell code that a command runs (sh -c, bash -c, flock -c, a shell's heredoc ...), read as shell; scope: the
    shell's own when the code runs in it (trap, source), else the code is a new shell's; args: the words after the
    code of sh -c CODE ARG0 ARGS (call: that command): a command of the code that holds "$@", $*, or $0 to $9, is read
    with them in their place (at each reading, which its length and its commands count for: see charge()); outer: the
    scope of the shell that starts a new one, whose python variables it may have exported (export INTERP; bash -c
    '... "$INTERP" ...')."""
    uses = args is not None and USES_ARGS.search(code)
    if key is not None and not uses:
        if id(key) in shelled:
            return
        shelled[id(key)] = key
    try:
        if uses:
            charge(len(code) // 80, where)
        lx = Lexer(code, where, scope=scope)
        if scope is None and outer is not None:
            lx.scope.pynames |= outer.pynames
        lx.run()
        if scope is None:
            lx.scope.values = values(lx.commands)
        else:                                   # code that runs in this shell: a variable it names may change
            for name in named(lx.commands):
                lx.scope.values.pop(name, None)
            lx.scope.pyx.clear()
        lx.scope.pynames |= python_names(lx.commands)
        prepare(lx.scope)
        if uses:
            charge(len(lx.commands), where)
        for cmd in lx.commands:
            if cmd.at_call:
                continue
            if uses and not cmd.in_body and holds_args(cmd, zero=True):
                cmd = substituted(cmd, args, call, where, zero=True)
            analyse(cmd, lambda line: f"{where}:{line}", lx.scope)
    except SyntaxError as e:
        print(f"cannot read: {e}"); globals()["bad"] = 1
    except RecursionError:
        cannot(where, "nested too deeply for this check")
# Python's own environment variables, which name no interpreter (PYTHONEXECUTABLE, which does, is not among them)
PY_OWN = {"PYTHONPATH", "PYTHONHOME", "PYTHONSAFEPATH", "PYTHONPLATLIBDIR", "PYTHONSTARTUP", "PYTHONOPTIMIZE",
          "PYTHONBREAKPOINT", "PYTHONDEBUG", "PYTHONINSPECT", "PYTHONUNBUFFERED", "PYTHONVERBOSE", "PYTHONCASEOK",
          "PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX", "PYTHONHASHSEED", "PYTHONINTMAXSTRDIGITS",
          "PYTHONIOENCODING",
          "PYTHONNOUSERSITE", "PYTHONUSERBASE", "PYTHONWARNINGS", "PYTHONFAULTHANDLER", "PYTHONTRACEMALLOC",
          "PYTHONPROFILEIMPORTTIME", "PYTHONASYNCIODEBUG", "PYTHONMALLOC", "PYTHONMALLOCSTATS", "PYTHONCOERCECLOCALE",
          "PYTHONDEVMODE", "PYTHONUTF8", "PYTHONWARNDEFAULTENCODING", "PYTHONNODEBUGRANGES", "PYTHONPERFSUPPORT",
          "PYTHONLEGACYWINDOWSFSENCODING", "PYTHONLEGACYWINDOWSSTDIO", "PYTHONTHREADDEBUG", "PYTHONDUMPREFS",
          "PYTHONDUMPREFSFILE", "PYTHON_PERF_JIT_SUPPORT", "PYTHON_CPU_COUNT", "PYTHON_FROZEN_MODULES", "PYTHON_COLORS",
          "PYTHON_BASIC_REPL", "PYTHON_HISTORY", "PYTHON_GIL", "PYTHON_JIT", "PYTHON_PRESITE"}
EXPANDS = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")     # $NAME, ${NAME ...} (not ${#NAME}, ${!NAME})
def py_named(name):
    """Whether a variable's name is python's: a part of it between "_" starts with py (PY, PYBIN, PYEXE, PYTHON3,
    VENV_PY, py_cmd); not Python's own variables (PYTHONPATH ...), nor DPY, COPY or VENVPY."""
    return name.upper() not in PY_OWN and any(part[:2].lower() == "py" for part in name.split("_"))
def base(w):
    """os.path.basename of the word's text, once for each word (the words of a function's body come again at its
    calls)."""
    if w.bname is None:
        w.bname = os.path.basename(w.text)
    return w.bname
def normed(w):
    """norm() of the word's text, once for each word."""
    if w.nname is None:
        w.nname = norm(w.text)
    return w.nname
def names_python(w):
    """Whether the word w holds python as written, also in a command substitution in it ("$(command -v python3)"),
    once for each word."""
    if w.pyword is None:
        w.pyword = bool(PYMENTION.search(unnamed(w.text))) or any(PYMENTION.search(x) for x in w.subs)
    return w.pyword
@functools.lru_cache(maxsize=64)
def text_names_python(text):
    """Whether a text (a file's, an input's) holds python (not as a variable's name: unnamed()): once for each text that
    a call reads again."""
    return bool(PYMENTION.search(unnamed(text)))
def python_variable(name, scope):
    """Whether the variable name is python's: named for it (py_named()), or set by an assignment or an array to a word
    that names python (scope.pynames)."""
    return py_named(name) or name in scope.pynames
def python_expansion(text, scope):
    """The first expansion in a text, as written ($NAME, ${NAME...}, "${NAME[@]}"), of a variable that is python's
    (python_variable()), or None; once for each text that a call reads again."""
    if text not in scope.pyx:
        scope.pyx[text] = next((m.group(0) for m in EXPANDS.finditer(text) if python_variable(m.group(1), scope)),
                               None)
    return scope.pyx[text]
def repo_text(p, where):
    """The text of the file p of the repository, or None (when it cannot be read: said)."""
    try:
        return repo_read(p)
    except OSError as e:
        cannot(where, f"{os.path.relpath(p, TOP)} cannot be read: {e.strerror}")
        return None
@functools.lru_cache(maxsize=16)
def repo_read(p):
    with open(p, "rb") as fh:
        return fh.read().decode(errors="replace")
def in_repo(w):
    """The path of the file of the repository that the literal word w names, from the top, or None."""
    p = os.path.normpath(w.text)
    return p if w.literal and os.path.isfile(p) and os.path.realpath(p).startswith(TOP + os.sep) else None
def wrote(f):
    """written[f], with what the script added to it with >> since it was last read (kept apart until then: joining at
    each >> would take time quadratic in their number)."""
    if pending.get(f):
        text, what, key = written[f]
        written[f] = (text + "".join(pending.pop(f)), what, key)
    return written[f]
def says_python(texts, scope):
    """What in texts (as written) names python: the text, or an expansion of a variable that is python's; or None."""
    for text in texts:
        if text_names_python(text):
            return "python"
        x = python_expansion(text, scope)
        if x:
            return x
    return None
def file_says(f, scope):
    """says_python() of the texts the script wrote to the file f, as written and whole: once for each state of them
    (a body's command that reads the file is read again at each call)."""
    k = ("file", f, id(raws[f]), len(raws[f]))
    if k not in scope.pyx:                      # (the list is kept: its id is not taken by another)
        scope.pyx[k] = raws[f], says_python(raws[f] + [wrote(f)[0]], scope)
    return scope.pyx[k][1]
def input_says(got, scope):
    """says_python() of what a command reads on its standard input, as text_in() found it."""
    return file_says(got[4], scope) if len(got) > 4 else says_python([got[0]] + got[3], scope)
def mention(cmd, words, where_of, scope, files):
    """What a command that this check does not follow is given that names python (a word that holds python, also in a
    command substitution, or an expansion of a variable that is python's: python_variable(); with files, a file the
    script wrote that holds one; its standard input as the script gives it: a heredoc, a here-string, a pipe of
    literal text), said for the message; None when nothing is."""
    for w in words:
        if names_python(w) or python_expansion(w.text, scope):
            return f"a word that names python ({w.text[:40]})"
        if files and normed(w) in written and file_says(normed(w), scope):
            return f"a file the script wrote that names python ({w.text[:40]})"
    got = text_in(cmd, where_of)
    if got is not None and input_says(got, scope):
        return "an input that names python"
    return None
def given_expansion(cmd):
    """Whether the standard input that the script gives cmd as text holds an expansion: a heredoc whose delimiter is not
    quoted, or a here-string, with one in it, or a pipe from echo or printf given one (also through cat)."""
    last = last_input(cmd, 0)
    if last is None:
        p = cmd.pipe_from
        if p is None:
            return False
        if cat_only(p):
            return given_expansion(p)
        ws = leading(p.words)
        return bool(ws) and ws[0].literal and base(ws[0]) in ("echo", "printf") and any(not w.literal for w in ws[1:])
    op, obj = last
    if op in ("<<", "<<-"):
        return not obj.quoted and PH in obj.code
    return op == "<<<" and not obj.literal
def unseen(cmd, where_of, scope):
    """Why a program that runs its standard input as code cannot be given what the script gives it there: None when the
    script gives it nothing (no redirection of it, no pipe into it), or text that this check sees in full, without an
    expansion, that names no python (a heredoc, a here-string, a pipe of literal text, a file the script wrote or one of
    the repository, /dev/null)."""
    last = last_input(cmd, 0)
    if last is None and cmd.pipe_from is None:
        return None
    if given_expansion(cmd):
        return "an input with an expansion in it"
    got = text_in(cmd, where_of)
    if got is not None:
        return "an input that names python" if input_says(got, scope) else None
    texts = None
    if last is not None and last[0] in ("<", "<>") and isinstance(last[1], Word):
        if last[1].literal and last[1].text == "/dev/null":
            texts = []
        elif in_repo(last[1]):
            texts = [repo_text(in_repo(last[1]), where_of(last[1].line)) or ""]
    if texts is None:
        return ("an input that this check does not see (a program's output, a process substitution, a file that the "
                "script did not write and that is not one of the repository)")
    return "an input that names python" if says_python(texts, scope) else None
def job(cmd, words, where_of, where, scope):
    """Why the job of at or batch fails: None when this check sees it, as the script gives it, and it names no python
    (its standard input: a heredoc, a here-string, a pipe of literal text, a file the script wrote; or -f FILE: a file
    the script wrote or one of the repository), or when there is none (-l, -d, -r, -c and -V list, remove or print)."""
    if any(x.text[:1] == "-" and x.text[1:2] in tuple("ldrcV") for x in words[1:]):
        return None
    f = next((words[i + 1] for i in range(1, len(words) - 1) if words[i].text == "-f"), None)
    f = f or next((tail(x, 2) for x in words[1:] if x.text.startswith("-f") and len(x.text) > 2), None)
    if f is None:
        if text_in(cmd, where_of) is None:
            return ("reads its job from its standard input, which this check does not see here (the terminal, or a "
                    "program's output)")
        return None                             # (mention() looked at it)
    if normed(f) in written:
        return f"is given a job that names python ({f.text})" if file_says(normed(f), scope) else None
    texts = None
    if in_repo(f):
        texts = [repo_text(os.path.realpath(in_repo(f)), where)]
        texts = None if texts[0] is None else texts
    if texts is None:
        return f"reads its job from {f.text}, which this check does not see"
    return f"is given a job that names python ({f.text})" if says_python(texts, scope) else None
def shell_code(w, how, where, where_of, scope=None, args=None, call=None, outer=None):
    """A word that a shell runs as code (sh -c CODE, su -c, trap ...): read, or why it cannot be; outer: the scope of
    the shell that gives it (see resolved())."""
    if w is None:
        cannot(where, f"{how} with no code")
    elif w.only_expansion or w.subst:
        cannot(where_of(w.line), f"{how} runs what a variable or a command gives, which this check cannot read")
    else:
        shell(resolved(w, outer), f"{where} ({how})", w.key, scope, args, call, outer)
def shell_options(words, i):
    """A shell's options from words[i]: the index of its first operand, and whether -c and -s are given."""
    dash_c = dash_s = False
    while i < len(words) and words[i].text[:1] in "-+" and len(words[i].text) > 1:
        t = words[i].text
        if t in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            i += 2; continue
        if t == "--":
            i += 1; break
        if not t.startswith("--") and t[0] == "-":
            dash_c, dash_s = dash_c or "c" in t[1:], dash_s or "s" in t[1:]
        i += 1
    return i, dash_c, dash_s
def holds_args(cmd, zero=False, positional=False):
    """Whether a word of cmd is the arguments ("$@", "${@:N}", $*; with zero, also $0 to $9; with positional, only $1 to
    $9)."""
    return any(not w.literal and (POS.fullmatch(w.text) if positional else ARGS.fullmatch(w.text) or
                                  zero and ZERO.fullmatch(w.text)) for w in cmd.words)
def c_code(words):
    """The code that su, runuser or script (words[0]) is given with -c, anywhere among their words (they are permuted):
    (True, the word, or None when -c ends the words), or (False, None) when there is none."""
    letters, valued = RUNS_C[base(words[0])]
    i = 1
    while i < len(words):
        t = words[i].text
        if t in ("-c", "--command", "--session-command"):
            return True, words[i + 1] if i + 1 < len(words) else None
        if t.startswith(("--command=", "--session-command=")):
            return True, tail(words[i], t.index("=") + 1)
        if t in valued:
            i += 2; continue
        if t.startswith("-") and not t.startswith("--"):
            for j, ch in enumerate(t[1:], 1):
                if ch == "c":
                    return True, tail(words[i], j + 1) if t[j + 1:] else words[i + 1] if i + 1 < len(words) else None
                if ch in letters:               # an option with a value: the rest of the word, or the next one
                    i += 0 if t[j + 1:] else 1
                    break
        i += 1
    return False, None
def runs_input(words):
    """Whether the program of READS_STDIN that words start with runs its standard input: su and runuser without -c, sg
    with no command, the others always."""
    b = base(words[0])
    if b in RUNS_C:
        return not c_code(words)[0]
    if b == "sg":
        return len(words[2:] if len(words) > 1 and words[1].text == "-" else words[1:]) <= 1
    return True
def passes_on(cmd):
    """Whether what cmd reads on its standard input may reach its output: cmd has no redirection of it and is no echo
    or printf (cat, tee ...), and so does each command that pipes into it."""
    while cmd is not None:
        ws = leading(cmd.words)
        if last_input(cmd, 0) is not None or ws and ws[0].literal and base(ws[0]) in ("echo", "printf"):
            return False
        cmd = cmd.pipe_from
    return True
def reads_program(cmd, scope=None, piped=False):
    """Whether cmd runs python, or a shell, that reads its program from its standard input, or a program that may run
    its standard input (READS_STDIN: ssh, at, su without -c ...; sudo -s or -i, doas -s with no command; xargs that
    puts it among the words of python or a shell), or calls a function of scope whose body reads the call's input, and
    cmd has no redirection of it nor pipe into it (in a function's body, the standard input is then the call's); with
    piped, a pipe into it from commands that pass their input on counts as none (ssh's command: cat | bash)."""
    if last_input(cmd, 0) is not None or cmd.pipe_from is not None and not (piped and passes_on(cmd.pipe_from)):
        return False
    first = next((w for w in leading(cmd.words) if not assigns(w)), None)
    if scope is not None and first is not None and first.literal and first.text in scope.functions:
        return any(body.feeds for body in scope.functions[first.text] if body.at_calls is not None)
    for k, w in enumerate(cmd.words):
        if w.literal and (base(w) in READS_STDIN and runs_input(cmd.words[k:]) or
                          base(w) in ("sudo", "doas") and bare_shell(cmd.words[k:]) or
                          base(w) == "xargs" and any(is_python(x) or x.literal and base(x) in SHELLS | FOREIGN
                                                     for x in cmd.words[k + 1:])):
            return True
        if is_python(w):
            kind, fd = python_program(cmd.words[k:])
            if kind == "end" or kind == "stdin" and fd == 0:
                return True
        elif w.literal and os.path.basename(w.text) in SHELLS | FOREIGN:
            i, dash_c, dash_s = shell_options(cmd.words, k + 1)
            if not dash_c and (dash_s or i >= len(cmd.words) or read_fd(cmd.words[i].text) == 0):
                return True
    return False
def prepare(scope):
    """The commands of each body of a function (once) that are read at each call instead of where they stand: those that
    hold the function's arguments ("$@", and $1 to $9 when every shift of the body is counted), and those that read a
    standard input their body does not give them (also a call of a function whose body reads the call's input); each
    with the number of words that the body's shifts drop before it: shift or shift N alone at the body's top level,
    before any if, while, until, for, case or select there, and not after && or || nor in a pipeline (a shift after one
    of those is not counted: the call is read with more words then, and $1 to $9 are not read at the call)."""
    for bodies in scope.functions.values():
        for body in bodies:
            if body.at_calls is not None:
                continue
            body.at_calls, body.drops, drop, counting, uncounted = [], {}, 0, True, False
            top = min((c.depth for c in body.commands), default=0)
            for c in body.commands:
                first = c.words[0].text if c.words and c.words[0].literal and not c.words[0].quoted else None
                if counting and c.depth == top and first == "shift":
                    n = c.words[1].text if len(c.words) == 2 and c.words[1].literal else "1" if len(c.words) == 1 else ""
                    if c.unsure or not n.isdigit():
                        counting, uncounted = False, True
                    else:
                        drop += int(n)
                elif first == "shift":
                    uncounted = True
                elif counting and c.depth == top and first in COMPOUND:
                    counting = False
                body.drops[id(c)] = drop
            body.positional = not uncounted
            for c in body.commands:
                if holds_args(c) or reads_program(c) or body.positional and holds_args(c, positional=True):
                    c.at_call = True
                    body.at_calls.append((c, body.drops[id(c)]))
            body.feeds = any(reads_program(c) for c, _ in body.at_calls)
    # a call, in a body, of a function whose body reads the call's input: read at each call of that body too, so that
    # it gets that call's input (a worklist of the functions found to read it, each once: linear in the calls)
    callers = {}                                # name: (the calling function, its body, the call) for each such call
    for name, bodies in scope.functions.items():
        for body in bodies:
            for c in body.commands:
                first = next((w for w in leading(c.words) if not assigns(w)), None)
                if not c.at_call and first is not None and first.literal and first.text in scope.functions:
                    callers.setdefault(first.text, []).append((name, body, c))
    todo, grown = [name for name, bodies in scope.functions.items() if any(b.feeds for b in bodies)], {}
    while todo:
        for name, body, c in callers.get(todo.pop(), ()):
            if not c.at_call and reads_program(c, scope):
                c.at_call = True
                body.at_calls.append((c, body.drops[id(c)]))
                grown[id(body)] = body
                if not any(b.feeds for b in scope.functions[name]):
                    todo.append(name)
                body.feeds = True
    for body in grown.values():
        order = {id(x): n for n, x in enumerate(body.commands)}
        body.at_calls.sort(key=lambda pair: order[id(pair[0])])
def charge(n, where, what="calls of the script's functions (with their commands and words)"):
    """n more of the calls of a script's functions, and of the commands and words read for them, and of the lines of
    the files it writes in parts (>>) read again: at most 20000."""
    global calls
    calls += n
    if calls > 20000:
        cannot(where, f"more {what} than this check follows")
        raise Stop
def parts(f, where):
    """A text the script wrote to f in parts (>>), read whole again: its lines counted (each reading)."""
    text, _, key = wrote(f)
    if isinstance(key, tuple) and id(key) not in compiled and id(key) not in shelled:
        charge(text.count("\n") + 1, where, "lines of files written in parts and read again")
def substituted(c, args, call, where, drop=0, zero=False, positional=False):
    """The command c of a function's body (or of sh -c code given its arguments) with the words of a call (its name, or
    $0, and its arguments: args) in place of each word that is the arguments ("$@", "${@:N}", $*; with zero also $0
    to $9, with positional $1 to $9, each the word of its place), less the first drop arguments (the body's shifts),
    and with the call's redirections and pipe."""
    params = args[:1] + args[1 + drop:]
    spans = []
    for w in c.words:
        m = None if w.literal else ARGS.fullmatch(w.text)
        n = None if w.literal else ZERO.fullmatch(w.text) if zero else POS.fullmatch(w.text) if positional else None
        if m:
            first = max(int(m.group(1) or 1), 1)
            spans.append((first, first + int(m.group(2)) if m.group(2) else len(params)))
        elif n and n.re is POS and n.group(3) is not None and int(n.group(2)) >= len(params):
            spans.append(w)                     # ${1:-x} with no $1: as written (its default)
        elif n:
            k = int(n.group(1) or n.group(2))
            spans.append((k, k + 1))
        else:
            spans.append(w)
    charge(sum(1 if isinstance(s, Word) else max(min(s[1], len(params)) - s[0], 0) for s in spans), where)
    new = Command(c.line)
    for s in spans:
        new.words += [s] if isinstance(s, Word) else params[s[0]:s[1]]
    new.array, new.pipe_from = c.array, c.pipe_from or call.pipe_from
    new.redirs, new.heredocs = call.redirs + c.redirs, call.heredocs + c.heredocs
    return new
def analyse(cmd, where_of, scope, stack=()):
    """One command: the Python it runs (compiled), the shell code it runs (read), or why this cannot be read."""
    words, code, how = unwrap(cmd.words, where_of, scope, cmd.array)
    if code is not None:
        shell_code(code, how, where_of(code.line), where_of, outer=scope)
        return
    if not words:
        return
    prog = words[0]
    name = os.path.basename(prog.text)
    where = where_of(prog.line)
    if assigns(prog):                           # assignments only: no program
        return
    if cmd.array and not is_python(prog):       # an array's words run nothing (as_user=(runuser -u "$u" --)): only
        return                                  # python's code in them is compiled (RUN=(python3 -c CODE))
    if not prog.literal and ARGS.fullmatch(prog.text) and code_option(words[1:]):     # at a script's top (a function's
        cannot(where, f"{prog.text} {code_option(words[1:])}: the program is the script's arguments, which this check "
                      "cannot read")        # body reads "$@" at each call)
        return
    if prog.literal and prog.text in scope.functions and prog.text not in stack:      # a function of the script
        for body in scope.functions[prog.text]:
            charge(1 + len(body.at_calls), where)
            for c, drop in body.at_calls:
                analyse(substituted(c, words, cmd, where, drop, positional=body.positional), where_of, scope,
                        stack + (prog.text,))
        return
    if prog.text in stack and prog is next((w for w in cmd.words if not (w.literal and w.text in KEYWORDS or
                                                                        assigns(w))), None):
        return                                  # a call of a function in its own call (read there), not through
                                                # command or another program, which runs a program of that name
    if is_python(prog):
        kind, w = python_program(words)
        if kind == "c":
            if w is None:
                cannot(where, "python -c with no program")
            elif w.subst:
                cannot(where, "python -c runs a command substitution: put the Python in a file")
            elif w.only_expansion:
                cannot(where, "python -c runs what a variable holds: this check cannot read it")
            else:                               # each expansion stands for a name
                check(w.code, f"{where_of(w.line)} (python3 -c)", w.key)
        elif kind == "file" and w.procsub:
            cannot(where, "python runs a process substitution: put the Python in a file")
        elif kind == "file" and w.array:
            cannot(where, f"python's arguments come from an array ({w.text}), which this check cannot read")
        elif kind == "file":                    # a file the script wrote
            run_written(w, "python", where, python=True)
        elif kind in ("stdin", "end") and not cmd.array:
            got = program_input(cmd, w)
            if got[0] == "code":
                check(got[1], f"{where_of(got[2])} ({'python3 <<<' if isinstance(got[3], Word) else 'heredoc'})", got[3])
            elif got[0] == "file":
                if not program_file(got[1], "python", where):
                    cannot(where, f"python reads its program from {got[1].text}, which this check does not see (it "
                                  "reads a file the script wrote, and one of the repository by a literal path)")
            else:
                cannot(where, f"python reads its program from {got[1]}")
        return
    if prog.literal and name in SHELLS:
        i, dash_c, dash_s = shell_options(words, 1)
        if dash_c:
            shell_code(words[i] if i < len(words) else None, f"{name} -c", where, where_of, None, words[i + 1:], cmd,
                       scope)
        elif dash_s or i >= len(words) or read_fd(words[i].text) is not None:
            shell_input(cmd, name, 0 if dash_s or i >= len(words) else read_fd(words[i].text), where, where_of,
                        outer=scope)
        elif words[i].procsub:
            cannot(where, f"{name} runs a process substitution: put the code in a file")
        else:                                   # a script the script wrote
            run_written(words[i], name, where, outer=scope)
        return
    if prog.literal and name in RUNS_C:         # -c CODE anywhere among its words (they are permuted)
        found, w = c_code(words)
        if found:
            shell_code(w, f"{name} -c", where, where_of, outer=scope)
        else:
            shell_input(cmd, name, 0, where, where_of, outer=scope)    # no -c: its shell reads its standard input
        return
    if prog.literal and name == "sg":           # sg [-] GROUP [-c] COMMAND: sh -c COMMAND; with no COMMAND, a shell
        rest = words[2:] if len(words) > 1 and words[1].text == "-" else words[1:]
        if len(rest) > 1:
            shell_code(rest[2] if rest[1].text == "-c" and len(rest) > 2 else rest[1], "sg -c", where, where_of,
                       outer=scope)
        else:
            shell_input(cmd, name, 0, where, where_of, outer=scope)
        return
    if prog.literal and name == "trap":         # trap CODE SIGNAL...: the code runs later, in this shell
        rest = [x for x in words[1:] if not x.text.startswith("-")]
        if len(rest) > 1:
            shell_code(rest[0], "trap", where, where_of, scope, outer=scope)
        return
    if prog.literal and name == "watch":       # watch runs its arguments as shell code, or with -x as a command
        i, exec_ = 1, False
        while i < len(words) and words[i].text.startswith("-"):
            t = words[i].text
            if t == "--":
                i += 1; break
            exec_ = exec_ or t == "--exec" or not t.startswith("--") and "x" in t[1:]
            i += 2 if t in ("-n", "--interval", "-q", "--equexit") else 1
        if i < len(words) and exec_:
            new = Command(cmd.line)
            new.words = words[i:]
            analyse(new, where_of, scope, stack)
        elif i < len(words) and all(x.only_expansion or x.subst for x in words[i:]):
            cannot(where, "watch runs what a variable or a command gives, which this check cannot read")
        elif i < len(words):
            code = " ".join(resolved(x, scope) for x in words[i:])
            if stack:                           # read again at each call of a function (its words may be the call's):
                charge(len(code) // 80, where)  # counted
            shell(code, f"{where} (watch)", None if stack else words[i].key, outer=scope)
        return
    if prog.literal and name == "find":        # find ... -exec CMD ... ; (or +): CMD runs
        i = 1
        while i < len(words):
            if words[i].text in ("-exec", "-execdir", "-ok", "-okdir"):
                j = i + 1
                while j < len(words) and words[j].text not in (";", "+"):
                    j += 1
                new = Command(cmd.line)
                new.words = words[i + 1:j]
                analyse(new, where_of, scope, stack)
                i = j
            i += 1
        return
    if prog.literal and name == "eval":
        cannot(where, "eval: this check cannot read what it runs")
        return
    if prog.literal and name in ("source", "."):   # its file is read in this shell
        w = words[1] if len(words) > 1 else None
        if w is not None and w.procsub:
            cannot(where, f"{name} runs a process substitution: put the code in a file")
        elif w is not None and w.text != "-" and read_fd(w.text) is not None:
            shell_input(cmd, name, read_fd(w.text), where, where_of, scope)
        elif w is not None and not run_written(w, name, where, scope):     # a file the script wrote, or else one of
            sourced(w, name, where, scope)                                  # the repository (its functions: its own)
        return
    if prog.literal and name == "shopt" and any(w.text == "expand_aliases" for w in words) and \
            any(w.text.startswith("-") and "s" in w.text for w in words[1:]):
        cannot(where, "aliases (shopt -s expand_aliases): this check does not follow them")
        return
    if prog.literal and name in ("cat", "tee", "printf", "echo"):
        writes(cmd, words, where_of)
        return
    if prog.literal and name in DATA:
        return
    if cmd.array or prog.literal and not prog.quoted and (prog.text in COMPOUND | CLOSERS or
                                                          prog.text.startswith(("[[", "(("))):
        return                                  # the words of an array, a for, a select, a case, [[ ]] or (( )): data
    # a program this check does not follow (see the header): what names python among its words, in its input, or in a
    # file the script wrote that it is given (when it may run it or send it on) is refused; so is an expansion among
    # the words of a program that runs them as a command line (CMDLINE), or after a shell that runs code or such a
    # program among the words of another (docker run img bash -c "$CMD"); an input that this check does not see, or
    # that holds an expansion, given to a program that runs it as code (ssh with no command, or with one that reads it;
    # a shell among another program's words that reads its standard input); an input with an expansion given to a
    # program of CMDLINE that reads it; and for at and batch, a job this check does not see
    k, stdin, safe, quiet, start = (0 if name in CMDLINE else None), False, set(), False, 1
    if k is None:
        k, stdin, start = launched(words) or (None, False, None)
    if k is not None and entry(words[k]) in CMDLINE:     # (by its name, or after another program)
        quiet, safe, stdin = runs_stdin(words[k:], where)
        safe = {k + n for n in safe}
    files = k is not None or name in SENDS or any(x.literal and base(x) in SHELLS | FOREIGN for x in words[1:])
    said = mention(cmd, words, where_of, scope, files)
    x = None if start is None else next((x for n, x in enumerate(words[start:], start) if not x.literal and
                                         n not in safe), None)
    runs = words[k].text + (f" (after {name})" if k else "") if k is not None else name
    if said:
        cannot(where, f"{name} is given {said}, and this check does not follow {name}: reword it, or give it to echo, "
                      "printf or logger")
    elif x is not None:
        cannot(where_of(x.line), f"{runs} runs what it is given as a command line, and is given {x.text}, which this "
                                 "check cannot read")
    elif stdin and unseen(cmd, where_of, scope):        # (its texts are found once: see printed(), python_expansion())
        cannot(where, f"{runs} runs its standard input as code, and is given {unseen(cmd, where_of, scope)}")
    elif k is not None and entry(words[k]) in CMDLINE and not quiet and given_expansion(cmd):
        cannot(where, f"{runs} runs what it is given as a command line, and is given an input with an expansion in it, "
                      "which this check cannot read")
    elif name in JOBS and job(cmd, words, where_of, where, scope):
        cannot(where, f"{name} {job(cmd, words, where_of, where, scope)}")
def runs_stdin(words, where):
    """A program of CMDLINE (words[0]) and its words: (whether it reads no standard input, as ssh -n; the places of the
    values of its options that are safe, as ssh's but -o and -F; whether it runs its standard input as code: ssh with no
    command or with one that reads it, mosh, pdsh and clush, and csh, tcsh or fish with -s or no operand and no -c)."""
    name = entry(words[0])
    if name == "ssh":
        quiet, safe, command = ssh_layout(words)
        return quiet, safe, not quiet and (not command or reads_input(command, where))
    if name in FOREIGN:
        i, dash_c, dash_s = shell_options(words, 1)
        return False, set(), not dash_c and (dash_s or i >= len(words) or read_fd(words[i].text) == 0)
    return False, set(), name in REMOTE
def ssh_layout(words):
    """ssh's words as OpenSSH reads them (its options before and after the destination, then the command): (whether
    no shell on the other host reads ssh's standard input, as with -N or -n; where the values of its options are, but
    of -o and -F; its command's words)."""
    quiet, safe, i, dest = False, set(), 1, None
    while i < len(words):
        t = words[i].text
        if t == "--":
            i += 2 if dest is None else 1           # (the destination after --)
            break
        if words[i].literal and t.startswith("-") and len(t) > 1:
            for j, ch in enumerate(t[1:], 1):
                quiet = quiet or ch in SSH_QUIET
                if ch in SSH_VALUED:
                    if j == len(t) - 1 and i + 1 < len(words):
                        i += 1
                        if ch not in SSH_UNSAFE:
                            safe.add(i)
                    break
            i += 1
        elif dest is None:
            dest, i = i, i + 1
        else:
            break
    return quiet, safe, words[i:]
def reads_input(words, where):
    """Whether ssh's command (its words, joined as the other host's shell reads them) runs python, a shell or another
    program of READS_STDIN that reads its standard input (ssh host bash -s, sudo bash, sudo -i, cat | bash); also when
    this check cannot read that command.  Each text once (a body's command comes again at each call), and its length
    counted (see charge())."""
    text = " ".join(w.text for w in words)
    if text not in ssh_reads:
        charge(len(text) // 80, where, "characters of ssh's commands (each 80) read")
        try:
            lx = Lexer(text, "(ssh's command)")
            lx.run()
            ssh_reads[text] = any(reads_program(c, piped=True) for c in lx.commands)
        except (SyntaxError, RecursionError):
            ssh_reads[text] = True
    return ssh_reads[text]
def entry(w):
    """The base name of the program that the word w names, also as docker's --entrypoint=PROGRAM."""
    return os.path.basename(w.text[len("--entrypoint="):]) if w.text.startswith("--entrypoint=") else base(w)
def names_data(option, data):
    """Whether an option word is one of data (NAMES_PROGRAM's options of a program), also as the last of a group of
    short options (rsync -avze ssh, journalctl -xeu ssh, useradd -ms /bin/bash)."""
    return option in data or bool(re.fullmatch(r"-[A-Za-z]+", option)) and "-" + option[-1] in data
def launched(words):
    """Where, among the words of a program this check does not follow, a shell or a program of CMDLINE stands that the
    program runs: (its place, whether it reads its standard input as code, the place from which an expansion among the
    words is refused, or None), or None.  A shell counts with -c (its code is a command line: from the code on), and
    with -s, "-" or no operand (it reads its standard input; the words after are its arguments); with an operand it
    runs a file (strace bash "$script"), but a process substitution's code.  A program of CMDLINE counts by its name or
    an absolute path (not results/batch): every word after it.  Neither counts as the value of an option that names a
    program as data (NAMES_PROGRAM: rsync -e ssh, chsh -s /bin/bash, also rsync -avze ssh); a shell as docker's
    entrypoint (--entrypoint bash) runs code when a later word is an option with c (img -c "$CMD": from the word after
    it on), and else may read its standard input."""
    data = NAMES_PROGRAM.get(base(words[0]), ())
    for k, x in enumerate(words[1:], 1):
        if not x.literal or names_data(words[k - 1].text, data):
            continue
        name, after = entry(x), x.text.startswith("--entrypoint=") or words[k - 1].text == "--entrypoint"
        if name in SHELLS and after:
            c = next((j for j in range(k + 1, len(words)) if words[j].text[:1] == "-" and words[j].text[1:2] != "-"
                      and "c" in words[j].text), None)
            return (k, True, None) if c is None else (k, False, c + 1)
        if name in SHELLS:
            i, dash_c, dash_s = shell_options(words, k + 1)
            if dash_c or i < len(words) and words[i].procsub:      # (a process substitution as its file: its code)
                return k, False, i
            if dash_s or i >= len(words) or read_fd(words[i].text) == 0:
                return k, True, None
        elif name in CMDLINE and (x.text == name or x.text.startswith(("/", "--entrypoint="))):
            return k, False, k + 1
    return None
for f in sys.argv[1:]:
    compiled.clear(); shelled.clear(); written.clear(); unread.clear(); read_once.clear(); calls = 0
    pending.clear(); raws.clear(); printed_once.clear(); ssh_reads.clear()
    current = f
    try:
        with open(f, "rb") as fh:
            raw = fh.read()
    except OSError as e:
        unreadable(e)
        continue
    if raw.startswith(b"\xef\xbb\xbf"):
        cannot(f, "it starts with a byte order mark, which bash takes for part of its first word")
        continue
    try:
        text = raw.decode()
    except UnicodeDecodeError as e:
        cannot(f, f"it is not UTF-8 (at byte {e.start}), which this check reads")
        continue
    if PH in text:
        cannot(f, "it holds the character U+E000, which this check uses itself")
        continue
    try:
        lx = Lexer(text, f)
        lx.run()
        lx.scope.values, lx.scope.pynames = values(lx.commands), lx.scope.pynames | python_names(lx.commands)
        prepare(lx.scope)
    except SyntaxError as e:
        print(f"cannot read: {e}"); bad = 1; continue
    except RecursionError:
        cannot(f, "nested too deeply for this check"); continue
    try:
        for cmd in lx.commands:                 # the files of the repository that the script sources (source or .
            ws = cmd.words                      # with a literal path, not in a function nor after && or ||), read
            if not cmd.in_body and not cmd.unsure and len(ws) > 1 and ws[0].literal and not ws[0].quoted and \
                    ws[0].text in ("source", ".") and ws[1].literal:     # first: their functions are known to bodies
                sourced(ws[1], ws[0].text, f"{f}:{ws[0].line}", lx.scope)   # defined before the source line
        for cmd in lx.commands:
            try:
                if not cmd.at_call:
                    analyse(cmd, lambda line, f=f: f"{f}:{line}", lx.scope)
            except RecursionError:
                cannot(f, "nested too deeply for this check")
        for name in list(written):              # the files it wrote, as they are at its end (one that another's code
            if name in written:                 # overwrote from words this check cannot read was read then)
                settle(name)
    except Stop:
        pass
print(f"compiled {n} pieces of Python")
sys.exit(bad)
EOF

step "systemd units: their programs (every Exec line of every unit and drop-in), then systemd-analyze verify"
# systemd/ holds units (*.service, *.socket, *.mount, *.swap, *.timer, *.path), logrotate files (*.logrotate), and
# drop-in directories, systemd/<unit>.d/ with only *.conf files; anything else fails.  One parser for both: the copies
# verify reads (the live checkout's path as the tested tree, and with --repo-only a program that does not exist here as
# /bin/true; a drop-in beside its unit's copy, or, when systemd/ does not have its unit, beside a copy of the
# distribution's unit (/lib/systemd/system; what verify finds in that copy alone, as a key this systemd does not know or
# a program that is not installed, is no finding of the drop-in, and is left out), or else beside a stand-in that verify
# loads: a service runs /bin/true, a timer has OnCalendar=, a socket ListenStream= and a service, a path PathExists=, a
# mount What= (verify takes a mount's, an automount's and a swap's paths from the name); for a prefix drop-in,
# user-.slice.d, a unit it is for, user-x.slice; so verify reads the drop-in as systemd would: an unknown key or a bad
# value in it is a finding; verify reads these and the distribution's units, not the units and drop-ins of /etc and
# /run, and with --man=no, so the units' Documentation= is not checked), and the check that every program the units run
# exists here.  The distribution's units differ between this machine and CI's container, so verify's result can too: a
# Requires= on a unit that only this machine has passes here and fails in CI, and so does a drop-in that adds a second
# ExecStart= to a oneshot service that CI's container lacks (its stand-in is a simple service).  An Exec line is split
# as systemd splits it (quotes, C escapes also inside single quotes, ";" alone between commands; an empty one only
# resets, in a drop-in: one in a unit fails, though systemd and check-drift.sh take it for a reset there too), and
# checked: the program of each command, the one that a wrapper (env, nice, timeout, flock,
# sudo, runuser -u, systemd-run ...) runs, the script that python or a shell runs (python -c, -m and a program on
# standard input are refused: a unit's Python is a script), the code of sh -c (parsed by that shell; % and $ in it must
# be %% and $$, which systemd passes on as % and $, or $ only with the ":" prefix, which turns off systemd's $; python
# in it is refused), the programs that code names by a path, and every path of the live checkout among the arguments;
# python among the words of su, runuser without -u, script or a shell other than sh, bash and dash, whose code this
# check does not read in a unit, is refused.  Other Exec... keys (ExecPaths=) are no commands: verify reads them.
# A line of a unit or a drop-in that ends with a backslash that no other backslash escapes (not a comment line, whose
# first character but blanks is # or ;, nor one with blanks after its backslash) fails: systemd joins it with the next
# line, and these checks read each line alone
mkdir "$tmp/units" || exit 1
python3 - "$D" "$LIVE" "$REPO_ONLY" "$tmp/units" <<'EOF' || fail=1
import os, pathlib, re, subprocess, sys
for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(errors="backslashreplace")        # a lone surrogate (\ud800 in a unit) prints, as \ud800
D, LIVE, repo_only, out = sys.argv[1], sys.argv[2], sys.argv[3] == "1", pathlib.Path(sys.argv[4])
keys = {"ExecStart", "ExecStartPre", "ExecStartPost", "ExecCondition", "ExecReload", "ExecStop", "ExecStopPre",
        "ExecStopPost"}
PYTHON = re.compile(r"python(?:3(?:\.\d+)?)?")
search = ["/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"]   # systemd's for a bare name
UNIT_SUFFIXES = (".service", ".socket", ".mount", ".swap", ".timer", ".path")
DROPIN = re.compile(r"[A-Za-z0-9:_.\\@-]+\.(?:service|socket|mount|swap|timer|path|slice|target|automount|scope)\.d")
SHELLS = {"sh", "bash", "dash"}
# programs that run code this check does not read in a unit (su -c CODE, runuser without -u, script -c, the other
# shells): python among their words is refused
NOT_READ = {"su", "runuser", "script", "zsh", "ksh", "mksh", "ash", "csh", "tcsh", "fish"}
WRAPPERS = {   # program: (its options that take a value, the words after its options, before the command)
    "env": ({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}, 0), "nice": ({"-n", "--adjustment"}, 0),
    "ionice": ({"-c", "--class", "-n", "--classdata"}, 0), "taskset": (set(), 1),
    "timeout": ({"-k", "--kill-after", "-s", "--signal"}, 1), "setsid": (set(), 0), "chrt": (set(), 1),
    "flock": ({"-w", "--timeout", "-E", "--conflict-exit-code"}, 1), "stdbuf": ({"-i", "-o", "-e"}, 0),
    "nohup": (set(), 0), "prlimit": (set(), 0), "choom": ({"-n", "--adjust"}, 0),
    "sudo": ({"-u", "--user", "-g", "--group", "-C", "-D", "-h", "-p", "-r", "-t", "-U"}, 0),
    "time": ({"-f", "--format", "-o", "--output"}, 0),
    "setpriv": ({"--reuid", "--regid", "--groups", "--inh-caps", "--ambient-caps", "--bounding-set", "--securebits",
                 "--pdeathsig"}, 0),
    "runuser": ({"-u", "--user", "-g", "--group", "-G", "--supp-group", "-w", "--whitelist-environment"}, 0),
    "systemd-run": ({"-p", "--property", "-E", "--setenv", "-u", "--unit", "-H", "--host", "-M", "--machine",
                     "--description", "--slice", "--uid", "--gid", "--nice", "--working-directory", "--on-active",
                     "--on-boot", "--on-startup", "--on-unit-active", "--on-unit-inactive", "--on-calendar",
                     "--timer-property", "--path-property", "--socket-property"}, 0)}
ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
line_re = re.compile(r"^\s*(Exec(?:Start|StartPre|StartPost|Condition|Reload|Stop|StopPre|StopPost))\s*=(.*)$")
bad = n = skipped = 0
files, stand_ins = [], []      # (file, its copy, a drop-in); the units that a drop-in is for and systemd/ does not have
for p in sorted(pathlib.Path("systemd").iterdir()):
    if p.is_file() and p.suffix in UNIT_SUFFIXES:
        files.append((p, out / p.name, False))
    elif p.is_file() and p.suffix == ".logrotate":
        continue
    elif p.is_dir() and DROPIN.fullmatch(p.name):
        (out / p.name).mkdir()
        for q in sorted(p.iterdir()):
            if q.is_file() and q.suffix == ".conf":
                files.append((q, out / p.name / q.name, True))
            else:
                print(f"{q}: not a drop-in this check reads (a *.conf file)"); bad = 1
        if not (p.parent / p.name[:-2]).is_file():
            stand_ins.append(p.name[:-2])
    else:
        print(f"{p}: not a unit, or a drop-in directory (systemd/<unit>.d/*.conf), that this check reads"); bad = 1
DIST = (pathlib.Path("/lib/systemd/system"), pathlib.Path("/usr/lib/systemd/system"))     # the distribution's units
def stand_in(name):
    """A unit that stands in for name, for which systemd/ has drop-ins only and the distribution has no unit, which
    verify loads (a mount's Where= and a swap's What= are the path its name escapes, as verify takes it)."""
    base, kind = name.rsplit(".", 1)
    path = "/" + "/".join(re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), x)
                          for x in base.split("-") if x)
    needs = {"service": "[Service]\nExecStart=/bin/true\n", "socket": f"[Socket]\nListenStream=/run/{name}\n",
             "timer": "[Timer]\nOnCalendar=daily\n", "path": f"[Path]\nPathExists=/run/{name}\n",
             "mount": f"[Mount]\nWhat=/dev/null\nWhere={path}\n", "automount": f"[Automount]\nWhere={path}\n",
             "swap": f"[Swap]\nWhat={path}\n"}
    (out / name).write_text(f"[Unit]\nDescription=stand-in for {name}: systemd/ has only drop-ins for it\n\n"
                            f"{needs.get(kind, '')}")
for name in stand_ins:          # a prefix drop-in (user-.slice.d) is for its units: stand in for one, user-x.slice
    unit = name.replace("-.", "-x.", 1) if re.fullmatch(r"[^.]+-\.[a-z]+", name) else name
    dist = [d / unit for d in DIST if (d / unit).is_file()]
    if dist:                    # the distribution's unit (a oneshot service may have more ExecStart=): a copy of it,
        (out / unit).write_bytes(dist[0].read_bytes())          # and one in dist/, which verify reads alone too
        (out.parent / "dist").mkdir(exist_ok=True)
        (out.parent / "dist" / unit).write_bytes(dist[0].read_bytes())
        continue
    stand_in(unit)
    socket_service = unit[:-len(".socket")] + ".service"
    if unit.endswith(".socket") and not (pathlib.Path("systemd") / socket_service).is_file() and \
            socket_service not in stand_ins:
        stand_in(socket_service)                # the service a socket starts
def executable(path):
    return os.path.isfile(path) and os.access(path, os.X_OK)
def located(prog, no):
    """Where the program is here (the tested tree for one of the live checkout), or None when it is not (said)."""
    global skipped
    if not prog:
        no("a prefix with no program"); return None
    if "%" in prog:
        no(f"{prog}: a specifier in the program is not checked here"); return None
    if not prog.startswith("/"):
        found = next((f"{d}/{prog}" for d in search if executable(f"{d}/{prog}")), None)
        if repo_only and "/" not in prog:
            skipped += 1
            return found or prog
        if "/" in prog or found is None:
            no(f"{prog} is not an absolute path, and systemd's search path has no such program"); return None
        return found
    if prog.startswith(LIVE + "/"):
        here = D + prog[len(LIVE):]
        if not executable(here):
            no(f"{prog} -> {here} is missing or not executable"); return None
        return here
    if "/p100-q3-deployment" in prog:
        no(f"{prog} names a checkout other than {LIVE}"); return None
    if repo_only:
        skipped += 1
        return prog
    if not executable(prog):
        no(f"{prog} does not exist on this machine or is not executable"); return None
    return prog
def script(path, by, no):
    """A script that python or a shell runs: there (the tested tree's for one of the live checkout)."""
    global skipped
    if "%" in path:
        no(f"{path}: a specifier in the script that {by} runs is not checked here")
    elif not path.startswith("/"):
        no(f"{path}, which {by} runs, is not an absolute path: this check does not find it")
    elif path.startswith(LIVE + "/"):
        if not os.path.isfile(D + path[len(LIVE):]):
            no(f"{path} -> {D + path[len(LIVE):]} does not exist")
    elif repo_only:
        skipped += 1
    elif not os.path.isfile(path):
        no(f"{path}, which {by} runs, does not exist on this machine")
def python_script(c, no):
    """The script python runs, after its options (-W, -X and --check-hash-based-pycs take a value); None (said) for -c,
    -m or a program on its standard input: a unit's Python is a script, which this check finds."""
    i = 1
    while i < len(c) and c[i].startswith("-") and c[i] != "-":
        a = c[i]
        if a == "--":
            i += 1; break
        if a.startswith("--"):
            i += 2 if a == "--check-hash-based-pycs" else 1; continue
        for j, ch in enumerate(a[1:], 1):
            if ch in "cm":
                no(f"{c[0]} -{ch}: python in a unit runs a script here (this check does not read Python in a unit)")
                return None
            if ch in "WX":
                i += 1 if a[j + 1:] else 2
                break
        else:
            i += 1
    if i >= len(c) or c[i] == "-":
        no(f"{c[0]} reads its program from its standard input: python in a unit runs a script here"); return None
    return c[i]
def unwrap(c):
    """The command a wrapper runs (its words), the code of flock -c as ("-c", code), or None when it runs none."""
    name = os.path.basename(c[0])
    valued, positional = WRAPPERS[name]
    i = 1
    while i < len(c) and c[i].startswith("-") and c[i] != "-":
        if c[i] == "--":
            i += 1; break
        if name == "flock" and c[i] in ("-c", "--command"):
            return ("-c", c[i + 1] if i + 1 < len(c) else None)
        i += 2 if c[i] in valued else 1
    if name == "env":
        while i < len(c) and ASSIGN.match(c[i]):
            i += 1
    if name == "flock" and i + 1 < len(c) and c[i + 1] in ("-c", "--command"):
        return ("-c", c[i + 2] if i + 2 < len(c) else None)
    return c[i + positional:] or None
ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\", '"': '"', "'": "'",
           "s": " "}
def unescaped(s, j):
    """The character of the C escape whose backslash is s[j - 1], as systemd decodes it, and where the text goes on;
    an escape systemd does not know stays as written (systemd-analyze verify reports it)."""
    c = s[j:j + 1]
    if c in ESCAPES and c:
        return ESCAPES[c], j + 1
    for kind, digits, base in (("x", 2, 16), ("u", 4, 16), ("U", 8, 16)):
        if c == kind and re.fullmatch(r"[0-9A-Fa-f]{%d}" % digits, s[j + 1:j + 1 + digits]):
            v = int(s[j + 1:j + 1 + digits], base)
            if 0 < v <= 0x10FFFF:
                return chr(v), j + 1 + digits
    if re.fullmatch(r"[0-7]{3}", s[j:j + 3]) and 0 < int(s[j:j + 3], 8) <= 255:
        return chr(int(s[j:j + 3], 8)), j + 3
    return "\\" + c, j + 1
def split(line):
    """An Exec line's commands, as systemd splits them: words bare, in "..." or in '...', with C escapes (also inside
    single quotes); a ";" alone between commands (\\; is a word ";").  ValueError for a quote with no end."""
    commands, words, i = [], [], 0
    while True:
        while i < len(line) and line[i] in " \t\r\n":
            i += 1
        if i >= len(line):
            commands.append(words)
            return commands
        if line[i] == ";" and line[i + 1:i + 2] in ("", " ", "\t", "\r", "\n"):
            commands.append(words); words = []; i += 1
            continue
        if line.startswith("\\;", i) and line[i + 2:i + 3] in ("", " ", "\t", "\r", "\n"):
            words.append(";"); i += 2
            continue
        w, quote = [], None
        while i < len(line) and (quote or line[i] not in " \t\r\n"):
            c = line[i]
            if c == "\\" and i + 1 < len(line):
                ch, i = unescaped(line, i + 1)
                w.append(ch)
            elif quote is None and c in "'\"":
                quote = c; i += 1
            elif c == quote:
                quote = None; i += 1
            else:
                w.append(c); i += 1
        if quote:
            raise ValueError("No closing quotation")
        words.append("".join(w))
def sh_words(code):
    """The words of sh code, each with whether it starts a command (after ; & | ( ) or a newline; a word after < or > is
    a file): quotes and backslashes as sh reads them, and a comment only at the start of a word."""
    out, i, start, target = [], 0, True, False
    while i < len(code):
        c = code[i]
        if c in " \t":
            i += 1
        elif c in ";&|()\n":
            start, target = True, False; i += 1
        elif c in "<>":
            target = True; i += 1
        elif c == "#":
            nl = code.find("\n", i)
            i = len(code) if nl < 0 else nl
        else:
            w = []
            while i < len(code) and code[i] not in " \t;&|()<>\n":
                c = code[i]
                if c == "\\":
                    w.append(code[i + 1:i + 2].replace("\n", "")); i += 2
                elif c == "'":
                    j = code.find("'", i + 1)
                    if j < 0:
                        return out
                    w.append(code[i + 1:j]); i = j + 1
                elif c == '"':
                    j = i + 1
                    while j < len(code) and code[j] != '"':
                        if code[j] == "\\" and code[j + 1:j + 2] in ('"', "\\", "$", "`", "\n"):
                            w.append(code[j + 1].replace("\n", "")); j += 2
                        else:
                            w.append(code[j]); j += 1
                    if j >= len(code):
                        return out
                    i = j + 1
                else:
                    w.append(c); i += 1
            word = "".join(w)
            if i < len(code) and code[i] in "<>" and word.isdigit():
                continue                       # 2>file: a file descriptor, no word
            if target:
                target = False                 # the file of a redirection
                continue
            out.append((word, start))
            start = False
    return out
def shell_code(code, what, shell, no, expands=True):
    """The shell code of a unit's command: what systemd passes on (with expands, $$ is $: without the ":" prefix),
    parsed by that shell; and the programs it names by a path."""
    if code is None:
        no(f"{what} with no code"); return
    if "%" in code.replace("%%", ""):
        no(f"the shell code of {what} has a % that systemd expands (a specifier): write %% for a %")
    if expands and "$" in code.replace("$$", ""):
        no(f"the shell code of {what} has a $ that systemd may expand: write $$ for a $")
    if not expands and "$$" in code:
        no(f"the shell code of {what} has $$, which systemd passes on as $$ with the \":\" prefix: write $ for a $")
    code = code.replace("%%", "%")
    if expands:
        code = code.replace("$$", "$")
    if re.search(r"\bpython", code):
        no(f"the shell code of {what} runs python, which this check does not read in a unit: put it in a script")
    try:
        r = subprocess.run([shell, "-n", "-c", code], capture_output=True, text=True)
    except (UnicodeError, ValueError) as e:     # a lone surrogate or a NUL that an escape gives
        no(f"the shell code of {what} cannot be given to {shell}: {e.__class__.__name__}")
        return
    if r.returncode != 0:
        no(f"the shell code of {what} does not parse: {r.stderr.strip()}")
        return
    first = False                              # a program it runs by a path: the first word of each command
    for w, start in sh_words(code):
        first = first or start
        if first and (re.match(r"[A-Za-z_][A-Za-z0-9_]*=", w) or w.startswith("-") or
                      w in ("exec", "command", "nohup", "nice", "setsid", "env", "time", "builtin", "if", "then",
                            "elif", "else", "do", "while", "until", "!", "{")):
            continue                           # what comes before the program
        if first:
            first = False
            if w.startswith("/"):
                located(w, no)
units = sum(1 for _, _, dropin in files if not dropin)
for unit, dest, dropin in files:
    copy = []
    try:
        text = unit.read_text()
    except UnicodeDecodeError as e:
        print(f"{unit}: it is not UTF-8 (at byte {e.start}), which this check reads"); bad = 1
        continue
    for i, line in enumerate(text.split("\n"), 1):   # (read_text() makes \r\n and \r line ends \n, as systemd reads them)
        if not line.lstrip(" \t").startswith(("#", ";")) and (len(line) - len(line.rstrip("\\"))) % 2:
            print(f"{unit}:{i}: it ends with a backslash, so systemd joins it with the next line, which these checks do "
                  "not: put it on one line"); bad = 1
            copy.append(line)
            continue
        m = line_re.match(line)
        if not m:
            copy.append(line)
            continue
        where = f"{unit}:{i}"
        key, cmd = m.group(1), m.group(2).strip()
        n += 1
        def no(why):
            global bad
            print(f"{where}: {why}"); bad = 1
        try:
            commands = split(cmd)
        except ValueError as e:
            no(f"cannot read the command: {e}"); copy.append(line); continue
        if dropin and commands == [[]]:        # Exec...= in a drop-in: the unit's commands of that key are reset
            copy.append(line); continue
        if any(not c for c in commands):
            no(f"{key}= with no command (a reset, only for a drop-in), or an empty one between \";\"")
            copy.append(line); continue
        new = line
        for c in commands:
            prefix = re.match(r"[-@:+!]*", c[0]).group(0)
            prog = c[0][len(prefix):]
            c = [prog] + (c[2:] if "@" in prefix else c[1:])     # @prog argv0 args: the program is the first word
            here = located(prog, no)
            if here is None:
                continue
            if repo_only and prog.startswith("/") and not prog.startswith(LIVE + "/") and not executable(prog):
                new = new.replace(prog, "/bin/true", 1)
            for a in c[1:]:                    # a path of the live checkout in an argument: the tested tree's
                for p in re.findall(re.escape(LIVE) + r"(/[^\s'\":;,]*)", a):
                    if not os.path.exists(D + p):
                        no(f"{LIVE}{p} -> {D}{p} does not exist")
            while os.path.basename(c[0]) in WRAPPERS and not (os.path.basename(c[0]) == "runuser" and not any(
                    a in ("-u", "--user") or a.startswith("--user=") for a in c)):   # runuser USER -c CODE: as su
                r = unwrap(c)
                if r is None:
                    no(f"{c[0]} runs no command"); break
                if isinstance(r, tuple):
                    shell_code(r[1], f"{c[0]} -c", "sh", no, ":" not in prefix); break
                c, here = r, located(r[0], no)
                if here is None:
                    break
            else:
                base = os.path.basename(c[0])
                if PYTHON.fullmatch(base):     # python's program: a script, there
                    path = python_script(c, no)
                    if path is not None:
                        script(path, c[0], no)
                elif base in SHELLS:           # sh -c CODE (options before the code or after -c), or sh SCRIPT
                    j, dash_c, dash_s = 1, False, False
                    while j < len(c) and c[j][:1] in "-+" and len(c[j]) > 1:
                        if c[j] in ("-o", "+o", "-O", "+O"):
                            j += 2; continue
                        if c[j] == "--":
                            j += 1; break
                        if c[j][0] == "-" and not c[j].startswith("--"):
                            dash_c, dash_s = dash_c or "c" in c[j][1:], dash_s or "s" in c[j][1:]
                        j += 1
                    if dash_c:
                        shell = here if executable(here) else base
                        shell_code(c[j] if j < len(c) else None, f"{c[0]} -c", shell, no, ":" not in prefix)
                    elif dash_s or j >= len(c) or c[j] == "-":
                        no(f"{c[0]} reads its commands from its standard input: give it -c CODE or a script")
                    else:
                        script(c[j], c[0], no)
                elif base in NOT_READ and any(re.search(r"\bpython", a) for a in c[1:]):
                    no(f"{c[0]} runs python, which this check does not follow in a unit: put it in a script")
        copy.append(new.replace(LIVE + "/", D + "/"))
    dest.write_text("\n".join(copy))
note = f" (--repo-only: {skipped} outside the repo not checked)" if repo_only else ""
dropins = f" and {len(files) - units} drop-ins" if len(files) > units else ""
print(f"read {n} Exec lines in {units} units{dropins}{note}")
sys.exit(bad)
EOF
# the units (verify reads a drop-in directory beside its unit), and the distribution's units they need (targets,
# slices): not the units and drop-ins of /etc and /run, which differ from machine to machine (the distribution's units
# differ too, between this machine and CI's container).  --man=no: the units' Documentation= is not checked (CI's
# container has no man).  What verify finds in a copy of a distribution's unit alone (a key this systemd does not know,
# a program that is not installed here) is no finding of the drop-in beside it: those lines are left out, and when that
# copy alone fails verify, verify's failure passes only if it printed nothing else
units=(); for f in "$tmp"/units/*; do [ -f "$f" ] && units+=("$f"); done
dist=(); for f in "$tmp"/dist/*; do [ -f "$f" ] && dist+=("$f"); done
# keep the lines about these units: some start with the file path, some with the unit name ("x.service: Command …"),
# a template's with an instance's (foo@i.service: ...)
pat=(-e "$tmp/units/"); for f in "${units[@]}"; do
  f=${f##*/}; pat+=(-e "$f: "); [[ $f == *@* ]] && pat+=(-e "${f%%@*}@")
done
if [ ${#units[@]} = 0 ]; then echo "systemd-analyze verify: no units in systemd/"
else
  path=/lib/systemd/system:/usr/lib/systemd/system
  raw=$(SYSTEMD_UNIT_PATH=$path systemd-analyze verify --man=no "${units[@]}" 2>&1); rc=$? base= brc=0
  if [ ${#dist[@]} != 0 ]; then
    base=$(SYSTEMD_UNIT_PATH=$path systemd-analyze verify --man=no "${dist[@]}" 2>&1); brc=$?
    base=${base//"$tmp/dist/"/"$tmp/units/"}
  fi
  extra=$(grep -avxF -f <(printf '%s\n' "$base") <<< "$raw")       # what the distribution's copies alone do not give
  out=$(grep -aF "${pat[@]}" <<< "$extra")
  if [ -n "$out" ]; then echo "${out//"$tmp/units/"/systemd/}"; fail=1
  elif [ $rc != 0 ] && { [ $brc = 0 ] || [ -n "$extra" ]; }; then
    echo "systemd-analyze verify exited with $rc:"; tail -n 5 <<< "$extra"; fail=1
  else echo "systemd-analyze verify: no findings for systemd/*"; fi
fi

echo
if [ $fail = 0 ]; then echo "all checks passed"; else echo "FAILED"; fi
exit $fail
