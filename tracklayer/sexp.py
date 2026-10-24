"""Minimal S-expression reader for KiCad files.

Parses into nested Python lists. Bare atoms and quoted strings both come back as `str`;
numbers are converted on demand with `num()`. Quoted strings keep their escapes resolved.
"""
from __future__ import annotations


class Sexp(list):
    """A parsed list node. `node.key` is the first atom, e.g. "footprint".
    `node.span` is the (start, end) character range of the node in the source text."""
    span: tuple[int, int] = (0, 0)

    @property
    def key(self) -> str | None:
        return self[0] if self and isinstance(self[0], str) else None

    def find(self, key: str) -> "Sexp | None":
        for it in self:
            if isinstance(it, Sexp) and it.key == key:
                return it
        return None

    def find_all(self, key: str):
        for it in self:
            if isinstance(it, Sexp) and it.key == key:
                yield it

    def atoms(self):
        """The bare (non-list) items after the key."""
        return [it for it in self[1:] if not isinstance(it, Sexp)]

    def value(self, key: str, default=None):
        """First atom of the child `key`, as a string, or default."""
        n = self.find(key)
        if n is None:
            return default
        a = n.atoms()
        return a[0] if a else default

    def values(self, key: str, default=()):
        n = self.find(key)
        return n.atoms() if n is not None else list(default)


def num(s, default=0.0) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def parse(text: str) -> Sexp:
    """Parse one top-level S-expression."""
    i, n = 0, len(text)
    stack: list[Sexp] = []
    root: Sexp | None = None
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c == '(':
            node = Sexp()
            node.span = (i, i)
            if stack:
                stack[-1].append(node)
            else:
                root = node
            stack.append(node)
            i += 1
        elif c == ')':
            done = stack.pop()
            done.span = (done.span[0], i + 1)
            i += 1
            if not stack:
                break
        elif c == '"':
            j = i + 1
            out = []
            while j < n:
                ch = text[j]
                if ch == '\\' and j + 1 < n:
                    nxt = text[j + 1]
                    out.append({'n': '\n', 't': '\t', 'r': '\r'}.get(nxt, nxt))
                    j += 2
                elif ch == '"':
                    break
                else:
                    out.append(ch)
                    j += 1
            stack[-1].append(''.join(out))
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '()':
                j += 1
            stack[-1].append(text[i:j])
            i = j
    if root is None:
        raise ValueError('no S-expression found')
    return root


def quote(s: str) -> str:
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'


def fmt(v: float) -> str:
    """Number formatting the way KiCad writes it (no trailing zeros, up to 6 dp)."""
    s = f'{v:.6f}'.rstrip('0').rstrip('.')
    return '0' if s in ('', '-0') else s
