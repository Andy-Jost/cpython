"""Tests for the choice operator ``?`` and types.ChoiceType.

Agent-authored (Claude); not yet human-reviewed.
"""

import ast
import copy
import dis
import importlib
import importlib.machinery
import importlib.util
import inspect
import io
import marshal
import opcode
import os
import pickle
import pkgutil
import py_compile
import re
import sys
import textwrap
import token
import tokenize
import types
import unittest
import weakref

import _imp
import annotationlib
from importlib import _bootstrap_external

from test import support
from test.support import (check_syntax_error, gc_collect, import_helper,
                          os_helper, script_helper)


def dump(src):
    """Return ast.dump() of the expression *src*."""
    return ast.dump(ast.parse(src, mode="eval").body)


class TokenizerTests(unittest.TestCase):

    def test_exact_token_type(self):
        self.assertIs(token.EXACT_TOKEN_TYPES['?'], token.QUESTION)
        self.assertEqual(token.tok_name[token.QUESTION], 'QUESTION')
        # Values below OP are reported as OP by the tokenize module.
        self.assertLess(token.QUESTION, token.OP)
        self.assertEqual(token.N_TOKENS, 70)

    def test_tokenize_module(self):
        toks = list(tokenize.tokenize(io.BytesIO(b'a ? b\n').readline))
        ops = [t for t in toks if t.type == token.OP]
        self.assertEqual(len(ops), 1)
        tok = ops[0]
        self.assertEqual(tok.string, '?')
        self.assertEqual(tok.exact_type, token.QUESTION)
        self.assertEqual(tok.start, (1, 2))
        self.assertEqual(tok.end, (1, 3))

    def test_generate_tokens_str_input(self):
        toks = list(tokenize.generate_tokens(io.StringIO('a ? b\n').readline))
        self.assertEqual([(t.type, t.string, t.exact_type) for t in toks[:3]],
                         [(token.NAME, 'a', token.NAME),
                          (token.OP, '?', token.QUESTION),
                          (token.NAME, 'b', token.NAME)])

    def test_untokenize_roundtrip(self):
        source = b'x = a ? b ? c\n'
        toks = list(tokenize.tokenize(io.BytesIO(source).readline))
        self.assertEqual(tokenize.untokenize(toks), source)
        # Compatibility mode (2-tuples) keeps the operator as well.
        toks = list(tokenize.generate_tokens(io.StringIO('a ? b\n').readline))
        compat = tokenize.untokenize([(t.type, t.string) for t in toks])
        self.assertIn('?', compat)
        retoks = list(tokenize.generate_tokens(io.StringIO(compat).readline))
        self.assertEqual([(t.type, t.string) for t in retoks],
                         [(t.type, t.string) for t in toks])

    def test_format_spec_fill_unaffected(self):
        # '?' as a format-spec fill character goes through FSTRING_MIDDLE.
        self.assertEqual(format(5, '?>4'), '???5')
        self.assertEqual(f'{5:?<4}', '5???')
        self.assertEqual('{:?^5}'.format(1), '??1??')

    def test_question_in_strings_and_comments(self):
        toks = list(tokenize.generate_tokens(
            io.StringIO("'a?b'  # ?\n").readline))
        kinds = [(token.tok_name[t.type], t.string) for t in toks[:2]]
        self.assertEqual(kinds, [('STRING', "'a?b'"), ('COMMENT', '# ?')])
        self.assertNotIn(token.OP, [t.type for t in toks])


class GrammarTests(unittest.TestCase):

    def assertSame(self, a, b):
        self.assertEqual(dump(a), dump(b))

    def assertDifferent(self, a, b):
        self.assertNotEqual(dump(a), dump(b))

    def test_binop_choice_dump(self):
        self.assertEqual(
            dump("a ? b"),
            "BinOp(left=Name(id='a', ctx=Load()), op=Choice(), "
            "right=Name(id='b', ctx=Load()))")

    def test_right_associative(self):
        self.assertSame("1 ? 2 ? 3", "1 ? (2 ? 3)")
        self.assertDifferent("1 ? 2 ? 3", "(1 ? 2) ? 3")
        node = ast.parse("1 ? 2 ? 3", mode="eval").body
        self.assertEqual(node.left.value, 1)
        self.assertIsInstance(node.right, ast.BinOp)
        self.assertIsInstance(node.right.op, ast.Choice)

    def test_precedence_or(self):
        self.assertSame("a or b ? c", "(a or b) ? c")
        self.assertSame("a ? b or c", "a ? (b or c)")
        self.assertSame("a and b ? c or d", "(a and b) ? (c or d)")

    def test_precedence_not(self):
        self.assertSame("not a ? b", "(not a) ? b")
        self.assertDifferent("not a ? b", "not (a ? b)")

    def test_precedence_comparison(self):
        self.assertSame("a ? b == c", "a ? (b == c)")
        self.assertSame("a == b ? c", "(a == b) ? c")
        self.assertSame("a ? b in c", "a ? (b in c)")

    def test_precedence_arith(self):
        self.assertSame("a ? b + c", "a ? (b + c)")
        self.assertSame("a + b ? c", "(a + b) ? c")
        self.assertSame("a ? b | c", "a ? (b | c)")
        self.assertSame("a ? b ** c", "a ? (b ** c)")

    def test_precedence_ifexp(self):
        self.assertSame("a ? b if c else d", "(a ? b) if c else d")
        self.assertSame("a if b ? c else d", "a if (b ? c) else d")
        self.assertSame("a if c else d ? e", "a if c else (d ? e)")

    def test_precedence_lambda(self):
        node = ast.parse("lambda: a ? b", mode="eval").body
        self.assertIsInstance(node, ast.Lambda)
        self.assertIsInstance(node.body, ast.BinOp)
        self.assertIsInstance(node.body.op, ast.Choice)
        node = ast.parse("a ? (lambda: b)", mode="eval").body
        self.assertIsInstance(node, ast.BinOp)
        self.assertIsInstance(node.right, ast.Lambda)
        node = ast.parse("(lambda: a) ? b", mode="eval").body
        self.assertIsInstance(node, ast.BinOp)
        self.assertIsInstance(node.left, ast.Lambda)

    def test_walrus_target(self):
        node = ast.parse("(x := a ? b)", mode="eval").body
        self.assertIsInstance(node, ast.NamedExpr)
        self.assertIsInstance(node.value, ast.BinOp)
        self.assertIsInstance(node.value.op, ast.Choice)
        node = ast.parse("(x := a) ? b", mode="eval").body
        self.assertIsInstance(node, ast.BinOp)
        self.assertIsInstance(node.left, ast.NamedExpr)

    def test_in_call_and_keyword_argument(self):
        node = ast.parse("f(a ? b, k=a ? b)", mode="eval").body
        self.assertIsInstance(node.args[0], ast.BinOp)
        self.assertIsInstance(node.args[0].op, ast.Choice)
        self.assertEqual(node.keywords[0].arg, "k")
        self.assertIsInstance(node.keywords[0].value, ast.BinOp)
        self.assertIsInstance(node.keywords[0].value.op, ast.Choice)
        node = ast.parse("f(*(a ? b), **(c ? d))", mode="eval").body
        self.assertIsInstance(node.args[0].value, ast.BinOp)
        self.assertIsInstance(node.keywords[0].value, ast.BinOp)

    def test_in_decorator(self):
        tree = ast.parse("@a ? b\ndef f(): pass")
        deco = tree.body[0].decorator_list[0]
        self.assertIsInstance(deco, ast.BinOp)
        self.assertIsInstance(deco.op, ast.Choice)

    def test_in_annotations(self):
        tree = ast.parse("x: a ? b = c")
        self.assertIsInstance(tree.body[0].annotation, ast.BinOp)
        self.assertIsInstance(tree.body[0].annotation.op, ast.Choice)
        tree = ast.parse("def f(x: a ? b) -> a ? b: pass")
        func = tree.body[0]
        self.assertIsInstance(func.args.args[0].annotation.op, ast.Choice)
        self.assertIsInstance(func.returns.op, ast.Choice)

    def test_in_fstring_and_tstring(self):
        node = ast.parse("f'{a ? b}'", mode="eval").body
        self.assertIsInstance(node, ast.JoinedStr)
        self.assertIsInstance(node.values[0].value, ast.BinOp)
        self.assertIsInstance(node.values[0].value.op, ast.Choice)
        node = ast.parse("t'{a ? b}'", mode="eval").body
        self.assertIsInstance(node, ast.TemplateStr)
        self.assertIsInstance(node.values[0].value, ast.BinOp)
        self.assertIsInstance(node.values[0].value.op, ast.Choice)
        ns = {"a": 1, "b": 2}
        self.assertEqual(eval("f'{a ? b!r}'", ns), "ChoiceType(1, 2)")
        self.assertEqual(eval("f'{a ? b=}'", ns), "a ? b=ChoiceType(1, 2)")
        template = eval("t'{a ? b}'", ns)
        self.assertEqual(template.interpolations[0].expression, "a ? b")
        self.assertIs(type(template.interpolations[0].value), types.ChoiceType)

    def test_in_subscript(self):
        node = ast.parse("a[b ? c]", mode="eval").body
        self.assertIsInstance(node.slice, ast.BinOp)
        self.assertIsInstance(node.slice.op, ast.Choice)
        node = ast.parse("a[b ? c:d]", mode="eval").body
        self.assertIsInstance(node.slice, ast.Slice)
        self.assertIsInstance(node.slice.lower, ast.BinOp)
        node = ast.parse("a[b ? c, d]", mode="eval").body
        self.assertIsInstance(node.slice, ast.Tuple)
        self.assertIsInstance(node.slice.elts[0], ast.BinOp)

    def test_in_type_alias(self):
        tree = ast.parse("type T = a ? b")
        self.assertIsInstance(tree.body[0], ast.TypeAlias)
        self.assertIsInstance(tree.body[0].value, ast.BinOp)
        self.assertIsInstance(tree.body[0].value.op, ast.Choice)

    def test_in_match_subject(self):
        tree = ast.parse("match a ? b:\n    case _:\n        pass")
        self.assertIsInstance(tree.body[0].subject, ast.BinOp)
        self.assertIsInstance(tree.body[0].subject.op, ast.Choice)

    def test_in_comprehension_element(self):
        for src, kind in [("[a ? b for x in y]", ast.ListComp),
                          ("{a ? b for x in y}", ast.SetComp),
                          ("(a ? b for x in y)", ast.GeneratorExp)]:
            with self.subTest(src=src):
                node = ast.parse(src, mode="eval").body
                self.assertIsInstance(node, kind)
                self.assertIsInstance(node.elt, ast.BinOp)
                self.assertIsInstance(node.elt.op, ast.Choice)
        node = ast.parse("{a ? b: c for x in y}", mode="eval").body
        self.assertIsInstance(node, ast.DictComp)
        self.assertIsInstance(node.key, ast.BinOp)
        node = ast.parse("{c: a ? b for x in y}", mode="eval").body
        self.assertIsInstance(node.value, ast.BinOp)

    def test_comprehension_clauses_need_parens(self):
        # for_if_clause takes a disjunction: '?' in the iterable or in a
        # condition of a comprehension must be parenthesized.
        for src in ["[x for x in (a ? b)]", "[x for x in y if (a ? b)]"]:
            with self.subTest(src=src):
                node = ast.parse(src, mode="eval").body
                self.assertIsInstance(node, ast.ListComp)
        for src in ["[x for x in a ? b]", "[x for x in y if a ? b]"]:
            with self.subTest(src=src):
                with self.assertRaises(SyntaxError):
                    ast.parse(src, mode="eval")

    def test_await_yield_return(self):
        tree = ast.parse("async def f():\n    return await a ? b")
        value = tree.body[0].body[0].value
        self.assertIsInstance(value, ast.BinOp)
        self.assertIsInstance(value.op, ast.Choice)
        self.assertIsInstance(value.left, ast.Await)
        tree = ast.parse("def f():\n    yield a ? b")
        value = tree.body[0].body[0].value
        self.assertIsInstance(value, ast.Yield)
        self.assertIsInstance(value.value, ast.BinOp)
        tree = ast.parse("def f():\n    return a ? b")
        self.assertIsInstance(tree.body[0].body[0].value, ast.BinOp)

    def test_syntax_errors(self):
        for src in ["a ?", "? a", "a ?? b", "a ?= b", "?", "a ? ? b",
                    "a ? lambda: b"]:
            with self.subTest(src=src):
                check_syntax_error(self, src, "invalid syntax")
        # '?' in the left operand of an 'if' and in the first of two
        # juxtaposed items degrades the message to plain "invalid syntax";
        # only the exception type is pinned here.
        for src in ["x = a ? b if c", "[a ? b c]"]:
            with self.subTest(src=src):
                check_syntax_error(self, src)
        check_syntax_error(self, "a ?", "invalid syntax", offset=4)
        check_syntax_error(self, "? a", "invalid syntax", offset=1)
        # A lambda on the right of '?' needs parentheses: lambdef is
        # reachable only from 'expression', and the right operand of '?'
        # is 'choice'. The error points at the lambda keyword.
        check_syntax_error(self, "a ? lambda: b", "invalid syntax", offset=5)

    def test_not_in_patterns_or_targets(self):
        with self.assertRaises(SyntaxError):
            compile("match x:\n    case a ? b:\n        pass", "<t>", "exec")
        cannot_assign = "cannot assign|invalid syntax"
        for src in ["a ? b = 1", "for a ? b in c: pass",
                    "with x as a ? b: pass"]:
            with self.subTest(src=src):
                with self.assertRaisesRegex(SyntaxError, cannot_assign):
                    compile(src, "<test>", "exec")
        cannot_delete = "cannot delete|invalid syntax"
        with self.assertRaisesRegex(SyntaxError, cannot_delete):
            compile("del a ? b", "<test>", "exec")
        with self.assertRaisesRegex(SyntaxError,
                                    "assignment expressions|invalid syntax"):
            compile("(a ? b := c)", "<test>", "exec")
        with self.assertRaisesRegex(SyntaxError,
                                    "augmented assignment|invalid syntax"):
            compile("a ? b += 1", "<test>", "exec")
        for src in ["def f(a ? b): pass", "lambda a ? b: 1", "import a ? b",
                    "global a ? b"]:
            with self.subTest(src=src):
                check_syntax_error(self, src, "invalid syntax")

    @support.skip_wasi_stack_overflow()
    def test_nested_depth(self):
        # A '?' chain is right-recursive and costs one parser frame per
        # operator, like '**'.
        compile("1 ? " * 200 + "1", "", "eval")
        # Every bracket kind at the tokenizer cap (MAXLEVEL 200) still
        # compiles, as on stock CPython: the choice rule costs one extra
        # parser frame per level and MAXSTACK absorbs it. compile(), not
        # eval(): a set of sets is unhashable at run time.
        compile("(" * 200 + ")" * 200, "", "eval")
        compile("[" * 200 + "]" * 200, "", "eval")
        compile("{" * 200 + "}" * 200, "", "eval")
        compile("def f():\n    return " + "[" * 199 + "]" * 199, "", "exec")
        with self.assertRaisesRegex(MemoryError, "too complex"):
            compile("1 ? " * 6300 + "1", "", "eval")

    @unittest.skipUnless(sys.platform == "wasi", "WASI parser stack limits")
    def test_nested_depth_wasi(self):
        # WASI caps MAXSTACK at 4000 (1000 with Py_DEBUG) instead of 6000.
        # The deepest list nesting stock CPython accepts there is
        # floor((MAXSTACK - 26) / 29) levels; the choice frame per level
        # must not push it over the fork's scaled limit.
        depth = 33 if support.Py_DEBUG else 137
        compile("[" * depth + "]" * depth, "", "eval")

    def test_ast_operator_class(self):
        self.assertIsSubclass(ast.Choice, ast.operator)
        self.assertEndsWith(ast.operator.__doc__, "| Choice")
        self.assertEqual(ast.Choice._fields, ())
        self.assertIsInstance(ast.parse("a ? b", mode="eval").body.op,
                              ast.Choice)


# Sources that ast.unparse(ast.parse(src)) must reproduce byte for byte.
ROUNDTRIP_SOURCES = [
    "a ? b",
    "a ? b ? c",
    "(a ? b) ? c",
    "a or b ? c",
    "a ? b or c",
    "(a ? b) or c",
    "a ? b if c else d",
    "a if c ? d else e",
    "a if c else d ? e",
    "(a if c else d) ? e",
    "a ? (b if c else d)",
    "lambda: a ? b",
    "(lambda: a) ? b",
    "not a ? b",
    "not (a ? b)",
    "a ? b == c",
    "(a ? b) == c",
    "a ? b + c",
    "(a ? b) + c",
    "[x for x in (a ? b) if (c ? d)]",
    "[a ? b for x in y]",
    "f(a ? b, k=a ? b)",
    "a[b ? c]",
    "a[b ? c:d]",
    "(a ? b, c)",
    "f(*(a ? b))",
    "{**(a ? b)}",
    "f'{a ? b}'",
    "(x := (a ? b))",
    "x: a ? b = c",
    "@a ? b\ndef f():\n    pass",
    "async def f():\n    await a ? b",
    "async def f():\n    await (a ? b)",
]


class UnparseTests(unittest.TestCase):

    def check_src_roundtrip(self, src):
        self.assertEqual(ast.unparse(ast.parse(src)), src)

    def check_ast_roundtrip(self, src):
        tree = ast.parse(src)
        again = ast.parse(ast.unparse(tree))
        self.assertEqual(ast.dump(again), ast.dump(tree))

    def test_roundtrip_sources(self):
        for src in ROUNDTRIP_SOURCES:
            with self.subTest(src=src):
                self.check_src_roundtrip(src)

    def test_ast_roundtrip_sources(self):
        for src in ROUNDTRIP_SOURCES:
            with self.subTest(src=src):
                self.check_ast_roundtrip(src)

    def test_future_annotations(self):
        # Stringification under `from __future__ import annotations` goes
        # through the C unparser (Python/ast_unparse.c).
        src = textwrap.dedent("""\
            from __future__ import annotations
            x: a ? b
            y: (a ? b) ? c
            z: a ? b if c else d
            w: [i for i in (a ? b)]
            v: a ? (b if c else d)
            def f(p: a ? b ? c) -> not a ? b: pass
            """)
        ns = {}
        exec(src, ns)
        self.assertEqual(ns["__annotations__"], {
            "x": "a ? b",
            "y": "(a ? b) ? c",
            "z": "a ? b if c else d",
            "w": "[i for i in (a ? b)]",
            "v": "a ? (b if c else d)",
        })
        self.assertEqual(ns["f"].__annotations__,
                         {"p": "a ? b ? c", "return": "not a ? b"})

    def test_unparse_hand_built(self):
        node = ast.BinOp(ast.Name("a", ast.Load()), ast.Choice(),
                         ast.Name("b", ast.Load()))
        self.assertEqual(ast.unparse(ast.Expression(node)), "a ? b")
        nested = ast.BinOp(node, ast.Choice(), ast.Name("c", ast.Load()))
        self.assertEqual(ast.unparse(nested), "(a ? b) ? c")
        nested = ast.BinOp(ast.Name("c", ast.Load()), ast.Choice(), node)
        self.assertEqual(ast.unparse(nested), "c ? a ? b")


class AnnotationlibTests(unittest.TestCase):

    def test_string_format(self):
        def f(x: a ? b, y: (a ? b) ? c, z: a ? b ? c, w: 1 ? a, v: a ? 1):
            pass
        fmt = annotationlib.Format.STRING
        self.assertEqual(
            annotationlib.get_annotations(f, format=fmt),
            {"x": "a ? b", "y": "(a ? b) ? c", "z": "a ? b ? c",
             "w": "1 ? a", "v": "a ? 1"})

    def test_string_format_conditional_collapses(self):
        # Format.STRING executes 'if'/'else' at run time and _Stringifier
        # has no __bool__, so the conditional collapses to its first branch,
        # exactly like 'y: 1 if c else 0' stringifies to '1'.
        def f(x: a ? b if c else d, y: 1 if c else 0):
            pass
        fmt = annotationlib.Format.STRING
        self.assertEqual(
            annotationlib.get_annotations(f, format=fmt),
            {"x": "a ? b", "y": "1"})

    def test_forwardref_format(self):
        def f(x: a ? b):
            pass
        anno = annotationlib.get_annotations(
            f, format=annotationlib.Format.FORWARDREF)
        fwdref = anno["x"]
        self.assertIsInstance(fwdref, annotationlib.ForwardRef)
        self.assertEqual(fwdref.__forward_arg__, "a ? b")
        node = fwdref.evaluate(globals={"a": 1, "b": 2})
        self.assertIs(type(node), types.ChoiceType)
        self.assertEqual((node.lhs, node.rhs), (1, 2))


class DispatchTests(unittest.TestCase):

    def test_choice_called_with_operands(self):
        calls = []
        class A:
            def __choice__(self, other):
                calls.append((self, other))
                return "result"
        a = A()
        self.assertEqual(a ? 1, "result")
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][0], a)
        self.assertEqual(calls[0][1], 1)

    def test_result_returned_unchanged(self):
        # Only NotImplemented means "defer"; None and falsy values are results.
        for value in (42, None, [1, 2], "s", 0, False, ()):
            class A:
                def __choice__(self, other):
                    return value
            with self.subTest(value=value):
                self.assertIs(A() ? 1, value)

    def test_rchoice_when_left_type_lacks_choice(self):
        class R:
            def __rchoice__(self, other):
                return ("rchoice", other)
        r = R()
        self.assertEqual(1 ? r, ("rchoice", 1))
        self.assertEqual("x" ? r, ("rchoice", "x"))
        obj = object()
        self.assertEqual(obj ? r, ("rchoice", obj))
        class L:
            pass
        left = L()
        self.assertEqual(left ? r, ("rchoice", left))

    def test_left_choice_preferred_over_right_rchoice(self):
        log = []
        class L:
            def __choice__(self, other):
                log.append("L.__choice__")
                return "left"
        class R:
            def __rchoice__(self, other):
                log.append("R.__rchoice__")
                return "right"
        self.assertEqual(L() ? R(), "left")
        self.assertEqual(log, ["L.__choice__"])

    def test_notimplemented_falls_through_each_step(self):
        log = []
        class L:
            def __choice__(self, other):
                log.append("L.__choice__")
                return NotImplemented
        class R:
            def __rchoice__(self, other):
                log.append("R.__rchoice__")
                return "right"
        self.assertEqual(L() ? R(), "right")
        self.assertEqual(log, ["L.__choice__", "R.__rchoice__"])
        log.clear()
        class R2:
            def __rchoice__(self, other):
                log.append("R2.__rchoice__")
                return NotImplemented
        left, right = L(), R2()
        node = left ? right
        self.assertIs(type(node), types.ChoiceType)
        self.assertIs(node.lhs, left)
        self.assertIs(node.rhs, right)
        self.assertEqual(log, ["L.__choice__", "R2.__rchoice__"])

    def test_subclass_right_overriding_rchoice_goes_first(self):
        log = []
        class B:
            def __choice__(self, other):
                log.append("B.__choice__")
                return "B.choice"
            def __rchoice__(self, other):
                log.append("B.__rchoice__")
                return "B.rchoice"
        class C(B):
            def __rchoice__(self, other):
                log.append("C.__rchoice__")
                return "C.rchoice"
        self.assertEqual(B() ? C(), "C.rchoice")
        self.assertEqual(log, ["C.__rchoice__"])
        log.clear()
        # The other way round the left operand's __choice__ wins.
        self.assertEqual(C() ? B(), "B.choice")
        self.assertEqual(log, ["B.__choice__"])

    def test_subclass_right_inheriting_rchoice_does_not_go_first(self):
        log = []
        class B:
            def __choice__(self, other):
                log.append("B.__choice__")
                return "B.choice"
            def __rchoice__(self, other):
                log.append("B.__rchoice__")
                return "B.rchoice"
        class C(B):
            pass
        self.assertEqual(B() ? C(), "B.choice")
        self.assertEqual(log, ["B.__choice__"])

    def test_inherited_classmethod_rchoice_counts_as_overloaded(self):
        # The overload test compares the attributes looked up on the two
        # types with !=. An inherited classmethod binds to the subclass and
        # so counts as overloaded, exactly as for the stock operators.
        log = []
        class B:
            def __choice__(self, other):
                log.append("B.__choice__")
                return "B.choice"
            @classmethod
            def __rchoice__(cls, other):
                log.append(f"{cls.__name__}.__rchoice__")
                return f"{cls.__name__}.rchoice"
            def __add__(self, other):
                log.append("B.__add__")
                return "B.add"
            @classmethod
            def __radd__(cls, other):
                log.append(f"{cls.__name__}.__radd__")
                return f"{cls.__name__}.radd"
        class C(B):
            pass
        self.assertEqual(B() ? C(), "C.rchoice")
        self.assertEqual(log, ["C.__rchoice__"])
        log.clear()
        self.assertEqual(B() + C(), "C.radd")
        self.assertEqual(log, ["C.__radd__"])

    def test_reflected_not_retried_after_notimplemented(self):
        log = []
        class B:
            def __choice__(self, other):
                log.append("B.__choice__")
                return NotImplemented
            def __rchoice__(self, other):
                log.append("B.__rchoice__")
                return NotImplemented
        class C(B):
            def __rchoice__(self, other):
                log.append("C.__rchoice__")
                return NotImplemented
        b, c = B(), C()
        node = b ? c
        self.assertIs(type(node), types.ChoiceType)
        self.assertIs(node.lhs, b)
        self.assertIs(node.rhs, c)
        self.assertEqual(log, ["C.__rchoice__", "B.__choice__"])

    def test_same_type_never_calls_rchoice(self):
        log = []
        class A:
            def __choice__(self, other):
                log.append("__choice__")
                return NotImplemented
            def __rchoice__(self, other):
                log.append("__rchoice__")
                return "reflected"
        node = A() ? A()
        self.assertIs(type(node), types.ChoiceType)
        self.assertEqual(log, ["__choice__"])

    def test_exception_propagates(self):
        class L:
            def __choice__(self, other):
                raise ValueError("left")
        with self.assertRaisesRegex(ValueError, "left"):
            L() ? 1
        class R:
            def __rchoice__(self, other):
                raise ValueError("right")
        with self.assertRaisesRegex(ValueError, "right"):
            1 ? R()
        class Desc:
            def __init__(self, exc):
                self.exc = exc
            def __get__(self, obj, objtype=None):
                raise self.exc
        class BadGet:
            __choice__ = Desc(RuntimeError("descriptor"))
        with self.assertRaisesRegex(RuntimeError, "descriptor"):
            BadGet() ? 1
        class BadRGet:
            __rchoice__ = Desc(RuntimeError("rdescriptor"))
        with self.assertRaisesRegex(RuntimeError, "rdescriptor"):
            1 ? BadRGet()
        # An AttributeError from __get__ means "no such method".
        class MissingGet:
            __choice__ = Desc(AttributeError("missing"))
        left = MissingGet()
        node = left ? 1
        self.assertIs(type(node), types.ChoiceType)
        self.assertIs(node.lhs, left)
        self.assertEqual(node.rhs, 1)

    def test_fallback_node_identity(self):
        a, b = object(), object()
        node = a ? b
        self.assertIs(type(node), types.ChoiceType)
        self.assertIs(node.lhs, a)
        self.assertIs(node.rhs, b)

    def test_instance_attribute_ignored(self):
        # Special-method lookup uses the type, not the instance.
        class A:
            pass
        a = A()
        a.__choice__ = lambda other: "instance"
        a.__rchoice__ = lambda other: "instance"
        node = a ? 1
        self.assertIs(type(node), types.ChoiceType)
        node = 1 ? a
        self.assertIs(type(node), types.ChoiceType)

    def test_staticmethod_and_classmethod(self):
        class S:
            @staticmethod
            def __choice__(other):
                return ("static", other)
        self.assertEqual(S() ? 1, ("static", 1))
        class K:
            @classmethod
            def __choice__(cls, other):
                return ("class", cls, other)
        self.assertEqual(K() ? 1, ("class", K, 1))
        class RS:
            @staticmethod
            def __rchoice__(other):
                return ("rstatic", other)
        self.assertEqual(1 ? RS(), ("rstatic", 1))
        class RK:
            @classmethod
            def __rchoice__(cls, other):
                return ("rclass", cls, other)
        self.assertEqual(1 ? RK(), ("rclass", RK, 1))

    def test_choice_none_raises_typeerror(self):
        # Like __add__ = None: the value is called and fails.
        class A:
            __choice__ = None
        with self.assertRaisesRegex(TypeError,
                                    "'NoneType' object is not callable"):
            A() ? 1
        class R:
            __rchoice__ = None
        with self.assertRaisesRegex(TypeError,
                                    "'NoneType' object is not callable"):
            1 ? R()

    def test_builtin_operands_fall_back(self):
        for a, b in [(1, 2), ("a", None), ([], {}), (1.5, 2), (True, b"x"),
                     ((), set()), (int, str), (print, len)]:
            with self.subTest(a=a, b=b):
                node = a ? b
                self.assertIs(type(node), types.ChoiceType)
                self.assertIs(node.lhs, a)
                self.assertIs(node.rhs, b)

    def test_nested_chain(self):
        node = 1 ? 2 ? 3
        self.assertEqual(repr(node), "ChoiceType(1, ChoiceType(2, 3))")
        self.assertEqual(node.lhs, 1)
        self.assertIs(type(node.rhs), types.ChoiceType)
        self.assertEqual((node.rhs.lhs, node.rhs.rhs), (2, 3))
        node = (1 ? 2) ? 3
        self.assertEqual(repr(node), "ChoiceType(ChoiceType(1, 2), 3)")
        self.assertIs(type(node.lhs), types.ChoiceType)
        self.assertEqual(node.rhs, 3)

    def test_evaluation_order(self):
        log = []
        def side(name):
            log.append(name)
            return name
        node = side("left") ? side("right")
        self.assertEqual(log, ["left", "right"])
        self.assertEqual((node.lhs, node.rhs), ("left", "right"))
        # Both operands are evaluated even when __choice__ ignores one.
        class Ignore:
            def __choice__(self, other):
                return "ignored"
        log.clear()
        self.assertEqual(Ignore() ? side("right"), "ignored")
        self.assertEqual(log, ["right"])

    def test_specialization_stability(self):
        # BINARY_OP with oparg NB_CHOICE never specializes; results must
        # stay correct after the adaptive counter gives up.
        a, b = 1, 2
        for _ in range(3000):
            node = a ? b
            self.assertIs(type(node), types.ChoiceType)
        self.assertEqual((node.lhs, node.rhs), (1, 2))
        class A:
            def __choice__(self, other):
                return other + 1
        x = A()
        for i in range(3000):
            self.assertEqual(x ? i, i + 1)
        self.assertIs(type(a ? b), types.ChoiceType)


class ChoiceTypeTests(unittest.TestCase):

    def test_type_identity(self):
        self.assertIs(type(0 ? 1), types.ChoiceType)
        self.assertEqual(types.ChoiceType.__module__, "types")
        self.assertEqual(types.ChoiceType.__name__, "ChoiceType")
        self.assertEqual(types.ChoiceType.__qualname__, "ChoiceType")
        self.assertIn("ChoiceType", types.__all__)

    def test_constructor(self):
        node = types.ChoiceType(1, 2)
        self.assertIs(type(node), types.ChoiceType)
        self.assertEqual((node.lhs, node.rhs), (1, 2))
        with self.assertRaisesRegex(TypeError, "expected 2 arguments, got 1"):
            types.ChoiceType(1)
        with self.assertRaisesRegex(TypeError, "expected 2 arguments, got 3"):
            types.ChoiceType(1, 2, 3)
        with self.assertRaisesRegex(TypeError, "expected 2 arguments, got 0"):
            types.ChoiceType()
        with self.assertRaisesRegex(TypeError, "takes no keyword arguments"):
            types.ChoiceType(lhs=1, rhs=2)
        with self.assertRaisesRegex(TypeError, "takes no keyword arguments"):
            types.ChoiceType(1, rhs=2)

    def test_members_readonly(self):
        node = types.ChoiceType(1, 2)
        for name in ("lhs", "rhs"):
            with self.subTest(name=name):
                with self.assertRaises(AttributeError):
                    setattr(node, name, 3)
                with self.assertRaises(AttributeError):
                    delattr(node, name)
        with self.assertRaises(AttributeError):
            node.foo = 1
        self.assertNotHasAttr(node, "__dict__")
        self.assertEqual((node.lhs, node.rhs), (1, 2))

    def test_repr_nested(self):
        self.assertEqual(repr(types.ChoiceType(1, types.ChoiceType(2, 3))),
                         "ChoiceType(1, ChoiceType(2, 3))")
        self.assertEqual(repr(1 ? 2 ? 3), "ChoiceType(1, ChoiceType(2, 3))")
        self.assertEqual(repr(types.ChoiceType("a", None)),
                         "ChoiceType('a', None)")
        self.assertEqual(str(types.ChoiceType(1, 2)), "ChoiceType(1, 2)")

    def test_repr_recursive(self):
        operands = []
        node = types.ChoiceType(operands, 1)
        operands.append(node)
        self.assertEqual(repr(node), "ChoiceType([ChoiceType(...)], 1)")

    def test_bool_raises(self):
        node = types.ChoiceType(1, 2)
        msg = "truth value of a ChoiceType is ambiguous"
        with self.assertRaisesRegex(TypeError, msg):
            bool(node)
        with self.assertRaisesRegex(TypeError, msg):
            not node
        with self.assertRaisesRegex(TypeError, msg):
            if node:
                pass
        with self.assertRaisesRegex(TypeError, msg):
            node and 1
        with self.assertRaisesRegex(TypeError, msg):
            node or 1
        with self.assertRaisesRegex(TypeError, msg):
            1 if node else 2

    def test_identity_eq_hash(self):
        node = types.ChoiceType(1, 2)
        other = types.ChoiceType(1, 2)
        self.assertTrue(node == node)
        self.assertFalse(node != node)
        self.assertFalse(node == other)
        self.assertTrue(node != other)
        self.assertFalse(node == 1)
        self.assertTrue(node != 1)
        self.assertEqual(hash(node), object.__hash__(node))
        mapping = {node: "a", other: "b"}
        self.assertEqual(mapping[node], "a")
        self.assertEqual(len({node, other}), 2)
        self.assertIn(node, [other, node])

    def test_weakref(self):
        node = types.ChoiceType(1, 2)
        called = []
        ref = weakref.ref(node, called.append)
        self.assertIs(ref(), node)
        del node
        gc_collect()
        self.assertIsNone(ref())
        self.assertEqual(len(called), 1)

    def test_getnewargs(self):
        node = types.ChoiceType(1, 2)
        self.assertEqual(node.__getnewargs__(), (1, 2))

    def test_pickle_and_copy(self):
        node = types.ChoiceType(1, types.ChoiceType("a", None))
        # __reduce__ names the type and the operands, so every protocol
        # rebuilds a node, including the copyreg-based protocols 0 and 1.
        cls, args = node.__reduce__()
        self.assertIs(cls, types.ChoiceType)
        self.assertEqual(len(args), 2)
        self.assertEqual(args[0], 1)
        self.assertIs(args[1], node.rhs)
        for proto in range(pickle.HIGHEST_PROTOCOL + 1):
            with self.subTest(proto=proto):
                copied = pickle.loads(pickle.dumps(node, proto))
                self.assertIs(type(copied), types.ChoiceType)
                self.assertIsNot(copied, node)
                self.assertEqual(repr(copied), repr(node))
                self.assertEqual(copied.lhs, 1)
                self.assertIs(type(copied.rhs), types.ChoiceType)
        shallow = copy.copy(node)
        self.assertIs(type(shallow), types.ChoiceType)
        self.assertIsNot(shallow, node)
        self.assertIs(shallow.rhs, node.rhs)
        deep = copy.deepcopy(node)
        self.assertIs(type(deep), types.ChoiceType)
        self.assertIsNot(deep, node)
        self.assertIsNot(deep.rhs, node.rhs)
        self.assertEqual(repr(deep), repr(node))

    def test_match_positional_and_keyword(self):
        self.assertEqual(types.ChoiceType.__match_args__, ("lhs", "rhs"))
        node = 1 ? 2 ? 3
        match node:
            case types.ChoiceType(a, b):
                self.assertEqual(a, 1)
                self.assertIs(b, node.rhs)
            case _:
                self.fail("positional pattern did not match")
        match node:
            case types.ChoiceType(lhs=a, rhs=types.ChoiceType(lhs=b, rhs=c)):
                self.assertEqual((a, b, c), (1, 2, 3))
            case _:
                self.fail("keyword pattern did not match")
        match node:
            case types.ChoiceType(1, types.ChoiceType(2, 3)):
                pass
            case _:
                self.fail("nested literal pattern did not match")
        match node:
            case types.ChoiceType(2, _):
                self.fail("pattern must not match")
            case types.ChoiceType(_, _):
                pass
            case _:
                self.fail("wildcard pattern did not match")
        match 1:
            case types.ChoiceType(_, _):
                self.fail("an int is not a node")
            case _:
                pass

    def test_subclassing_raises(self):
        msg = "is not an acceptable base type"
        with self.assertRaisesRegex(TypeError, msg):
            class X(types.ChoiceType):
                pass

    def test_immutable_type(self):
        with self.assertRaisesRegex(TypeError, "immutable type"):
            types.ChoiceType.x = 1
        with self.assertRaisesRegex(TypeError, "immutable type"):
            del types.ChoiceType.lhs
        self.assertNotHasAttr(types.ChoiceType, "x")

    def test_gc_cycle_collected(self):
        operands = []
        node = types.ChoiceType(operands, operands)
        operands.append(node)
        ref = weakref.ref(node)
        del operands, node
        gc_collect()
        self.assertIsNone(ref())

    def test_no_dunder_choice(self):
        self.assertNotHasAttr(types.ChoiceType, "__choice__")
        self.assertNotHasAttr(types.ChoiceType, "__rchoice__")
        # The dispatch nests nodes without help from the type.
        node = types.ChoiceType(1, 2) ? 3
        self.assertIs(type(node), types.ChoiceType)
        self.assertIs(type(node.lhs), types.ChoiceType)
        self.assertEqual(node.rhs, 3)

    def test_no_arithmetic_iteration_or_len(self):
        node = types.ChoiceType(1, 2)
        with self.assertRaises(TypeError):
            node + 1
        with self.assertRaises(TypeError):
            iter(node)
        with self.assertRaises(TypeError):
            len(node)
        with self.assertRaises(TypeError):
            node[0]
        with self.assertRaises(TypeError):
            1 in node
        with self.assertRaises(TypeError):
            node()
        with self.assertRaises(TypeError):
            node < node

    def test_signature(self):
        self.assertEqual(str(inspect.signature(types.ChoiceType)),
                         "(lhs, rhs, /)")
        self.assertEqual(types.ChoiceType.__text_signature__, "(lhs, rhs, /)")

    def test_doc_first_line(self):
        self.assertStartsWith(types.ChoiceType.__doc__,
                              "The type of a choice node")
        self.assertEqual(types.ChoiceType.lhs.__doc__, "the left operand")
        self.assertEqual(types.ChoiceType.rhs.__doc__, "the right operand")


class CompilerTests(unittest.TestCase):

    @staticmethod
    def all_consts(code):
        for const in code.co_consts:
            yield const
            if isinstance(const, types.CodeType):
                yield from CompilerTests.all_consts(const)

    def assertNoNodeConst(self, code):
        for const in self.all_consts(code):
            self.assertIsNot(type(const), types.ChoiceType)

    def test_nb_ops_entry(self):
        self.assertEqual(opcode._nb_ops[27], ("NB_CHOICE", "?"))
        self.assertEqual(len(opcode._nb_ops), 28)
        self.assertEqual(opcode._nb_ops[26], ("NB_SUBSCR", "[]"))

    def test_dis_shows_question(self):
        code = compile("a ? b", "", "eval")
        binops = [ins for ins in dis.get_instructions(code)
                  if ins.opname == "BINARY_OP"]
        self.assertEqual(len(binops), 1)
        self.assertEqual(binops[0].arg, 27)
        self.assertEqual(binops[0].argrepr, "?")
        out = io.StringIO()
        dis.dis(code, file=out)
        self.assertRegex(out.getvalue(), r"BINARY_OP\s+27 \(\?\)")

    def test_no_constant_folding(self):
        for optimize in (0, 1, 2):
            with self.subTest(optimize=optimize):
                code = compile("1 ? 2", "", "eval", optimize=optimize)
                self.assertNoNodeConst(code)
                ops = [(ins.opname, ins.arg)
                       for ins in dis.get_instructions(code)]
                self.assertIn(("BINARY_OP", 27), ops)
                first, second = eval(code), eval(code)
                self.assertIs(type(first), types.ChoiceType)
                self.assertIs(type(second), types.ChoiceType)
                self.assertIsNot(first, second)
                data = marshal.dumps(code)
                self.assertEqual(marshal.loads(data).co_code, code.co_code)

    def test_no_folding_variants(self):
        for src in ["'a' ? 'b'", "(1 ? 2) ? 3", "1 ? 2 ? 3", "None ? ()",
                    "1 ? 2 + 3", "-1 ? 2", "(1, 2) ? 'x'",
                    "1 ? 2 if 1 else 3"]:
            with self.subTest(src=src):
                code = compile(src, "", "eval", optimize=2)
                self.assertNoNodeConst(code)
                binops = [ins.arg for ins in dis.get_instructions(code)
                          if ins.opname == "BINARY_OP"]
                self.assertIn(27, binops)
                self.assertIs(type(eval(code)), types.ChoiceType)
                marshal.dumps(code)

    def test_hand_built_binop_compiles(self):
        node = ast.Expression(ast.BinOp(ast.Constant(1), ast.Choice(),
                                        ast.Constant(2)))
        ast.fix_missing_locations(node)
        result = eval(compile(node, "<ast>", "eval"))
        self.assertIs(type(result), types.ChoiceType)
        self.assertEqual((result.lhs, result.rhs), (1, 2))

    def test_hand_built_augassign_raises(self):
        stmt = ast.AugAssign(ast.Name("x", ast.Store()), ast.Choice(),
                             ast.Constant(1))
        module = ast.Module([stmt], [])
        ast.fix_missing_locations(module)
        with self.assertRaisesRegex(
                ValueError,
                "choice operator cannot be used in augmented assignment"):
            compile(module, "<ast>", "exec")

    def test_pyc_roundtrip_of_choice_module(self):
        source = "def make(a, b):\n    return a ? b ? 1\n\nNODE = 2 ? 3\n"
        with import_helper.ready_to_import(source=source) as (name, path):
            cached = importlib.util.cache_from_source(path)
            self.assertEqual(py_compile.compile(path, doraise=True), cached)
            with open(cached, "rb") as f:
                data = f.read()
            self.assertEqual(data[:4], importlib.util.MAGIC_NUMBER)
            module = importlib.import_module(name)
            self.assertEqual(module.__cached__, cached)
            node = module.make(1, 2)
            self.assertEqual(repr(node), "ChoiceType(1, ChoiceType(2, 1))")
            self.assertEqual(repr(module.NODE), "ChoiceType(2, 3)")
            # The cached bytecode was loaded, not rewritten.
            with open(cached, "rb") as f:
                self.assertEqual(f.read(), data)


class PycMagicTests(unittest.TestCase):

    def test_fork_magic_is_stock_plus_2000(self):
        fork = importlib.util.MAGIC_NUMBER
        stock = _bootstrap_external._STOCK_MAGIC_NUMBER
        self.assertEqual(fork, _bootstrap_external.MAGIC_NUMBER)
        self.assertEqual(len(fork), 4)
        self.assertEqual(len(stock), 4)
        self.assertEqual(fork[2:], b"\r\n")
        self.assertEqual(stock[2:], b"\r\n")
        fork_value = int.from_bytes(fork[:2], "little")
        stock_value = int.from_bytes(stock[:2], "little")
        self.assertEqual(fork_value, stock_value + 2000)
        # Stock 3.n magic numbers start at 2900 + 50n.
        start = 2900 + sys.version_info.minor * 50
        self.assertIn(stock_value, range(start, start + 50))
        self.assertEqual(_imp.pyc_magic_number_token.to_bytes(4, "little"),
                         fork)
        self.assertEqual(
            _imp.pyc_magic_number_token_stock.to_bytes(4, "little"), stock)

    def test_classify_pyc_accepts_both_magics(self):
        code = compile("x = 1\n", "<test>", "exec")
        data = _bootstrap_external._code_to_timestamp_pyc(code, mtime=1,
                                                          source_size=6)
        self.assertEqual(data[:4], importlib.util.MAGIC_NUMBER)
        classify = _bootstrap_external._classify_pyc
        self.assertEqual(classify(data, "spam", {}), 0)
        stock_data = _bootstrap_external._STOCK_MAGIC_NUMBER + data[4:]
        self.assertEqual(classify(stock_data, "spam", {}), 0)
        # Both headers validate against the same source metadata.
        validate = _bootstrap_external._validate_timestamp_pyc
        for header in (data, stock_data):
            validate(header, 1, 6, "spam", {})

    def test_classify_pyc_rejects_unknown_magic(self):
        code = compile("x = 1\n", "<test>", "exec")
        data = _bootstrap_external._code_to_timestamp_pyc(code)
        stock_value = int.from_bytes(
            _bootstrap_external._STOCK_MAGIC_NUMBER[:2], "little")
        for value in (stock_value + 1000, stock_value - 1, 0):
            magic = value.to_bytes(2, "little") + b"\r\n"
            with self.subTest(magic=magic):
                with self.assertRaisesRegex(ImportError, "bad magic number"):
                    _bootstrap_external._classify_pyc(magic + data[4:],
                                                      "spam", {})
        with self.assertRaisesRegex(ImportError, "bad magic number"):
            _bootstrap_external._classify_pyc(b"0000" + data[4:], "spam", {})

    def test_import_stock_timestamp_pyc(self):
        source = "VALUE = 'from source'\n"
        with import_helper.ready_to_import(source=source) as (name, path):
            cached = importlib.util.cache_from_source(path)
            py_compile.compile(path, doraise=True)
            with open(cached, "rb") as f:
                data = f.read()
            # A pyc as a stock interpreter would have written it for this
            # source: stock magic, same mtime and size, different code.
            code = compile("VALUE = 'from pyc'\n", path, "exec")
            data = (_bootstrap_external._STOCK_MAGIC_NUMBER + data[4:16]
                    + marshal.dumps(code))
            with open(cached, "wb") as f:
                f.write(data)
            module = importlib.import_module(name)
            self.assertEqual(module.VALUE, "from pyc")
            self.assertEqual(module.__cached__, cached)
            with open(cached, "rb") as f:
                self.assertEqual(f.read(), data)

    def test_stock_checked_hash_pyc_accepted(self):
        source = "VALUE = 'from source'\n"
        with import_helper.ready_to_import(source=source) as (name, path):
            with open(path, "rb") as f:
                source_bytes = f.read()
            # A checked-hash pyc as a stock interpreter would write it: the
            # stock magic in the header and the hash keyed on the stock
            # token. The code differs from the source so that a recompile
            # would be visible.
            code = compile("VALUE = 'from pyc'\n", path, "exec")
            source_hash = _imp.source_hash(_imp.pyc_magic_number_token_stock,
                                           source_bytes)
            data = _bootstrap_external._code_to_hash_pyc(code, source_hash,
                                                         checked=True)
            data = _bootstrap_external._STOCK_MAGIC_NUMBER + data[4:]
            self.assertEqual(_bootstrap_external._classify_pyc(data, name, {}),
                             0b11)
            cached = importlib.util.cache_from_source(path)
            os.makedirs(os.path.dirname(cached), exist_ok=True)
            with open(cached, "wb") as f:
                f.write(data)
            loader = importlib.machinery.SourceFileLoader(name, path)
            loaded = loader.get_code(name)
            namespace = {}
            exec(loaded, namespace)
            self.assertEqual(namespace["VALUE"], "from pyc")
            # No recompile: the file still carries the stock header.
            with open(cached, "rb") as f:
                self.assertEqual(f.read(), data)

    def test_sourceless_import_with_stock_magic(self):
        source = "VALUE = 'from source'\n"
        with import_helper.ready_to_import(source=source) as (name, path):
            py_compile.compile(path, doraise=True)
            legacy = import_helper.make_legacy_pyc(path)
            os.unlink(path)
            with open(legacy, "rb") as f:
                data = f.read()
            with open(legacy, "wb") as f:
                f.write(_bootstrap_external._STOCK_MAGIC_NUMBER + data[4:])
            importlib.invalidate_caches()
            module = importlib.import_module(name)
            self.assertEqual(module.VALUE, "from source")
            self.assertEqual(module.__file__, legacy)

    def test_pkgutil_read_code_accepts_stock_magic(self):
        code = compile("x = 1\n", "<test>", "exec")
        data = _bootstrap_external._code_to_timestamp_pyc(code)
        loaded = pkgutil.read_code(io.BytesIO(data))
        self.assertIsInstance(loaded, types.CodeType)
        stock_data = _bootstrap_external._STOCK_MAGIC_NUMBER + data[4:]
        loaded = pkgutil.read_code(io.BytesIO(stock_data))
        self.assertIsInstance(loaded, types.CodeType)
        self.assertEqual(loaded.co_code, code.co_code)
        self.assertIsNone(pkgutil.read_code(io.BytesIO(b"0000" + data[4:])))

    def test_run_pyc_file_with_stock_magic(self):
        with os_helper.temp_dir() as tempdir:
            path = script_helper.make_script(
                tempdir, "script", "print('from pyc', 1 ? 2)\n")
            pyc = py_compile.compile(path, cfile=os.path.join(tempdir,
                                                              "script.pyc"),
                                     doraise=True)
            with open(pyc, "rb") as f:
                data = f.read()
            self.assertEqual(data[:4], importlib.util.MAGIC_NUMBER)
            with open(pyc, "wb") as f:
                f.write(_bootstrap_external._STOCK_MAGIC_NUMBER + data[4:])
            rc, out, err = script_helper.assert_python_ok(pyc)
            self.assertEqual(out.strip(), b"from pyc ChoiceType(1, 2)")
            # Without the .pyc suffix the interpreter sniffs the magic
            # number (maybe_pyc_file in Python/pythonrun.c).
            nosuffix = os.path.join(tempdir, "script_bin")
            os.rename(pyc, nosuffix)
            rc, out, err = script_helper.assert_python_ok(nosuffix)
            self.assertEqual(out.strip(), b"from pyc ChoiceType(1, 2)")

    def test_compileall_keeps_stock_pyc(self):
        import compileall
        with import_helper.ready_to_import(source="x = 1\n") as (name, path):
            timestamp = py_compile.PycInvalidationMode.TIMESTAMP
            cached = py_compile.compile(path, doraise=True,
                                        invalidation_mode=timestamp)
            with open(cached, "rb") as f:
                data = f.read()
            stock = _bootstrap_external._STOCK_MAGIC_NUMBER + data[4:]
            with open(cached, "wb") as f:
                f.write(stock)
            # Without force, a pyc written by stock CPython is up to date:
            # compileall must neither rewrite it nor fail on it.
            self.assertTrue(compileall.compile_file(path, quiet=2))
            with open(cached, "rb") as f:
                self.assertEqual(f.read(), stock)
            # An unknown magic number is still stale and gets recompiled.
            with open(cached, "wb") as f:
                f.write(b"0000" + data[4:])
            self.assertTrue(compileall.compile_file(path, quiet=2))
            with open(cached, "rb") as f:
                self.assertEqual(f.read()[:4], importlib.util.MAGIC_NUMBER)

    def test_pydoc_importfile_stock_magic(self):
        import pydoc
        name = "test_choice_pydoc_stock"
        self.addCleanup(import_helper.unload, name)
        with os_helper.temp_dir() as tempdir:
            source = os.path.join(tempdir, name + ".py")
            with open(source, "w") as f:
                f.write("VALUE = 1\n")
            pyc = py_compile.compile(source,
                                     cfile=os.path.join(tempdir, name + ".pyc"),
                                     doraise=True)
            with open(pyc, "rb") as f:
                data = f.read()
            self.assertEqual(pydoc.importfile(pyc).VALUE, 1)
            import_helper.unload(name)
            # A pyc written by stock CPython is bytecode too, not source.
            with open(pyc, "wb") as f:
                f.write(_bootstrap_external._STOCK_MAGIC_NUMBER + data[4:])
            self.assertEqual(pydoc.importfile(pyc).VALUE, 1)


if __name__ == "__main__":
    unittest.main()
