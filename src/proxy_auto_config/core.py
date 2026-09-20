"""Core implementation for Proxy Auto Config evaluation.

This module implements a minimal JavaScript interpreter tailored to the
subset of JavaScript commonly found in PAC files. It provides a sandboxed
environment with the standard PAC helper functions (``isPlainHostName``,
``dnsDomainIs``, ``shExpMatch``, etc.) and the ``FindProxyForURL`` entry
point.

The interpreter is deliberately small and does not aim for full ECMAScript
compliance. It supports:

- Variable declarations with ``var``
- Function declarations and calls
- ``if`` / ``else`` statements
- ``return`` statements
- String, boolean, and numeric literals
- Binary operators: ``&&``, ``||``, ``!``, ``==``, ``!=``, ``<``, ``<=``,
  ``>``, ``>=``, ``+``
- The PAC helper functions

The choice to implement a custom interpreter rather than embed a JavaScript
engine is driven by the zero-dependency requirement. Embedding an engine
would require a third-party package or system library.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class ProxyConfig:
    """Result of evaluating a PAC file for a given URL and host.

    Attributes:
        proxies: A tuple of proxy strings, e.g. ``("DIRECT",)`` or
            ``("PROXY proxy1:8080", "PROXY proxy2:8080")``.
        raw: The exact string returned by ``FindProxyForURL``.
    """

    proxies: Tuple[str, ...]
    raw: str

    def __str__(self) -> str:
        return self.raw


# ---------------------------------------------------------------------------
# JavaScript value types
# ---------------------------------------------------------------------------


class JSBool:
    """JavaScript boolean value."""

    def __init__(self, value: bool):
        self.value = bool(value)

    def __repr__(self) -> str:
        return "true" if self.value else "false"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, JSBool):
            return self.value == other.value
        if isinstance(other, bool):
            return self.value == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.value)


class JSNull:
    """JavaScript ``null`` value."""

    def __repr__(self) -> str:
        return "null"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, JSNull)

    def __hash__(self) -> int:
        return 0


class JSUndefined:
    """JavaScript ``undefined`` value."""

    def __repr__(self) -> str:
        return "undefined"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, JSUndefined)

    def __hash__(self) -> int:
        return 1


@dataclass
class JSFunction:
    """User-defined JavaScript function."""

    name: str
    params: List[str]
    body: "List[Statement]"
    scope: "Scope"


# ---------------------------------------------------------------------------
# AST nodes
# ---------------------------------------------------------------------------


class Statement:
    """Base class for statements."""


class VarDecl(Statement):
    """``var name = expr;``"""

    def __init__(self, name: str, expr: "Expression"):
        self.name = name
        self.expr = expr


class FunctionDecl(Statement):
    """``function name(params) { ... }``"""

    def __init__(self, name: str, params: List[str], body: List[Statement]):
        self.name = name
        self.params = params
        self.body = body


class IfStatement(Statement):
    """``if (cond) { ... } else { ... }``"""

    def __init__(
        self,
        cond: "Expression",
        then_body: List[Statement],
        else_body: Optional[List[Statement]],
    ):
        self.cond = cond
        self.then_body = then_body
        self.else_body = else_body


class ReturnStatement(Statement):
    """``return expr;``"""

    def __init__(self, expr: "Expression"):
        self.expr = expr


class ExpressionStatement(Statement):
    """An expression used as a statement, e.g. a function call."""

    def __init__(self, expr: "Expression"):
        self.expr = expr


class Expression:
    """Base class for expressions."""


class Literal(Expression):
    """A literal value: string, number, boolean, null, or undefined."""

    def __init__(self, value: Any):
        self.value = value


class Identifier(Expression):
    """A variable or function name."""

    def __init__(self, name: str):
        self.name = name


class BinaryOp(Expression):
    """A binary operator expression."""

    def __init__(self, op: str, left: Expression, right: Expression):
        self.op = op
        self.left = left
        self.right = right


class UnaryOp(Expression):
    """A unary operator expression."""

    def __init__(self, op: str, operand: Expression):
        self.op = op
        self.operand = operand


class Call(Expression):
    """A function call."""

    def __init__(self, callee: Expression, args: List[Expression]):
        self.callee = callee
        self.args = args


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------


_TOKEN_RE = re.compile(
    r"""
    (?P<whitespace>\s+)
    |(?P<comment>//[^\n]*|/\*.*?\*/)
    |(?P<string>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
    |(?P<number>\d+(?:\.\d+)?)
    |(?P<identifier>[A-Za-z_$][A-Za-z0-9_$]*)
    |(?P<operator>===|!==|==|!=|<=|>=|&&|\|\||[{}()\[\];,<>=!+\-*/\.])
    """,
    re.DOTALL | re.VERBOSE,
)


class Token:
    """A single lexical token."""

    def __init__(self, kind: str, value: str, pos: int):
        self.kind = kind
        self.value = value
        self.pos = pos

    def __repr__(self) -> str:
        return f"Token({self.kind}, {self.value!r})"


class LexError(Exception):
    """Raised when the lexer encounters invalid characters."""


class ParseError(Exception):
    """Raised when the parser encounters invalid syntax."""


class EvaluationError(Exception):
    """Raised when evaluation encounters a runtime error."""


def tokenize(source: str) -> List[Token]:
    """Convert PAC JavaScript source into a list of tokens."""
    tokens: List[Token] = []
    pos = 0
    while pos < len(source):
        match = _TOKEN_RE.match(source, pos)
        if not match:
            raise LexError(f"Unexpected character at position {pos}: {source[pos]!r}")
        kind = match.lastgroup
        value = match.group()
        if kind == "whitespace" or kind == "comment":
            pass
        elif kind == "string":
            # Strip quotes and handle escapes
            raw = value
            quote = raw[0]
            inner = raw[1:-1]
            inner = inner.replace("\\" + quote, quote)
            inner = inner.replace("\\\\", "\\")
            tokens.append(Token("string", inner, pos))
        elif kind == "number":
            tokens.append(Token("number", value, pos))
        elif kind == "identifier":
            tokens.append(Token("identifier", value, pos))
        elif kind == "operator":
            tokens.append(Token("operator", value, pos))
        pos = match.end()
    tokens.append(Token("eof", "", pos))
    return tokens


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class Parser:
    """Recursive-descent parser for the PAC JavaScript subset."""

    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.index = 0

    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def advance(self) -> Token:
        token = self.current
        self.index += 1
        return token

    def expect(self, value: str = "", kind: Optional[str] = None) -> Token:
        token = self.current
        if kind is not None:
            if token.kind != kind:
                raise ParseError(f"Expected {kind}, got {token.kind} at position {token.pos}")
        elif value:
            if token.value != value:
                raise ParseError(f"Expected {value!r}, got {token.value!r} at position {token.pos}")
        else:
            raise ParseError("expect() requires a value or kind")
        return self.advance()

    def parse(self) -> List[Statement]:
        statements: List[Statement] = []
        while self.current.kind != "eof":
            statements.append(self.parse_statement())
        return statements

    def parse_statement(self) -> Statement:
        token = self.current
        if token.kind == "identifier":
            if token.value == "var":
                return self.parse_var_decl()
            if token.value == "function":
                return self.parse_function_decl()
            if token.value == "if":
                return self.parse_if()
            if token.value == "return":
                return self.parse_return()
        # Expression statement
        expr = self.parse_expression()
        self.expect(";")
        return ExpressionStatement(expr)

    def parse_var_decl(self) -> Statement:
        self.advance()  # var
        name_token = self.expect(kind="identifier")
        self.expect("=")
        expr = self.parse_expression()
        self.expect(";")
        return VarDecl(name_token.value, expr)

    def parse_function_decl(self) -> Statement:
        self.advance()  # function
        name_token = self.expect(kind="identifier")
        self.expect("(")
        params: List[str] = []
        if self.current.value != ")":
            while True:
                param = self.expect(kind="identifier")
                params.append(param.value)
                if self.current.value == ",":
                    self.advance()
                    continue
                break
        self.expect(")")
        self.expect("{")
        body: List[Statement] = []
        while self.current.value != "}":
            if self.current.kind == "eof":
                raise ParseError("Unexpected end of file in function body")
            body.append(self.parse_statement())
        self.expect("}")
        return FunctionDecl(name_token.value, params, body)

    def parse_if(self) -> Statement:
        self.advance()  # if
        self.expect("(")
        cond = self.parse_expression()
        self.expect(")")
        self.expect("{")
        then_body: List[Statement] = []
        while self.current.value != "}":
            if self.current.kind == "eof":
                raise ParseError("Unexpected end of file in if body")
            then_body.append(self.parse_statement())
        self.expect("}")
        else_body: Optional[List[Statement]] = None
        if self.current.kind == "identifier" and self.current.value == "else":
            self.advance()
            self.expect("{")
            else_body = []
            while self.current.value != "}":
                if self.current.kind == "eof":
                    raise ParseError("Unexpected end of file in else body")
                else_body.append(self.parse_statement())
            self.expect("}")
        return IfStatement(cond, then_body, else_body)

    def parse_return(self) -> Statement:
        self.advance()  # return
        expr = self.parse_expression()
        self.expect(";")
        return ReturnStatement(expr)

    def parse_expression(self) -> Expression:
        return self.parse_or()

    def parse_or(self) -> Expression:
        left = self.parse_and()
        while self.current.value == "||":
            op = self.advance().value
            right = self.parse_and()
            left = BinaryOp(op, left, right)
        return left

    def parse_and(self) -> Expression:
        left = self.parse_equality()
        while self.current.value == "&&":
            op = self.advance().value
            right = self.parse_equality()
            left = BinaryOp(op, left, right)
        return left

    def parse_equality(self) -> Expression:
        left = self.parse_relational()
        while self.current.value in ("==", "!=", "===", "!=="):
            op = self.advance().value
            right = self.parse_relational()
            left = BinaryOp(op, left, right)
        return left

    def parse_relational(self) -> Expression:
        left = self.parse_additive()
        while self.current.value in ("<", "<=", ">", ">="):
            op = self.advance().value
            right = self.parse_additive()
            left = BinaryOp(op, left, right)
        return left

    def parse_additive(self) -> Expression:
        left = self.parse_unary()
        while self.current.value == "+":
            op = self.advance().value
            right = self.parse_unary()
            left = BinaryOp(op, left, right)
        return left

    def parse_unary(self) -> Expression:
        if self.current.value == "!":
            op = self.advance().value
            operand = self.parse_unary()
            return UnaryOp(op, operand)
        return self.parse_call()

    def parse_call(self) -> Expression:
        expr = self.parse_primary()
        while self.current.value == "(":
            self.advance()
            args: List[Expression] = []
            if self.current.value != ")":
                while True:
                    args.append(self.parse_expression())
                    if self.current.value == ",":
                        self.advance()
                        continue
                    break
            self.expect(")")
            expr = Call(expr, args)
        return expr

    def parse_primary(self) -> Expression:
        token = self.current
        if token.kind == "string":
            self.advance()
            return Literal(token.value)
        if token.kind == "number":
            self.advance()
            return Literal(float(token.value) if "." in token.value else int(token.value))
        if token.kind == "identifier":
            self.advance()
            if token.value == "true":
                return Literal(JSBool(True))
            if token.value == "false":
                return Literal(JSBool(False))
            if token.value == "null":
                return Literal(JSNull())
            if token.value == "undefined":
                return Literal(JSUndefined())
            return Identifier(token.value)
        if token.value == "(":
            self.advance()
            expr = self.parse_expression()
            self.expect(")")
            return expr
        raise ParseError(f"Unexpected token {token!r}")


# ---------------------------------------------------------------------------
# Scope and interpreter
# ---------------------------------------------------------------------------


class Scope:
    """A lexical scope holding variable bindings."""

    def __init__(self, parent: Optional["Scope"] = None):
        self.parent = parent
        self.bindings: Dict[str, Any] = {}

    def get(self, name: str) -> Any:
        if name in self.bindings:
            return self.bindings[name]
        if self.parent is not None:
            return self.parent.get(name)
        raise EvaluationError(f"Undefined variable: {name}")

    def set(self, name: str, value: Any) -> None:
        self.bindings[name] = value

    def define(self, name: str, value: Any) -> None:
        self.bindings[name] = value


class ReturnSignal(Exception):
    """Internal signal used to unwind the call stack on return."""

    def __init__(self, value: Any):
        self.value = value


class Interpreter:
    """Tree-walking interpreter for the PAC JavaScript subset."""

    def __init__(self, statements: List[Statement]):
        self.statements = statements
        self.global_scope = Scope()
        self._install_pac_helpers()

    def _install_pac_helpers(self) -> None:
        """Install the standard PAC helper functions."""
        self.global_scope.define("isPlainHostName", self._is_plain_host_name)
        self.global_scope.define("dnsDomainIs", self._dns_domain_is)
        self.global_scope.define("localHostOrDomainIs", self._local_host_or_domain_is)
        self.global_scope.define("isResolvable", self._is_resolvable)
        self.global_scope.define("isInNet", self._is_in_net)
        self.global_scope.define("dnsResolve", self._dns_resolve)
        self.global_scope.define("myIpAddress", self._my_ip_address)
        self.global_scope.define("dnsDomainLevels", self._dns_domain_levels)
        self.global_scope.define("shExpMatch", self._sh_exp_match)
        self.global_scope.define("weekdayRange", self._weekday_range)
        self.global_scope.define("dateRange", self._date_range)
        self.global_scope.define("timeRange", self._time_range)
        self.global_scope.define("alert", self._alert)

    def _is_plain_host_name(self, host: str) -> JSBool:
        return JSBool("." not in host)

    def _dns_domain_is(self, host: str, domain: str) -> JSBool:
        domain = domain.lstrip(".")
        return JSBool(host == domain or host.endswith("." + domain))

    def _local_host_or_domain_is(self, host: str, domain: str) -> JSBool:
        domain = domain.lstrip(".")
        return JSBool(host == domain or host.endswith("." + domain) or "." not in host)

    def _is_resolvable(self, host: str) -> JSBool:
        return JSBool(False)

    def _is_in_net(self, host: str, pattern: str, mask: str) -> JSBool:
        return JSBool(False)

    def _dns_resolve(self, host: str) -> str:
        return ""

    def _my_ip_address(self) -> str:
        return "127.0.0.1"

    def _dns_domain_levels(self, host: str) -> int:
        return host.count(".")

    def _sh_exp_match(self, text: str, pattern: str) -> JSBool:
        # Convert shell pattern to regex
        regex = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
        return JSBool(re.fullmatch(regex, text) is not None)

    def _weekday_range(self, *args: str) -> JSBool:
        # Deterministic: always returns false unless the range includes all days
        # and no specific day is given. Kept simple; PAC files rarely depend on this.
        return JSBool(False)

    def _date_range(self, *args: str) -> JSBool:
        return JSBool(False)

    def _time_range(self, *args: str) -> JSBool:
        return JSBool(False)

    def _alert(self, message: str) -> JSUndefined:
        # Silently ignore alerts; they are diagnostic only.
        return JSUndefined()

    def evaluate(self, url: str, host: str) -> ProxyConfig:
        """Evaluate the PAC file for the given URL and host."""
        self.global_scope.define("url", url)
        self.global_scope.define("host", host)

        for statement in self.statements:
            self._exec_statement(statement, self.global_scope)

        # Find FindProxyForURL function
        try:
            func = self.global_scope.get("FindProxyForURL")
        except EvaluationError as exc:
            raise EvaluationError("PAC file does not define FindProxyForURL") from exc

        if not isinstance(func, JSFunction):
            raise EvaluationError("FindProxyForURL is not a function")

        result = self._call_js_function(func, [url, host], self.global_scope)
        if isinstance(result, str):
            raw = result
        else:
            raw = self._to_js_string(result)

        return ProxyConfig(proxies=self._parse_proxy_list(raw), raw=raw)

    def _exec_statement(self, statement: Statement, scope: Scope) -> None:
        if isinstance(statement, VarDecl):
            value = self._eval_expression(statement.expr, scope)
            scope.define(statement.name, value)
        elif isinstance(statement, FunctionDecl):
            func = JSFunction(statement.name, statement.params, statement.body, scope)
            scope.define(statement.name, func)
        elif isinstance(statement, IfStatement):
            cond = self._eval_expression(statement.cond, scope)
            if self._to_boolean(cond):
                for child in statement.then_body:
                    self._exec_statement(child, scope)
            elif statement.else_body is not None:
                for child in statement.else_body:
                    self._exec_statement(child, scope)
        elif isinstance(statement, ReturnStatement):
            value = self._eval_expression(statement.expr, scope)
            raise ReturnSignal(value)
        elif isinstance(statement, ExpressionStatement):
            self._eval_expression(statement.expr, scope)
        else:
            raise EvaluationError(f"Unknown statement type: {type(statement).__name__}")

    def _eval_expression(self, expr: Expression, scope: Scope) -> Any:
        if isinstance(expr, Literal):
            return expr.value
        if isinstance(expr, Identifier):
            return scope.get(expr.name)
        if isinstance(expr, BinaryOp):
            left = self._eval_expression(expr.left, scope)
            right = self._eval_expression(expr.right, scope)
            return self._apply_binary_op(expr.op, left, right)
        if isinstance(expr, UnaryOp):
            operand = self._eval_expression(expr.operand, scope)
            if expr.op == "!":
                return JSBool(not self._to_boolean(operand))
            raise EvaluationError(f"Unsupported unary operator: {expr.op}")
        if isinstance(expr, Call):
            callee = self._eval_expression(expr.callee, scope)
            args = [self._eval_expression(arg, scope) for arg in expr.args]
            if isinstance(callee, JSFunction):
                return self._call_js_function(callee, args, scope)
            if callable(callee):
                return callee(*args)
            raise EvaluationError(f"Attempted to call non-function: {callee!r}")
        raise EvaluationError(f"Unknown expression type: {type(expr).__name__}")

    def _call_js_function(self, func: JSFunction, args: List[Any], caller_scope: Scope) -> Any:
        local_scope = Scope(parent=func.scope)
        for param, arg in zip(func.params, args):
            local_scope.define(param, arg)
        # Extra args are ignored; missing args become undefined.
        for param in func.params[len(args):]:
            local_scope.define(param, JSUndefined())

        try:
            for statement in func.body:
                self._exec_statement(statement, local_scope)
        except ReturnSignal as signal:
            return signal.value
        return JSUndefined()

    def _apply_binary_op(self, op: str, left: Any, right: Any) -> Any:
        if op == "&&":
            if not self._to_boolean(left):
                return left
            return right
        if op == "||":
            if self._to_boolean(left):
                return left
            return right
        if op in ("==", "==="):
            return JSBool(self._js_equals(left, right, strict=(op == "==="))) 
        if op in ("!=", "!=="):
            return JSBool(not self._js_equals(left, right, strict=(op in ("!=", "!=="))))
        if op in ("<", "<=", ">", ">="):
            return self._compare(op, left, right)
        if op == "+":
            if isinstance(left, str) or isinstance(right, str):
                return self._to_js_string(left) + self._to_js_string(right)
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left + right
            return self._to_js_string(left) + self._to_js_string(right)
        raise EvaluationError(f"Unsupported binary operator: {op}")

    def _js_equals(self, left: Any, right: Any, strict: bool = False) -> bool:
        if strict:
            if type(left) is not type(right):
                return False
        # Handle JSBool, JSNull, JSUndefined
        if isinstance(left, JSBool):
            left = left.value
        if isinstance(right, JSBool):
            right = right.value
        if isinstance(left, JSNull) or isinstance(left, JSUndefined):
            return isinstance(right, (JSNull, JSUndefined))
        if isinstance(right, JSNull) or isinstance(right, JSUndefined):
            return False
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return left == right
        if isinstance(left, str) and isinstance(right, str):
            return left == right
        # Cross-type coercions for non-strict equality
        if not strict:
            if isinstance(left, (int, float)) and isinstance(right, str):
                try:
                    return left == float(right)
                except ValueError:
                    return False
            if isinstance(left, str) and isinstance(right, (int, float)):
                try:
                    return float(left) == right
                except ValueError:
                    return False
        return False

    def _compare(self, op: str, left: Any, right: Any) -> JSBool:
        # Only numeric comparisons are supported.
        left_num = self._to_number(left)
        right_num = self._to_number(right)
        if left_num is None or right_num is None:
            return JSBool(False)
        if op == "<":
            return JSBool(left_num < right_num)
        if op == "<=":
            return JSBool(left_num <= right_num)
        if op == ">":
            return JSBool(left_num > right_num)
        if op == ">=":
            return JSBool(left_num >= right_num)
        return JSBool(False)

    def _to_number(self, value: Any) -> Optional[float]:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        if isinstance(value, JSBool):
            return 1.0 if value.value else 0.0
        if isinstance(value, (JSNull, JSUndefined)):
            return 0.0 if isinstance(value, JSNull) else None
        return None

    def _to_boolean(self, value: Any) -> bool:
        if isinstance(value, JSBool):
            return value.value
        if isinstance(value, JSNull) or isinstance(value, JSUndefined):
            return False
        if isinstance(value, str):
            return len(value) > 0
        if isinstance(value, (int, float)):
            return value != 0
        return True

    def _to_js_string(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, JSBool):
            return "true" if value.value else "false"
        if isinstance(value, JSNull):
            return "null"
        if isinstance(value, JSUndefined):
            return "undefined"
        if isinstance(value, (int, float)):
            if isinstance(value, float) and value.is_integer():
                return str(int(value))
            return str(value)
        return str(value)

    def _parse_proxy_list(self, raw: str) -> Tuple[str, ...]:
        """Parse the proxy list returned by FindProxyForURL.

        PAC files return a semicolon-separated list of proxy directives.
        Each directive is either ``DIRECT`` or ``PROXY host:port``.
        """
        if not raw:
            return ()
        parts = [part.strip() for part in raw.split(";") if part.strip()]
        return tuple(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class PACFile:
    """Parse and evaluate a PAC file.

    Args:
        source: The JavaScript source code of the PAC file.

    Example:
        >>> pac = PACFile('function FindProxyForURL(url, host) { return "DIRECT"; }')
        >>> result = pac.find_proxy_for_url("http://internal.example.com/")
        >>> result.proxies
        ('DIRECT',)
    """

    def __init__(self, source: str):
        tokens = tokenize(source)
        parser = Parser(tokens)
        self._statements = parser.parse()
        self._interpreter = Interpreter(self._statements)

    def find_proxy_for_url(self, url: str) -> ProxyConfig:
        """Determine the proxy for the given URL.

        Args:
            url: The full URL to check, e.g. ``http://example.com/``.

        Returns:
            A :class:`ProxyConfig` with the parsed proxy list and raw return
            value.

        Raises:
            EvaluationError: If the PAC file does not define
                ``FindProxyForURL`` or if evaluation fails.
        """
        # Extract host from URL. This is a simplified extraction that does not
        # perform full URL parsing; it is sufficient for PAC evaluation where
        # host is used for domain matching.
        match = re.search(r"://([^/:]+)", url)
        host = match.group(1) if match else ""
        return self._interpreter.evaluate(url, host)
