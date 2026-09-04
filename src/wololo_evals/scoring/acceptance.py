"""``code:`` acceptance assertions: deterministic expressions over a run's
recorded outputs (open, Apache-2.0).

A ``code:`` assertion (README §11.3) is the mechanical counterpart to a
``trace:`` assertion: instead of checking the trajectory, it checks the *result*
the run left behind, and it does so without calling a model. The assertion body
is a single Python expression evaluated against a namespace the grader builds
from the run:

============  ==============================================================
``output``    the run's ``output.json`` (attribute *and* item access), i.e.
              whatever the harness recorded about what the run produced
``result``    the ``RunResult`` fields (``status``, ``cost_usd``, ...)
``trace``     the normalised ``TraceEvent`` sequence
``case``      the ``CaseSpec`` fields (``case_id``, ``metadata``, ...)
============  ==============================================================

**The expression is not trusted code.** Assertion text is authored in a
benchmark's ``cases.toml`` and the *values* it reads come from an agent run, so
the evaluator is an allow-list, not a sandbox-by-convention: the expression is
parsed to an AST, every node is checked against :data:`_ALLOWED_NODES`, and
evaluation runs with ``__builtins__`` replaced by :data:`SAFE_BUILTINS`.
Anything outside that surface is a :class:`CodeAssertionError`, never a silent
pass.

What that does and does not buy, stated precisely, because an overclaim here is
worse than no claim:

*Prevented.* Importing, calling arbitrary builtins, defining a lambda, binding
state with ``:=``, and every statement form. Naming a private or dunder
attribute *syntactically* (``x.__class__``, ``x._data``), which closes the
usual walk from any object to ``__globals__`` and back to the interpreter.
Unbounded exponentiation and f-strings (see :data:`_REJECTED_NODES`). Reaching
a name the namespace does not define, since the namespace *is* the global
scope.

*Not prevented.* Attribute traversal that happens inside a runtime string
rather than in the syntax tree. ``"{0.__class__}".format(x)`` is a plain
:class:`str` method call on a literal with no ``__class__`` node in the AST for
the walk to see, so the formatter reaches the attribute where the allow-list
cannot. ``format`` and ``format_map`` are therefore rejected by name (a real
assertion has no use for either), but that is a named block on the two methods
that are known to do it, not a general guarantee about every string method in
every future Python. The evaluator's job is to stop an authored expression
doing damage or hanging, and it does that; it is not a security boundary
against a hostile casebook author, who could equally well edit the benchmark.

Imports: ``core.interfaces`` + stdlib only.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, Mapping

from wololo_evals.core.interfaces import ScoredSignal

__all__ = [
    "CodeAssertionError",
    "CodeCheckResult",
    "SAFE_BUILTINS",
    "TASK_OUTCOME_SLOT",
    "Namespace",
    "is_code_assertion",
    "parse_code_assertion",
    "evaluate_code_assertion",
]

#: §16.1 slot every ``code:`` assertion feeds.
TASK_OUTCOME_SLOT = "task_outcome"

_PREFIX = "code:"

#: The only builtins an assertion may call. Everything here is pure and cannot
#: reach the filesystem, the network, or the interpreter internals.
#: Methods rejected by name. ``str.format``/``format_map`` interpret their
#: format string at runtime, so ``"{0.__class__}".format(x)`` traverses an
#: attribute the AST walk never sees. No assertion needs either.
_REJECTED_ATTRS = frozenset({"format", "format_map"})

SAFE_BUILTINS: Mapping[str, Any] = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "float": float,
    "int": int,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "True": True,
    "False": False,
    "None": None,
}

#: AST nodes an assertion expression may contain. Deliberately excludes
#: ``Lambda``, ``Await``, ``NamedExpr`` (``:=`` would let an expression bind
#: state), f-strings (``JoinedStr`` invokes ``__format__``), ``Pow`` (see
#: :data:`_REJECTED_NODES`), and every statement node.
_ALLOWED_NODES: frozenset[type[ast.AST]] = frozenset({
    ast.Expression,
    ast.BoolOp, ast.And, ast.Or,
    ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
    ast.Compare,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.Is, ast.IsNot, ast.In, ast.NotIn,
    ast.IfExp,
    ast.Call, ast.keyword,
    ast.Attribute, ast.Subscript, ast.Slice,
    ast.Name, ast.Load,
    ast.Constant,
    ast.List, ast.Tuple, ast.Dict, ast.Set,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.comprehension,
    ast.Starred,
})


#: Constructs rejected with a reason of their own rather than the generic
#: "disallowed construct" message, because the reason is not obvious.
_REJECTED_NODES: Mapping[type[ast.AST], str] = {
    # `9**9**9` is three tokens that allocate for minutes. Every allow-listed
    # operator is bounded by the size of its operands; exponentiation is the
    # one that is not, and no assertion has ever needed it.
    ast.Pow: (
        "exponentiation is not allowed: `a ** b` is the one arithmetic "
        "operator whose cost is unbounded in the size of its operands, and a "
        "gate must not hang on an authored expression"
    ),
    ast.JoinedStr: (
        "f-strings are not allowed: formatting invokes __format__ on the "
        "value, which is a route to attributes the allow-list otherwise blocks"
    ),
}


class CodeAssertionError(ValueError):
    """A ``code:`` assertion is malformed, uses a disallowed construct, or
    raised while evaluating. Fail loudly: a ``code:`` assertion that silently
    passed would be a green acceptance signal over an unevaluated check."""


class Namespace(Mapping[str, Any]):
    """Attribute-and-item view over a JSON-ish mapping.

    ``output.pytest.failed`` and ``output['pytest']['failed']`` address the same
    value, so a casebook can read a recorded output blob without knowing whether
    the harness wrote an object or a dict. A missing key raises rather than
    returning ``None``: a typo in an assertion must fail the assertion loudly,
    not score it as a quiet pass against ``None``.
    """

    __slots__ = ("_data",)

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = dict(data)

    @staticmethod
    def _wrap(value: Any) -> Any:
        if isinstance(value, Mapping):
            return Namespace(value)
        if isinstance(value, (list, tuple)):
            return [Namespace._wrap(v) for v in value]
        return value

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._wrap(self._data[name])
        except KeyError:
            raise AttributeError(
                f"no key {name!r} in namespace (have: {sorted(self._data)})"
            ) from None

    def __getitem__(self, key: str) -> Any:
        return self._wrap(self._data[key])

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Namespace({self._data!r})"

    def get(self, key: str, default: Any = None) -> Any:
        """Mapping-style lookup with a default (the one tolerant accessor: a
        casebook that *means* "absent is fine" says so explicitly)."""
        if key in self._data:
            return self._wrap(self._data[key])
        return default


@dataclass(frozen=True)
class CodeCheckResult:
    """Outcome of one ``code:`` assertion, mirroring ``TraceCheckResult``."""

    expression: str
    passed: bool
    evidence: str
    details: dict

    def to_signal(self, *, evaluator_id: str, weight: float = 1.0) -> ScoredSignal:
        """Adapt to the scoring pillar's ``ScoredSignal`` (kind ``assertion``,
        feeding the ``task_outcome`` slot, README §15.3 / §16.1)."""
        return ScoredSignal(
            evaluator_id=evaluator_id,
            kind="assertion",
            score=1.0 if self.passed else 0.0,
            weight=weight,
            verdict="pass" if self.passed else "fail",
            slot=TASK_OUTCOME_SLOT,
            details=self.details,
            evidence=self.evidence,
        )


def is_code_assertion(text: str) -> bool:
    """True iff ``text`` is a ``code:`` assertion (README §11.3)."""
    return text.strip().startswith(_PREFIX)


def parse_code_assertion(text: str) -> ast.Expression:
    """Parse and validate ``code:<expr>`` into a checked AST.

    Raises :class:`CodeAssertionError` on a syntax error, a disallowed node
    type, or a private/dunder name: the three ways an expression could reach
    past the evaluation namespace.
    """
    body = text.strip()
    if not body.startswith(_PREFIX):
        raise CodeAssertionError(f"not a code assertion: {text!r}")
    body = body[len(_PREFIX):].strip()
    if not body:
        raise CodeAssertionError(f"empty code assertion: {text!r}")
    try:
        tree = ast.parse(body, mode="eval")
    except SyntaxError as exc:
        raise CodeAssertionError(f"malformed code assertion {body!r}: {exc}") from exc

    for node in ast.walk(tree):
        rejection = _REJECTED_NODES.get(type(node))
        if rejection is not None:
            raise CodeAssertionError(f"{rejection} (in {body!r})")
        if type(node) not in _ALLOWED_NODES:
            raise CodeAssertionError(
                f"code assertion uses disallowed construct "
                f"{type(node).__name__} in {body!r}"
            )
        name = getattr(node, "attr", None) or (
            node.id if isinstance(node, ast.Name) else None
        )
        if isinstance(name, str) and name.startswith("_"):
            raise CodeAssertionError(
                f"code assertion references private name {name!r} in {body!r}"
            )
        if isinstance(node, ast.Attribute) and node.attr in _REJECTED_ATTRS:
            raise CodeAssertionError(
                f"code assertion calls {node.attr!r}, which interprets its "
                f"format string at runtime and so reaches attributes this "
                f"allow-list cannot see (in {body!r})"
            )
    return tree


def evaluate_code_assertion(text: str, namespace: Mapping[str, Any]) -> CodeCheckResult:
    """Evaluate one ``code:`` assertion against ``namespace``.

    The expression's truthiness is the verdict. A raise inside the expression
    (a missing key, a type error) is a :class:`CodeAssertionError`, so a
    casebook typo surfaces as a loud evaluator failure rather than a fail that
    looks like a genuine product defect.
    """
    tree = parse_code_assertion(text)
    body = text.strip()[len(_PREFIX):].strip()
    env = {k: (Namespace(v) if isinstance(v, Mapping) else v) for k, v in namespace.items()}
    env["__builtins__"] = dict(SAFE_BUILTINS)
    try:
        value = eval(compile(tree, "<code-assertion>", "eval"), env, {})  # noqa: S307
    except CodeAssertionError:
        raise
    except Exception as exc:  # noqa: BLE001 - any raise is an evaluator failure
        raise CodeAssertionError(
            f"code assertion {body!r} raised {type(exc).__name__}: {exc}"
        ) from exc
    passed = bool(value)
    return CodeCheckResult(
        expression=body,
        passed=passed,
        evidence=(
            f"{body} -> {value!r}" if passed else f"{body} -> {value!r} (falsy)"
        ),
        details={"expression": body, "value": repr(value)},
    )
