"""
boolean_search.py — tiny boolean query language for job title search.

Supports:
    AND, OR, NOT   (case-insensitive; bare whitespace between terms = implicit AND)
    "quoted phrases"
    (parentheses for grouping)

Examples:
    product manager AND (revenue OR pricing) NOT healthcare
    "product manager" OR "program manager"
    salesforce NOT (intern OR internship)

Precedence (highest to lowest): NOT, AND, OR. Parentheses override.
Matching is case-insensitive substring matching against a target string
(job title). Multi-word bare terms without quotes are treated as separate
implicitly-ANDed words, e.g. `product manager` == `product AND manager`.
Use quotes to require the exact phrase: `"product manager"`.
"""

import re

TOKEN_RE = re.compile(r'"[^"]*"|\(|\)|\bAND\b|\bOR\b|\bNOT\b|[^\s()"]+', re.IGNORECASE)


def tokenize(query):
    tokens = []
    for m in TOKEN_RE.finditer(query or ""):
        t = m.group(0)
        if t.strip():
            tokens.append(t)
    return tokens


class _Parser:
    """Recursive-descent parser.
    expr    := or_expr
    or_expr := and_expr (OR and_expr)*
    and_expr:= not_expr ((AND)? not_expr)*      # implicit AND
    not_expr:= NOT not_expr | atom
    atom    := '(' expr ')' | TERM
    """

    def __init__(self, tokens):
        self.tokens = tokens
        self.i = 0

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def next(self):
        t = self.peek()
        self.i += 1
        return t

    def is_kw(self, tok, kw):
        return tok is not None and tok.upper() == kw

    def parse(self):
        if not self.tokens:
            return None
        node = self.or_expr()
        return node

    def or_expr(self):
        left = self.and_expr()
        while self.is_kw(self.peek(), "OR"):
            self.next()
            right = self.and_expr()
            left = ("OR", left, right)
        return left

    def and_expr(self):
        left = self.not_expr()
        while True:
            tok = self.peek()
            if tok is None or self.is_kw(tok, "OR") or tok == ")":
                break
            if self.is_kw(tok, "AND"):
                self.next()
                right = self.not_expr()
                left = ("AND", left, right)
            else:
                # implicit AND between adjacent atoms
                right = self.not_expr()
                left = ("AND", left, right)
        return left

    def not_expr(self):
        if self.is_kw(self.peek(), "NOT"):
            self.next()
            return ("NOT", self.not_expr())
        return self.atom()

    def atom(self):
        tok = self.next()
        if tok == "(":
            node = self.or_expr()
            if self.peek() == ")":
                self.next()
            return node
        if tok is None:
            return ("TERM", "")
        if tok.startswith('"') and tok.endswith('"') and len(tok) >= 2:
            return ("TERM", tok[1:-1].lower())
        return ("TERM", tok.lower())


def parse_query(query):
    """Parse a boolean query string into an AST. Returns None for empty input."""
    tokens = tokenize(query)
    if not tokens:
        return None
    return _Parser(tokens).parse()


def evaluate(ast, text):
    """Evaluate a parsed AST against a lowercase-able text string."""
    if ast is None:
        return True
    tl = text.lower()
    kind = ast[0]
    if kind == "TERM":
        term = ast[1]
        return term in tl if term else True
    if kind == "AND":
        return evaluate(ast[1], text) and evaluate(ast[2], text)
    if kind == "OR":
        return evaluate(ast[1], text) or evaluate(ast[2], text)
    if kind == "NOT":
        return not evaluate(ast[1], text)
    return True


def leaf_terms(ast):
    """Collect all literal TERM strings from an AST (used for a cheap SQL prefilter)."""
    if ast is None:
        return []
    kind = ast[0]
    if kind == "TERM":
        return [ast[1]] if ast[1] else []
    if kind == "NOT":
        return []  # don't use negated terms to prefilter
    return leaf_terms(ast[1]) + leaf_terms(ast[2])
