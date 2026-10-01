# The choice operator `?` in this CPython build

## 1. What this build is and is not

This is a Python build that supports a Curry-style choice operator, written
`?`. A functional logic core is NOT implemented. The build contains only the
syntactic extension needed to parse `?`, the operator dispatch protocol
(`__choice__` and `__rchoice__`), and an inert node type
(`types.ChoiceType`). A library implementor must implement the
functional-logic core: search, evaluation strategy, laziness, and failure.
Everything else is stock CPython 3.14.7.

Drop-in guarantee: every valid stock program runs unchanged and means the
same thing. Every `cp314` wheel and every `abi3` wheel installs and imports
unchanged, and so does every conda-forge package built for
`python 3.14.* *_cp314`. The only stock behaviour that changes is that `?`
was a SyntaxError.

## 2. Detecting the build at runtime

```python
>>> import sys, types, importlib.util
>>> hasattr(types, "ChoiceType")      # False on stock CPython
True
>>> sys.version_info[:3], sys.implementation.cache_tag, sys.abiflags
((3, 14, 7), 'cpython-314', '')
>>> importlib.util.MAGIC_NUMBER       # stock 3.14 writes b'+\x0e\r\n' (3627)
b'\xfb\x15\r\n'
>>> int.from_bytes(importlib.util.MAGIC_NUMBER[:2], "little")
5627
```

- Use `hasattr(types, "ChoiceType")`. `sys.version_info`,
  `sys.implementation`, `sysconfig` tags such as `EXT_SUFFIX`
  (`.cpython-314-x86_64-linux-gnu.so`) and `SOABI`, and the wheel tags are
  stock and cannot tell the builds apart. `sys.version`,
  `platform.python_branch()`, `python_revision()`, and `python_build()`
  carry the git identifier of a build from a checkout (`heads/choice/3.14`);
  the conda package, built from the release tarball, carries no branch name,
  like every conda-forge build. Do not rely on them.
- The conda package build string ends in `_choice_cp314`, for example
  `python 3.14.7 hcd007b5_106_choice_cp314` in `conda list`.
- `importlib.util.MAGIC_NUMBER` encodes 5627, stock 3627 + 2000. Stock
  `.pyc` files still load (section 8).
- Stock CPython cannot import a module that contains `?`. Put `?` behind
  the check, in a separate module or in a string passed to `compile`.

## 3. Syntax

`?` is a binary operator. It binds looser than `or` and tighter than the
conditional expression and `lambda`. The rule sits between `expression` and
`disjunction`, is right-recursive, and so right-associative:
`a ? b ? c` is `a ? (b ? c)`. `??`, `?=`, and `?.` are not tokens.

    expression: choice 'if' choice 'else' expression | choice | lambdef
    choice:     disjunction '?' choice | disjunction

Precedence, from tightest to loosest, around `?`:

| operator            | example             | parses as             |
|---------------------|---------------------|-----------------------|
| `await x`           | `await a ? b`       | `(await a) ? b`       |
| comparisons         | `a ? b == c`        | `a ? (b == c)`        |
| `not x`             | `not a ? b`         | `(not a) ? b`         |
| `and`               | `a and b ? c`       | `(a and b) ? c`       |
| `or`                | `a ? b or c`        | `a ? (b or c)`        |
| `?`                 | `1 ? 2 ? 3`         | `1 ? (2 ? 3)`         |
| `if` -- `else`      | `a ? b if c else d` | `(a ? b) if c else d` |
|                     | `a if c else d ? e` | `a if c else (d ? e)` |
|                     | `a if b ? c else d` | `a if (b ? c) else d` |
| `lambda`            | `lambda: a ? b`     | `lambda: (a ? b)`     |
|                     | `a ? lambda: b`     | SyntaxError           |

The exact parses, with `ast.dump(..., annotate_fields=False)`:

```python
>>> import ast
>>> def parse(src):
...     return ast.dump(ast.parse(src, mode="eval").body, annotate_fields=False)
>>> parse("await a ? b")               # await takes a primary: write await (a ? b)
"BinOp(Await(Name('a', Load())), Choice(), Name('b', Load()))"
>>> parse("a ? b == c")
"BinOp(Name('a', Load()), Choice(), Compare(Name('b', Load()), [Eq()], [Name('c', Load())]))"
>>> parse("not a ? b")
"BinOp(UnaryOp(Not(), Name('a', Load())), Choice(), Name('b', Load()))"
>>> parse("a ? b if c else d")
"IfExp(Name('c', Load()), BinOp(Name('a', Load()), Choice(), Name('b', Load())), Name('d', Load()))"
>>> parse("a if c else d ? e")
"IfExp(Name('c', Load()), Name('a', Load()), BinOp(Name('d', Load()), Choice(), Name('e', Load())))"
>>> parse("1 ? 2 ? 3")
'BinOp(Constant(1), Choice(), BinOp(Constant(2), Choice(), Constant(3)))'
>>> parse("lambda: a ? b")
"Lambda(arguments(), BinOp(Name('a', Load()), Choice(), Name('b', Load())))"
>>> parse("a ? lambda: b")          # write a ? (lambda: b)
Traceback (most recent call last):
  ...
SyntaxError: invalid syntax
```

`?` is allowed everywhere an `expression` is allowed: lambda bodies, call
arguments and keyword arguments, comprehension elements, f-string and
t-string replacement fields, subscripts and slice bounds, decorators,
annotations, the right-hand side of `:=`, `return` and `yield` values,
`assert`, `match` subjects, and `type` alias values. All of these round-trip
through `ast.unparse`. The operand of `await` is a `primary`, not an
`expression`: `await a ? b` is `(await a) ? b`; write `await (a ? b)` to
await the node. What is a SyntaxError:

```python
>>> def check(src):
...     try:
...         compile(src, "<s>", "exec")
...     except SyntaxError as e:
...         print(f"{src!r}: {e.msg} (offset {e.offset})")
>>> for src in ["a ?", "? a", "a ?? b", "a ?= b", "a ?. b", "a ? lambda: b",
...             "a ? b = 1", "[x for x in a ? b]"]:
...     check(src)
'a ?': invalid syntax (offset 4)
'? a': invalid syntax (offset 1)
'a ?? b': invalid syntax (offset 4)
'a ?= b': invalid syntax (offset 4)
'a ?. b': invalid syntax (offset 4)
'a ? lambda: b': invalid syntax (offset 5)
'a ? b = 1': cannot assign to expression (offset 1)
'[x for x in a ? b]': invalid syntax (offset 15)
>>> check("match x:\n    case a ? b:\n        pass")
'match x:\n    case a ? b:\n        pass': invalid syntax (offset 12)
```

- No unary `?`, no augmented assignment `?=`, no `?` in assignment or
  deletion targets, no `?` in `match` patterns.
- The iterable and the conditions of a comprehension take a `disjunction`:
  `[x for x in a ? b]` and `[x for x in y if a ? b]` are errors. Write
  `[x for x in (a ? b)]`. The element may use `?` freely.
- Two stock hints are less specific when `?` appears: `x = a ? b if c` says
  "invalid syntax" instead of "expected 'else' after 'if' expression", and
  `[a ? b c]` loses the "forgot a comma" hint. No message is wrong.
- `?` inside strings, comments, and format specs is unchanged.

## 4. Semantics

`a ? b` evaluates `a`, then `b`, eagerly and left to right. Then the
interpreter calls `PyNumber_Choice(a, b)`, which dispatches like every other
binary operator:

1. Let `L = type(a)` and `R = type(b)`.
2. If `R` is a proper subclass of `L` and `R` defines `__rchoice__`
   differently from `L`, call `b.__rchoice__(a)` first. A result other than
   `NotImplemented` is the answer; otherwise the reflected method is not
   tried again.
3. If `L` defines `__choice__`, call `a.__choice__(b)`. A result other than
   `NotImplemented` is the answer.
4. If `R` is not `L`, `R` defines `__rchoice__`, and step 2 did not run,
   call `b.__rchoice__(a)`. A result other than `NotImplemented` is the
   answer.
5. Otherwise the answer is `types.ChoiceType(a, b)`.

Step 5 is the only difference from `+`, `*` and friends: the end of the
chain builds a node instead of raising `TypeError`. The precedent is `==`,
which falls back to identity instead of raising. `NotImplemented` keeps its
stock meaning, "defer"; a type that wants `?` to fail must raise itself.
Lookup uses the type, never the instance. Exceptions raised inside
`__choice__` or `__rchoice__` propagate unchanged. When both operands have
the same type, `__rchoice__` is never called. No evaluation, flattening,
laziness, or search happens: the node holds the two operands and nothing
else.

```python
>>> class Alt:
...     def __choice__(self, other): return ("Alt.__choice__", other)
...     def __rchoice__(self, other): return ("Alt.__rchoice__", other)
>>> Alt() ? 1
('Alt.__choice__', 1)
>>> 1 ? Alt()
('Alt.__rchoice__', 1)
>>> 1 ? 2                        # neither int handles it: step 5
ChoiceType(1, 2)
>>> class Base:
...     def __choice__(self, other): return "Base.__choice__"
...     def __rchoice__(self, other): return "Base.__rchoice__"
>>> class Sub(Base):
...     def __rchoice__(self, other): return "Sub.__rchoice__"
>>> Base() ? Sub()               # subclass on the right overrides __rchoice__: step 2
'Sub.__rchoice__'
>>> Base() ? Base()              # same type: __rchoice__ is never tried
'Base.__choice__'
>>> class Defer:
...     def __choice__(self, other): return NotImplemented
...     def __rchoice__(self, other): return NotImplemented
>>> d = Defer()
>>> node = d ? 1
>>> type(node).__name__, node.lhs is d, node.rhs
('ChoiceType', True, 1)
>>> class Boom:
...     def __choice__(self, other): raise ValueError("no choice today")
>>> Boom() ? 1
Traceback (most recent call last):
  ...
ValueError: no choice today
```

## 5. `types.ChoiceType` reference

`types.ChoiceType` is a final, immutable, GC-tracked C type: the type of the
node that step 5 builds. Libraries and tests can build nodes by hand.

- Constructor `ChoiceType(lhs, rhs)`: exactly two positional arguments, no
  keywords. `lhs` and `rhs` are read-only (`AttributeError: readonly
  attribute`); there is no `__dict__`.
- Equality and hash are by identity. `bool()` raises `TypeError`, so
  `if a ? b:` raises instead of taking a branch.
- `repr` is constructor style. `1 ? 2 ? 3` is `ChoiceType(1, ChoiceType(2, 3))`
  and `(1 ? 2) ? 3` is `ChoiceType(ChoiceType(1, 2), 3)`. Nothing flattens.
- `__match_args__` is `("lhs", "rhs")`: positional and keyword patterns work.
- Pickle works at every protocol through `__reduce__`, which returns
  `(ChoiceType, (lhs, rhs))`; `__getnewargs__` returns `(lhs, rhs)`.
  `copy.copy` and `copy.deepcopy` rebuild nodes. Weak references work.
- Not subclassable. GC-tracked, because operands may form cycles.
- Never constant-folded: every evaluation of `a ? b` creates a new node, so
  identity is the choice identifier a library can key on. No node ever
  lands in `co_consts` or in a `.pyc`.
- No `__choice__` (the dispatch nests nodes without help), no arithmetic,
  no `__iter__`, `__len__`, `__getitem__`, or ordering.

```python
>>> import copy, pickle, weakref, gc
>>> n = types.ChoiceType(1, 2)
>>> n, n.lhs, n.rhs
(ChoiceType(1, 2), 1, 2)
>>> types.ChoiceType(1)
Traceback (most recent call last):
  ...
TypeError: ChoiceType expected 2 arguments, got 1
>>> n == types.ChoiceType(1, 2), n == n, hash(n) == object.__hash__(n)
(False, True, True)
>>> if 1 ? 2:
...     pass
Traceback (most recent call last):
  ...
TypeError: the truth value of a ChoiceType is ambiguous
>>> 1 ? 2 ? 3, (1 ? 2) ? 3
(ChoiceType(1, ChoiceType(2, 3)), ChoiceType(ChoiceType(1, 2), 3))
>>> match 1 ? 2 ? 3:
...     case types.ChoiceType(a, types.ChoiceType(b, c)):
...         print("positional:", a, b, c)
positional: 1 2 3
>>> match 1 ? 2:
...     case types.ChoiceType(lhs=l, rhs=r):
...         print("keyword:", l, r)
keyword: 1 2
>>> n.__reduce__(), n.__getnewargs__()
((<class 'types.ChoiceType'>, (1, 2)), (1, 2))
>>> [pickle.loads(pickle.dumps(n, p)) for p in range(pickle.HIGHEST_PROTOCOL + 1)]
[ChoiceType(1, 2), ChoiceType(1, 2), ChoiceType(1, 2), ChoiceType(1, 2), ChoiceType(1, 2), ChoiceType(1, 2)]
>>> copy.copy(n) is n, copy.deepcopy(1 ? [2]), weakref.ref(n)() is n, gc.is_tracked(n)
(False, ChoiceType(1, [2]), True, True)
>>> class X(types.ChoiceType): pass
Traceback (most recent call last):
  ...
TypeError: type 'types.ChoiceType' is not an acceptable base type
>>> def f():
...     return 1 ? 2
>>> f() is f(), any(type(c) is types.ChoiceType for c in f.__code__.co_consts)
(False, False)
```

## 6. Writing a library on top

(a) A class that owns the operator. Implement `__choice__` and `__rchoice__`
on your base class. Build your own node for operands you understand and
return `NotImplemented` for the rest, so that the other operand gets its
turn and unknown pairs end in the inert fallback node:

```python
>>> class Node:
...     def __choice__(self, other):
...         if isinstance(other, (Node, int)):
...             return Alt(self, other)
...         return NotImplemented
...     def __rchoice__(self, other):
...         if isinstance(other, int):
...             return Alt(other, self)
...         return NotImplemented
>>> class Lit(Node):
...     def __init__(self, value): self.value = value
...     def __repr__(self): return f"Lit({self.value})"
>>> class Alt(Node):
...     def __init__(self, lhs, rhs): self.lhs, self.rhs = lhs, rhs
...     def __repr__(self): return f"Alt({self.lhs!r}, {self.rhs!r})"
>>> Lit(1) ? Lit(2) ? Lit(3), 0 ? Lit(1)
(Alt(Lit(1), Alt(Lit(2), Lit(3))), Alt(0, Lit(1)))
>>> Lit(1) ? "x"                 # declined on both sides: fallback node
ChoiceType(Lit(1), 'x')
```

A `types.ChoiceType` node is itself a possible operand. It defines neither
method, so the other operand decides alone; `Node` above declines it, and a
parenthesised plain-value choice yields a mixed tree:

```python
>>> (1 ? 2) ? Lit(3)             # Lit.__rchoice__(ChoiceType(1, 2)) declines
ChoiceType(ChoiceType(1, 2), Lit(3))
>>> Lit(1) ? (2 ? 3)
ChoiceType(Lit(1), ChoiceType(2, 3))
>>> 1 ? 2 ? Lit(3)               # right-associative: 2 ? Lit(3) is built first
Alt(1, Alt(2, Lit(3)))
```

For uniform trees, claim `types.ChoiceType` in both methods, for example
`if type(other) is types.ChoiceType: return Alt(self, lift(other))`, where
`lift` rebuilds the node's `lhs` and `rhs` as your own nodes by walking them
as in (b).

(b) A function that accepts raw `ChoiceType` trees. Plain values build
`ChoiceType` trees. Walk `lhs` and `rhs` recursively and convert the leaves:

```python
>>> def alternatives(tree):
...     """Return the leaves of a ChoiceType tree, left to right."""
...     if type(tree) is types.ChoiceType:
...         return alternatives(tree.lhs) + alternatives(tree.rhs)
...     return [tree]
>>> alternatives(1 ? 2 ? 3), alternatives((1 ? 2) ? (3 ? 4)), alternatives(5)
([1, 2, 3], [1, 2, 3, 4], [5])
```

(c) Call-time choice. Each evaluation of `?` creates a new node. A node
passed twice is one choice; two evaluations are two choices. Key on the
node, not on its operands:

```python
>>> def same(a, b):
...     return a is b
>>> x = 0 ? 1
>>> same(x, x), same(0 ? 1, 0 ? 1)
(True, False)
```

(d) NumPy and other extension types. No `PyNumberMethods` slot exists for
`?`, so a stock-built extension type never sees the operator. A NumPy scalar
or array on either side ends up in the fallback node unless your type claims
it:

```python
>>> import numpy as np
>>> node = np.arange(3) ? 1
>>> type(node).__name__, node.lhs, node.rhs
('ChoiceType', array([0, 1, 2]), 1)
>>> Lit(1) ? np.int64(2)         # Node accepts int only; np.int64 is not an int
ChoiceType(Lit(1), np.int64(2))
>>> class NumNode(Node):
...     def __repr__(self): return "NumNode()"
...     def __rchoice__(self, other):
...         if isinstance(other, (int, np.generic, np.ndarray)):
...             return Alt(other, self)
...         return NotImplemented
>>> np.arange(2) ? NumNode()      # ndarray has no __choice__; __rchoice__ claims it
Alt(array([0, 1]), NumNode())
>>> type(np.arange(2) ? np.arange(2)).__name__   # never elementwise
'ChoiceType'
```

## 7. For tool authors

- Tokenizer: `token.QUESTION` is the exact type of `?`, and
  `token.EXACT_TOKEN_TYPES['?'] is token.QUESTION`. `tokenize` reports it as
  `OP` with `exact_type == QUESTION`, like every operator. `token.OP` and
  every later value moved up by one; `N_TOKENS` is 70.
- AST: `a ? b` is `ast.BinOp(left=a, op=ast.Choice(), right=b)`. `ast.Choice`
  is a subclass of `ast.operator` with no fields; there is no new node class.
  `ast.unparse` and the C unparser (`from __future__ import annotations`)
  render `?` with the right precedence and parentheses. For `x: a ? b`,
  `annotationlib` `Format.STRING` gives `'a ? b'`. `Format.FORWARDREF`
  gives the node when `a` and `b` resolve, and otherwise
  `ForwardRef('a ? b')`, whose `__forward_arg__` is `'a ? b'` and whose
  `.evaluate()` builds the node once they do.
- Bytecode: no new opcode. `?` compiles to `BINARY_OP` with oparg
  `NB_CHOICE = 27`; `opcode._nb_ops[27] == ("NB_CHOICE", "?")` and `dis`
  shows `BINARY_OP 27 (?)`. The flowgraph optimizer never folds it.
- A hand-built `ast.AugAssign` with `ast.Choice` fails to compile with
  `ValueError("choice operator cannot be used in augmented assignment")`.
  A hand-built `ast.BinOp` with `ast.Choice` compiles.

```python
>>> import token, tokenize, io, dis, opcode, annotationlib
>>> token.tok_name[token.QUESTION], token.EXACT_TOKEN_TYPES['?'] is token.QUESTION
('QUESTION', True)
>>> [(token.tok_name[t.type], token.tok_name[t.exact_type], t.string)
...  for t in tokenize.generate_tokens(io.StringIO("a ? b").readline)][:3]
[('NAME', 'NAME', 'a'), ('OP', 'QUESTION', '?'), ('NAME', 'NAME', 'b')]
>>> ast.unparse(ast.parse("(a ? b) ? c")), ast.unparse(ast.parse("a ? (b ? c)"))
('(a ? b) ? c', 'a ? b ? c')
>>> def h(x: a ? b): pass
>>> annotationlib.get_annotations(h, format=annotationlib.Format.STRING)
{'x': 'a ? b'}
>>> def k(x: p ? q): pass          # p and q are undefined
>>> annotationlib.get_annotations(k, format=annotationlib.Format.FORWARDREF)['x'].__forward_arg__
'p ? q'
>>> opcode._nb_ops[27]
('NB_CHOICE', '?')
>>> dis.dis(compile("a ? b", "", "eval"))
  0           RESUME                   0
<BLANKLINE>
  1           LOAD_NAME                0 (a)
              LOAD_NAME                1 (b)
              BINARY_OP               27 (?)
              RETURN_VALUE
```

## 8. Compatibility notes

- Bytecode cache, both directions. The fork writes `.pyc` files with magic
  5627 and accepts 3627 as well, so a tree compiled by stock CPython imports
  without a recompile and without a rewrite. Stock CPython treats a fork
  `.pyc` as a bad magic number and recompiles from source: a `?`-free module
  is rewritten with the stock magic and works; a module that uses `?` raises
  `SyntaxError` on stock. Observed with both interpreters on one directory:

      stock  -c 'import plain'        -> writes plain.cpython-314.pyc, header b'+\x0e\r\n'
      fork   -c 'import plain'        -> loads it; header still b'+\x0e\r\n'
      fork   -c 'import uses_choice'  -> ChoiceType(1, 2); header b'\xfb\x15\r\n'
      stock  -c 'import uses_choice'  -> SyntaxError: invalid syntax (at the "?")
      stock  -c 'import plain'        -> after a fork compile: header back to b'+\x0e\r\n'

  `compileall`, `py_compile`, `pydoc.importfile`, `pkgutil.read_code`,
  `runpy.run_path`, `zipimport`, and `python file.pyc` accept both headers
  on the fork. `sys.implementation.cache_tag` stays `cpython-314`, so both
  interpreters share one `__pycache__` name per module; whichever compiled
  last wins, and neither crashes on the other's file.
- No new slot in `PyNumberMethods`, no `PyTypeObject` change, no new
  `Py_TPFLAGS` bit, no limited-API or stable-ABI change. Extension modules
  built against stock 3.14 load unchanged, `abi3` wheels too.
- Public C API: one new function, `PyObject *PyNumber_Choice(PyObject *o1,
  PyObject *o2)`, declared in `Include/cpython/abstract.h` (not in the
  limited API). `Include/opcode.h` gains `NB_CHOICE 27`, and `NB_OPARG_LAST`
  becomes 27. Nothing else in the public headers changes.
- No `operator.choice`, no new stdlib module, no `__future__` feature, and
  no `ast.parse(..., feature_version=...)` gate: `?` is always on.
- Packaging tags, `SOABI`, `EXT_SUFFIX`, and `abiflags` are stock;
  `sys.version` and `platform` differ only in the build identifier that
  every CPython build embeds (section 2). The conda package depends on
  `python_abi 3.14.* *_cp314`, so conda-forge packages for `cp314` resolve
  against it.

## 9. Known gaps

- `weakref.proxy` and `unittest.mock.MagicMock` do not forward `?`: they
  have no `__choice__`, so `proxy ? x` and `MagicMock() ? x` build a
  `ChoiceType` node instead of reaching the referent or recording a call.
- The `pydoc` topic texts are stock: `help("OPERATORS")` does not list `?`.
  The Sphinx documentation in `Doc/` is updated.
- No augmented assignment `?=`.
- Comprehension iterables and conditions need parentheses around `?`, and
  two error hints are less specific (section 3).
- Built and tested on Linux x86_64 only. The Windows launcher does not know
  the fork's magic number.

## 10. Where the rest lives

- The patch series is branch `choice/3.14` of `github.com/Andy-Jost/cpython`:
  23 commits on tag `v3.14.7`, 18 hand-edited (17 source and test commits
  plus the one that adds this file) and 5 that hold regenerated files
  (`regen:` subjects). This file sits at the root of that branch. The tests
  are `Lib/test/test_choice.py` (100 tests). The documentation changes are
  in `Doc/reference/expressions.rst` (choice expression, precedence table),
  `Doc/reference/datamodel.rst` (`__choice__`, `__rchoice__`, the end of the
  chain), `Doc/c-api/number.rst` (`PyNumber_Choice`), and
  `Doc/library/types.rst` (`ChoiceType`), with short entries in
  `Doc/library/ast.rst`, `dis.rst`, `token.rst`, `Doc/data/refcounts.dat`
  (the `PyNumber_Choice` rows), and `Doc/reference/lexical_analysis.rst`
  (where `?` moves from the error-character list to `other_op`).
- The companion repository `github.com/Andy-Jost/cpython-choice` (private
  at the time of writing; ask the owner for access) holds the design
  (`docs/design.md`), the implementation plan with its decisions
  (`docs/plan.md`), the run report (`docs/REPORT.md`), the porting guide
  (`docs/PORTING.md`), the reproduction guide (`docs/REPRODUCE.md`), a copy
  of this file (`docs/CHOICE_OPERATOR.md`), the exported patches
  (`patches/`), the conda recipe (`feedstock/`), and the compatibility and
  smoke tests (`tests/`).
