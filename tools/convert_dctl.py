#!/usr/bin/env python3
"""convert_dctl.py - Convert an OpenDRT DCTL file into a semantic spec JSON.

One DCTL -> one JSON describing the *transform* itself:
  parameters  - full UI surface: active preset-mode params + StickShift
                params (same file, commented-out block), tooltips attached
  selection   - the preset-mode control flow of transform(), lifted into
                branch tables: look / tonescale / creative white /
                display encoding / input gamut
  constants   - matrix #defines and __CONSTANT__ globals
  entry_point - transform() signature
  functions   - every other __DEVICE__ function as ordered statement IR
  pipeline    - transform()'s per-pixel body in execution order

This file knows NOTHING about Blender. Downstream tools consume the JSON.

Verification:
  - every statement carries its DCTL source line(s); selection entries too
  - line-coverage report: every physical line gets exactly one class;
    unclassifiable content fails conversion instead of silently vanishing
  - symbol-resolution pass: every identifier, target, call and arity in the
    IR must resolve to a parameter, constant, arg, declared local, or
    known operator (C-like block scoping)

IR expression grammar (nested JSON arrays, op = first entry, a string):
    ["add", a, b] ["sub", a, b] ["mul", a, b] ["div", a, b] ["mod", a, b]
    ["neg", a]
    ["lt", a, b] ["le", a, b] ["gt", a, b] ["ge", a, b] ["eq", a, b]
    ["ne", a, b] ["and", a, b] ["or", a, b]
    ["cond", c, a, b]                 (C ternary)
    ["attr", base, "x"]               (member read)
    ["f2", a, b] ["f3", a, b, c] ["m3x3", a, b, c]
    ["cast_float", a]                 (recorded for fidelity; value no-op)
    ["pow", a, b] ["exp", a] ["exp2", a] ["exp10", a] ["ln", a]
    ["log2", a] ["sqrt", a] ["abs", a] ["min", a, b] ["max", a, b]
    ["fmod", a, b] ["atan2", a, b]
  every other op: call to a user __DEVICE__ function of the same file.
  numbers: JSON numbers (int stays int); identifiers: plain strings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

SCHEMA_VERSION = "1"

PRIMITIVE_TYPES = {"float", "int", "float2", "float3", "float3x3"}

# DCTL compiler intrinsics -> neutral IR operator names. Every other call is
# a user __DEVICE__ function defined in the same file.
INTRINSICS: dict[str, str] = {
    "_powf": "pow",
    "powf": "pow",
    "_expf": "exp",
    "expf": "exp",
    "_exp2f": "exp2",
    "_exp10f": "exp10",
    "_logf": "ln",
    "logf": "ln",
    "_log10f": "log10",
    "_log2f": "log2",
    "_sqrtf": "sqrt",
    "sqrtf": "sqrt",
    "_fabs": "abs",
    "_fabsf": "abs",
    "fabs": "abs",
    "fabsf": "abs",
    "_fminf": "min",
    "fminf": "min",
    "_fmaxf": "max",
    "fmaxf": "max",
    "_fmod": "fmod",
    "fmodf": "fmod",
    "_atan2f": "atan2",
    "atan2f": "atan2",
    "make_float2": "f2",
    "make_float3": "f3",
    "make_float3x3": "m3x3",
}
CONSTRUCTORS = {"f2": 2, "f3": 3, "m3x3": 3}
UNARY_OPS = ("exp", "exp2", "exp10", "ln", "log2", "log10", "sqrt", "abs", "neg")
BINARY_OPS = ("pow", "min", "max", "fmod", "atan2",
              "add", "sub", "mul", "div", "mod",
              "lt", "le", "gt", "ge", "eq", "ne", "and", "or")

# Selection if-chain groups: driving UI parameter id -> group name.
SELECTION_VARS = {
    "look_preset": "look",
    "tonescale_preset": "tonescale",
    "_cwp": "creative_white",
    "display_encoding_preset": "display_encoding",
    "in_gamut": "input_gamut",
}

STATEMENT_KINDS: list[str] = [
    "declare: {type, names[], inits?{name: expr}, qualifier?=const}",
    "assign: {ops: [{tgt: dotted-path, expr}]}; compound ops pre-expanded to base ops",
    "return: {expr | null}",
    "if: {branches: [{cond, body[], note?, src}], els?: body[]}",
    "stage: banner comment name (reading aid inside statement lists)",
    "select: pointer to spec.selection[group], placed where the DCTL applies the preset chain",
    "skipped_preprocessor: region dropped with reason",
    "pass_through_ifdef/if: preprocessor guard with no semantic effect",
]

OP_DOC = (
    "add/sub/mul/div/mod: arithmetic with C scalar/vector promotion | "
    "lt/le/gt/ge/eq/ne: comparisons | and/or: boolean | cond: ternary "
    "[cond, a, b] | attr: [attr, base, comp] | f2/f3/m3x3: constructors | "
    "cast_float: recorded no-op | pow/exp/exp2/exp10/ln/log2/sqrt/abs/min/"
    "max/fmod/atan2: intrinsics | other op names: user function call, see "
    "spec.functions"
)


class ParseError(Exception):
    pass


# ---------------------------------------------------------------------------
# Comment handling
# ---------------------------------------------------------------------------


def strip_line_comment(raw: str) -> tuple[str, str | None]:
    """Cut one //comment. DCTL code contains no string literals."""
    idx = raw.find("//")
    if idx == -1:
        return raw, None
    return raw[:idx], raw[idx + 2 :].strip()


def make_block_record(start: int, end: int, text: str) -> dict:
    """Classify a removed block comment: banner (short, dash-bordered) or prose."""
    content = [ln.strip("/*-=*_ \t\r") for ln in text.splitlines()]
    meaningful = [ln for ln in content if ln and not re.fullmatch(r"[-*=_\s]*", ln)]
    bordered = bool(re.search(r"[-*=_]{3,}", text))
    banner = bordered and len(meaningful) <= 2
    return {
        "start": start,
        "end": end,
        "text": text.strip(),
        "banner": banner,
        "name": " ".join(meaningful)[:90] if banner else None,
    }


def block_comment_pass(raw_lines: list[str]) -> tuple[list[str], list[dict]]:
    """Strip /*...*/ spans (may span lines). Returns (code_lines, records).

    The returned code lines have block comments removed and per-line
    // comments removed. Line indices throughout are 1-based.
    """
    code: list[str] = []
    records: list[dict] = []
    in_block = False
    buf: list[str] = []
    start = 0
    for lineno, raw in enumerate(raw_lines, 1):
        if in_block:
            end = raw.find("*/")
            if end == -1:
                buf.append(raw)
                code.append("")
            else:
                records.append(make_block_record(start, lineno, "\n".join(buf) + raw[:end]))
                tail, _ = strip_line_comment(raw[end + 2 :])
                code.append(tail)
                in_block = False
        else:
            open_idx = raw.find("/*")
            if open_idx == -1:
                code_line, _ = strip_line_comment(raw)
                code.append(code_line)
            else:
                end = raw.find("*/", open_idx + 2)
                if end != -1:
                    records.append(make_block_record(lineno, lineno, raw[open_idx + 2 : end]))
                    head, _ = strip_line_comment(raw[:open_idx])
                    tail, _ = strip_line_comment(raw[end + 2 :])
                    code.append((head + " " + tail).strip() if tail.strip() else head)
                else:
                    in_block = True
                    start = lineno
                    buf = [raw[open_idx + 2 :]]
                    code.append(raw[:open_idx])
    if in_block:
        raise ParseError(f"unterminated block comment starting line {start}")
    return code, records


# ---------------------------------------------------------------------------
# UI parameters + tooltips
# ---------------------------------------------------------------------------

UI_PARAMS_RE = re.compile(r"DEFINE_UI_PARAMS")
UI_TOOLTIP_RE = re.compile(
    r'DEFINE_UI_TOOLTIP\s*\(\s*([^",\s][^,]*?)\s*,\s*"((?:[^"\\]|\\.)*)"\s*\)'
)

KIND_MAP = {
    "DCTLUI_SLIDER_FLOAT": "slider_float",
    "DCTLUI_CHECK_BOX": "check_box",
    "DCTLUI_COMBO_BOX": "enum",
}


def balanced_call(text: str, at_paren: int) -> str:
    """text[at_paren] == '(' -> contents of the balanced '(...)'."""
    depth = 0
    for i in range(at_paren, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[at_paren + 1 : i]
    raise ParseError(f"unbalanced parens at {text[:80]!r}...")


def split_top_commas(s: str) -> list[str]:
    """Split on commas that are not inside {}, () or quotes."""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    cur: list[str] = []
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in '{("':
            depth += 1
            if ch == '"':
                quote = ch
        elif ch in '})':
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    else:
        parts.append("")
    return parts


NUM_RE = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?[fF]?")


def num(text: str) -> float | int:
    text = text.strip()
    if not NUM_RE.fullmatch(text):
        raise ParseError(f"not a number literal: {text!r}")
    core = text[:-1] if text[-1] in "fF" else text
    if "." in core or "e" in core.lower():
        return float(core)
    return int(core)


def parse_brace_list(s: str) -> list[str]:
    s = s.strip()
    if not (s.startswith("{") and s.endswith("}")):
        raise ParseError(f"expected {{...}} list, got {s[:60]!r}")
    return [p.strip() for p in split_top_commas(s[1:-1])]


def parse_ui_params(raw_lines: list[str]) -> tuple[list[dict], list[dict]]:
    """All DEFINE_UI_PARAMS occurrences (active or commented) + tooltips."""
    occurrences: list[dict] = []
    tooltips: list[dict] = []
    for lineno, raw in enumerate(raw_lines, 1):
        work = raw.strip()
        commented = work.startswith("//")
        if commented:
            work = work[2:].lstrip()
        idx = work.find("DEFINE_UI_PARAMS(")
        if idx != -1:
            args = split_top_commas(balanced_call(work, work.index("(", idx)))
            occurrences.append(parse_ui_param(args, lineno, commented))
            continue
        m = UI_TOOLTIP_RE.search(work)
        if m:
            tooltips.append(
                {"label": m.group(1).strip(), "text": m.group(2), "src": str(lineno)}
            )
    return occurrences, tooltips


def parse_ui_param(args: list[str], lineno: int, commented: bool) -> dict:
    if len(args) < 4:
        raise ParseError(f"line {lineno}: DEFINE_UI_PARAMS has {len(args)} args")
    ident, label, kind = args[0].strip(), args[1].strip(), args[2].strip()
    if kind not in KIND_MAP:
        raise ParseError(f"line {lineno}: unknown UI kind {kind!r}")
    entry: dict = {
        "id": ident,
        "label": label,
        "kind": KIND_MAP[kind],
        "dctlui": kind,
        "default": num(args[3]),
        "active": not commented,
        "src": str(lineno),
    }
    rest = args[4:]
    if kind == "DCTLUI_SLIDER_FLOAT":
        if len(rest) < 3:
            raise ParseError(f"line {lineno}: slider needs min/max/step")
        entry["min"], entry["max"], entry["step"] = (num(x) for x in rest[:3])
    elif kind == "DCTLUI_COMBO_BOX":
        if len(rest) < 2:
            raise ParseError(f"line {lineno}: combo needs two {{...}} lists")
        entry["option_ids"] = parse_brace_list(rest[0])
        entry["option_labels"] = parse_brace_list(rest[1])
    return entry


def merge_parameters(
    occurrences: list[dict], tooltip_by_label: dict[str, list[str]]
) -> dict[str, dict]:
    """Merge UI parameter occurrences, grouped by DCTL mode.

    The same parameter id can be defined differently for the two DCTL modes:
    the active (preset-mode) block and the commented-out StickShift block
    (which becomes active under #define INIT_PARAMS). Example: `_cwp` has 6
    options {D93..D50} default 2 in StickShift mode, but 7 options including
    'USE LOOK PRESET' default 0 in preset mode - the mode remap chain lives
    under #else, so both are correct for their mode.

    Output: per parameter, shared {id,label,kind,dctlui} plus a per-mode
    record with that mode's default/min/max/step and enum options.
    """
    buckets: dict[str, list[dict]] = {}
    for p in occurrences:
        buckets.setdefault(p["id"], []).append(p)

    merged: dict[str, dict] = {}
    for pid, occs in buckets.items():
        by_mode: dict[str, list[dict]] = {"active": [], "stickshift": []}
        for o in occs:
            by_mode["active" if o["active"] else "stickshift"].append(o)
        for mode, mode_occs in by_mode.items():
            if len(mode_occs) > 1:
                seen = {
                    (o["src"], o["default"]) for o in mode_occs
                }
                raise ParseError(
                    f"parameter {pid}: {len(mode_occs)} occurrences in same "
                    f"mode: {seen}"
                )
        labels = {o["label"] for o in occs}
        if len(labels) > 1:
            raise ParseError(f"parameter {pid}: labels differ across modes: {labels}")
        kinds = {o["kind"] for o in occs}
        if len(kinds) > 1:
            raise ParseError(f"parameter {pid}: kinds differ across modes: {kinds}")
        modes: dict[str, dict] = {}
        for mode, mode_occs in by_mode.items():
            if not mode_occs:
                continue
            o = mode_occs[0]
            mo = {"default": o["default"], "src": int(o["src"])}
            if o["kind"] == "slider_float":
                mo.update({"min": o["min"], "max": o["max"], "step": o["step"]})
            if o["kind"] == "enum":
                mo["option_ids"] = o["option_ids"]
                mo["option_labels"] = o["option_labels"]
            modes[mode] = mo
        entry: dict = {
            "id": pid,
            "label": occs[0]["label"],
            "kind": occs[0]["kind"],
            "dctlui": occs[0]["dctlui"],
            "modes": modes,
            "src": sorted({int(o["src"]) for o in occs}),
        }
        tooltip_texts = tooltip_by_label.get(entry["label"])
        if tooltip_texts:
            entry["tooltip"] = "\n".join(tooltip_texts)
        merged[pid] = entry
    return merged


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

TOKEN_RE = re.compile(
    r"""(?P<num>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?[fF]?)
      | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
      | (?P<op>\*=|\+=|-=|/=|<=|>=|==|!=|&&|\|\||[-+*/%<>=(),;:.?{}])
    """,
    re.VERBOSE,
)

DIRECTIVE_RE = re.compile(r"^#\s*(\w+)\s*(.*)$")


class Tok:
    __slots__ = ("kind", "val", "line")

    def __init__(self, kind: str, val: object, line: int):
        self.kind = kind  # num | ident | op | stage | dir
        self.val = val
        self.line = line

    def is_op(self, val: str) -> bool:
        return self.kind == "op" and self.val == val


def tokenize_region(
    code_lines: list[str], first_line: int, banners: list[dict]
) -> list[Tok]:
    """Tokenize stripped lines; stage markers injected on banner lines;
    directives become dir tokens (handled by the parser)."""
    banner_at: dict[int, str] = {}
    for b in banners:
        if b["banner"] and b["name"]:
            banner_at.setdefault(b["start"], b["name"])
    toks: list[Tok] = []
    for offset, line in enumerate(code_lines):
        lineno = first_line + offset
        if lineno in banner_at:
            toks.append(Tok("stage", banner_at[lineno], lineno))
        if not line.strip():
            continue
        m = DIRECTIVE_RE.match(line.strip())
        if m:
            toks.append(Tok("dir", (m.group(1), m.group(2).strip()), lineno))
            continue
        pos = 0
        for t in TOKEN_RE.finditer(line):
            gap = line[pos : t.start()]
            if gap.strip():
                raise ParseError(f"line {lineno}: unexpected {gap.strip()!r}")
            val = t.group(0)
            if t.lastgroup == "num":
                toks.append(Tok("num", num(val), lineno))
            elif t.lastgroup == "ident":
                toks.append(Tok("ident", val, lineno))
            else:
                toks.append(Tok("op", val, lineno))
            pos = t.end()
        if line[pos:].strip():
            raise ParseError(f"line {lineno}: unparsed tail {line[pos:].strip()!r}")
    return toks


# ---------------------------------------------------------------------------
# Statement + expression parser
# ---------------------------------------------------------------------------


class Parser:
    def __init__(self, toks: list[Tok], line_comments: dict[int, str]):
        self.toks = toks
        self.i = 0
        self.line_comments = line_comments

    # ---- cursor
    def peek(self, ahead: int = 0) -> Tok | None:
        j = self.i + ahead
        return self.toks[j] if j < len(self.toks) else None

    def advance(self) -> Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect_op(self, val: str) -> Tok:
        t = self.peek()
        if t is None or not t.is_op(val):
            got = repr(t.val) if t else "<eof>"
            raise ParseError(f"line {self.here()}: expected {val!r}, got {got}")
        return self.advance()

    def here(self) -> int:
        t = self.peek()
        return t.line if t else (self.toks[-1].line if self.toks else -1)

    def done(self) -> bool:
        return self.i >= len(self.toks)

    def last_line(self) -> int:
        return self.toks[self.i - 1].line if self.i > 0 else -1

    # ---- statement sequence
    def parse_statements(self) -> list[dict]:
        stmts: list[dict] = []
        while not self.done():
            stmts.extend(self.parse_one())
        return stmts

    def parse_block_until_close(self) -> list[dict]:
        stmts: list[dict] = []
        while True:
            t = self.peek()
            if t is None:
                raise ParseError(f"line {self.here()}: expected '}}'")
            if t.kind == "op" and t.val == "}":
                return stmts
            stmts.extend(self.parse_one())

    def parse_one(self) -> list[dict]:
        """One statement, including stage + preprocessor handling."""
        t = self.peek()
        if t is None:
            return []
        if t.kind == "stage":
            self.advance()
            return [{"k": "stage", "name": t.val, "src": str(t.line)}]
        if t.kind == "dir":
            return self.parse_directive()
        return [self._note(self.parse_stmt())]

    def _note(self, stmt: dict) -> dict:
        note = self.line_comments.get(self.last_line())
        if note and stmt["k"] != "if":
            stmt = dict(stmt)
            stmt["note"] = note
        return stmt

    # ---- preprocessor
    def parse_directive(self) -> list[dict]:
        t = self.advance()
        name, args = t.val  # type: ignore[misc]
        if name == "ifdef" and args.strip() == "INIT_PARAMS":
            # Parameter passthrough branch: dropped (that surface is
            # spec.parameters); the #else branch is the real pipeline body.
            stopped = self.skip_until(("else", "endif"))
            note = {
                "k": "skipped_preprocessor",
                "src": str(t.line),
                "what": "#ifdef INIT_PARAMS branch: parameter passthrough "
                "(each preset parameter = itself); covered verbatim by "
                "spec.parameters",
            }
            kept: list[dict] = []
            if stopped == "else":
                while True:
                    t2 = self.peek()
                    if t2 is None:
                        raise ParseError(f"line {t.line}: no #endif for ifdef")
                    if t2.kind == "dir" and t2.val[0] == "endif":
                        self.advance()
                        break
                    kept.extend(self.parse_one())
            else:
                t2 = self.peek()
                if t2 is not None and t2.kind == "dir" and t2.val[0] == "endif":
                    self.advance()
                else:
                    raise ParseError(f"line {t.line}: missing #endif")
            return [note, *kept]
        if name in ("ifdef", "ifndef", "if"):
            # pass-through guard: parse content, consume matching #endif
            inner: list[dict] = []
            while True:
                t2 = self.peek()
                if t2 is None:
                    raise ParseError(f"line {t.line}: unterminated {name}")
                if t2.kind == "dir" and t2.val[0] == "endif":
                    self.advance()
                    break
                if t2.kind == "dir" and t2.val[0] in ("else", "elif"):
                    raise ParseError(
                        f"line {t2.line}: #else/#elif in pass-through {name}"
                    )
                inner.extend(self.parse_one())
            what = f"pass-through {name} ({args.strip()})" if args.strip() else f"pass-through {name}"
            tag = "pass_through_ifdef" if name != "if" else "pass_through_if"
            return [{"k": tag, "src": str(t.line), "what": what, "kept": inner}]
        if name == "endif":
            raise ParseError(f"line {t.line}: unexpected #endif")
        if name == "else":
            raise ParseError(f"line {t.line}: unexpected #else")
        if name in ("define", "line", "undef"):
            return [
                {
                    "k": "skipped_preprocessor",
                    "src": str(t.line),
                    "what": f"#define/#line/#undef outside parser: {name} {args.strip()}",
                }
            ]
        raise ParseError(f"line {t.line}: unhandled directive #{name}")

    def skip_until(self, terminators: tuple[str, ...]) -> str:
        """Skip statements token-wise until a # directive in `terminators`."""
        while True:
            t = self.peek()
            if t is None:
                raise ParseError(f"line {self.here()}: unterminated skip region")
            if t.kind == "dir":
                name = t.val[0]
                if name in terminators:
                    self.advance()
                    return name
                raise ParseError(f"line {t.line}: directive #{name} inside skip region")
            if t.is_op(";"):
                self.advance()
                continue
            if t.is_op("{"):
                depth = 0
                while True:
                    t2 = self.advance()
                    if t2.is_op("{"):
                        depth += 1
                    elif t2.is_op("}"):
                        depth -= 1
                        if depth == 0:
                            break
                continue
            if t.is_op("}"):
                raise ParseError(f"line {t.line}: stray '}}' in skip region")
            self.advance()

    def expect_ident(self) -> Tok:
        t = self.peek()
        if t is None or t.kind != "ident":
            got = repr(t.val) if t else "<eof>"
            raise ParseError(f"line {self.here()}: expected identifier, got {got}")
        return self.advance()

    # ---- statements
    def parse_stmt(self) -> dict:
        t = self.peek()
        assert t is not None
        if t.kind == "ident" and t.val == "if":
            return self.parse_if()
        if t.kind == "ident" and t.val == "return":
            return self.parse_return()
        if t.kind == "ident" and (t.val in PRIMITIVE_TYPES or t.val == "const"):
            return self.parse_decl()
        return self.parse_assign()

    def parse_return(self) -> dict:
        t = self.advance()  # 'return'
        start = t.line
        if self.peek().is_op(";"):
            self.advance()
            return {"k": "return", "src": str(start), "expr": None}
        expr = self.parse_cond()
        end = self.expect_op(";").line
        return {"k": "return", "src": _src(start, end), "expr": expr}

    def parse_decl(self) -> dict:
        start = self.peek().line
        const = False
        if self.peek().is_op(";"):
            raise ParseError(f"line {start}: 'const' alone")
        names: list[str] = []
        inits: dict[str, object] = {}
        first = self.peek()
        if first.kind == "ident" and first.val == "const":
            const = True
            self.advance()

        type_name = self.expect_ident().val
        if type_name not in PRIMITIVE_TYPES:
            raise ParseError(f"line {self.here()}: unknown type {type_name!r}")
        while True:
            name = self.expect_ident().val
            names.append(name)
            if self.peek().is_op("="):
                self.advance()
                inits[name] = self.parse_cond()
            if self.peek().is_op(","):
                self.advance()
                continue
            break
        end = self.expect_op(";").line
        stmt: dict = {
            "k": "declare",
            "src": _src(start, end),
            "type": type_name,
            "names": names,
        }
        if inits:
            stmt["inits"] = inits
        if const:
            stmt["qualifier"] = "const"
        return stmt

    def parse_assign(self) -> dict:
        start = self.peek().line
        ops: list[dict] = []
        while True:
            tgt = self.parse_target_path()
            t = self.advance()
            if not (t.kind == "op" and t.val in ("=", "+=", "-=", "*=", "/=")):
                raise ParseError(f"line {t.line}: expected assignment op, got {t.val!r}")
            rhs = self.parse_cond()
            if t.val != "=":
                rhs = [ops_compound(t.val), Parser.target_expr(tgt), rhs]
            ops.append({"tgt": tgt, "expr": rhs})
            if self.peek().is_op(","):
                self.advance()
                continue
            break
        end = self.expect_op(";").line
        return {"k": "assign", "src": _src(start, end), "ops": ops}

    def parse_target_path(self) -> str:
        name = self.expect_ident().val
        while self.peek().is_op("."):
            self.advance()
            name += "." + self.expect_ident().val
        return name

    @staticmethod
    def target_expr(dotted: str) -> object:
        if "." not in dotted:
            return dotted
        parts = dotted.split(".")
        e: object = parts[0]
        for part in parts[1:]:
            e = ["attr", e, part]
        return e

    # ---- if
    def parse_if(self) -> dict:
        t = self.advance()  # 'if'
        start = t.line
        self.expect_op("(")
        cond = self.parse_cond()
        self.expect_op(")")
        cond_line_comment = self.line_comments.get(start)
        body = self.parse_stmt_seq()
        branch0: dict = {
            "cond": cond,
            "body": body,
            "src": _src(start, self.last_line()),
        }
        if cond_line_comment:
            branch0["note"] = cond_line_comment
        branches = [branch0]
        els: list[dict] | None = None
        pending_stages: list[str] = []
        while True:
            # stage banners between chain members attach to the next branch
            while self.peek() is not None and self.peek().kind == "stage":
                pending_stages.append(self.advance().val)  # type: ignore[union-attr]
            p = self.peek()
            if p is None:
                raise ParseError(f"line {start}: unterminated if-chain at EOF")
            if p.kind == "ident" and p.val == "else":
                self.advance()
                nt = self.peek()
                if nt is None:
                    raise ParseError(f"line {start}: dangling else")
                if nt.kind == "ident" and nt.val == "if":
                    inner = self.parse_if()  # flattens its own chain + els
                    branch = inner["branches"][0]
                    if pending_stages:
                        branch.setdefault("note", pending_stages[0])
                        if len(pending_stages) > 1:
                            branch["stages"] = pending_stages
                    branches.append(branch)
                    branches.extend(inner["branches"][1:])
                    els = inner.get("els")
                    pending_stages = []
                    break
                els = self.parse_stmt_seq()
                break
            break
        stmt: dict = {"k": "if", "src": _src(start, self.last_line()), "branches": branches}
        if els is not None:
            stmt["els"] = els
        if pending_stages:
            stmt["stages_after"] = pending_stages
        return stmt

    def parse_stmt_seq(self) -> list[dict]:
        """Body of an if/else: one braced block or one bare statement."""
        if self.peek().is_op("{"):
            self.advance()
            body = self.parse_block_until_close()
            self.expect_op("}")
            return body
        return [self._note(self.parse_stmt())]

    # ---- expressions (C precedence, ternary right-assoc)
    def parse_cond(self) -> object:
        c = self.parse_or()
        p = self.peek()
        if p is not None and p.is_op("?"):
            self.advance()
            a = self.parse_cond()
            self.expect_op(":")
            b = self.parse_cond()
            return ["cond", c, a, b]
        return c

    def _binop(self, sub, ops: dict) -> object:
        e = sub()
        while True:
            t = self.peek()
            if t is None or t.kind != "op" or t.val not in ops:
                return e
            self.advance()
            e = [ops[t.val], e, sub()]

    def parse_or(self) -> object:
        return self._binop(self.parse_and, {"||": "or"})

    def parse_and(self) -> object:
        return self._binop(self.parse_eq, {"&&": "and"})

    def parse_eq(self) -> object:
        return self._binop(self.parse_rel, {"==": "eq", "!=": "ne"})

    def parse_rel(self) -> object:
        return self._binop(self.parse_add, {"<": "lt", ">": "gt", "<=": "le", ">=": "ge"})

    def parse_add(self) -> object:
        return self._binop(self.parse_mul, {"+": "add", "-": "sub"})
    def parse_mul(self) -> object:
        return self._binop(self.parse_unary, {"*": "mul", "/": "div", "%": "mod"})

    def parse_unary(self) -> object:
        t = self.peek()
        if t is None:
            raise ParseError(f"line {self.here()}: EOF in expression")
        if t.is_op("-"):
            self.advance()
            p = self.peek()
            if p is not None and p.kind == "num":
                return -self.advance().val  # negated literal, e.g. -0.8f
            return ["neg", self.parse_unary()]
        if t.is_op("+"):
            self.advance()
            return self.parse_unary()
        if t.is_op("("):
            nxt, nxt2 = self.peek(1), self.peek(2)
            if (
                nxt is not None
                and nxt.kind == "ident"
                and nxt.val in PRIMITIVE_TYPES
                and nxt2 is not None
                and nxt2.is_op(")")
            ):
                self.advance()  # (
                self.advance()  # type name
                self.advance()  # )
                return [f"cast_{nxt.val}", self.parse_unary()]
            self.advance()
            e = self.parse_cond()
            self.expect_op(")")
            return e
        return self.parse_postfix()

    def parse_postfix(self) -> object:
        e = self.parse_primary()
        while self.peek() is not None and self.peek().is_op("."):
            self.advance()
            e = ["attr", e, self.expect_ident().val]
        return e

    def parse_primary(self) -> object:
        t = self.advance()
        if t.kind == "num":
            return t.val
        if t.kind == "ident":
            if self.peek() is not None and self.peek().is_op("("):
                self.advance()
                args: list[object] = []
                if not self.peek().is_op(")"):
                    while True:
                        args.append(self.parse_cond())
                        if not self.peek().is_op(","):
                            break
                        self.advance()
                self.expect_op(")")
                return [INTRINSICS.get(t.val, t.val)] + args
            return t.val
        raise ParseError(f"line {t.line}: unexpected token {t.val!r} in expression")


COMPOUND_BASE = {"+=": "add", "-=": "sub", "*=": "mul", "/=": "div"}


def ops_compound(op_tok: str) -> str:
    return COMPOUND_BASE[op_tok]


def _src(start: int, end: int) -> str:
    return str(start) if start == end else f"{start}-{end}"


# ---------------------------------------------------------------------------
# Region extraction
# ---------------------------------------------------------------------------

DEVICE_FN_RE = re.compile(
    r"^__DEVICE__\s+(\w+)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)"
)
CONSTANT_RE = re.compile(
    r"^__CONSTANT__\s+float\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\S+)\s*;\s*$"
)
TYPEDEF_RE = re.compile(r"^typedef\s+struct")

MATRIX_DEFINE_RE = re.compile(r"^#define\s+(matrix_[A-Za-z0-9_]+)\s+make_float3x3\((.*)\)\s*$")
VEC3_RE = re.compile(
    r"make_float3\(\s*([-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?[fF]?)\s*,"
    r"\s*([-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?[fF]?)\s*,"
    r"\s*([-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?[fF]?)\s*\)"
)


def parse_fn_header(header_line: str) -> dict:
    m = DEVICE_FN_RE.match(header_line)
    if not m:
        raise ParseError(f"unparseable function header: {header_line[:120]!r}")
    args = []
    for arg in split_top_commas(m.group(3)):
        parts = arg.strip().split()
        if not parts:
            continue  # zero-arg function, e.g. identity()
        if len(parts) != 2:
            raise ParseError(f"bad arg {arg!r} in header {header_line[:60]!r}")
        args.append({"type": parts[0], "name": parts[1]})
    return {"returns": m.group(1), "name": m.group(2), "args": args}


def match_brace(text: str, open_idx: int) -> int | None:
    """Index of the '}' matching text[open_idx]=='{', or None."""
    depth = 0
    for k in range(open_idx, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                return k
    return None


def collect_function_spans(code_lines: list[str]) -> list[dict]:
    """Spans of __DEVICE__ functions. Line numbers are 1-based inclusive.

    Handles bodies that live on the same line as the header
    (``__DEVICE__ float f(...) { return ...; }``) as well as the common
    multi-line form.
    """
    spans = []
    n = len(code_lines)
    i = 0
    while i < n:
        line = code_lines[i]
        if not DEVICE_FN_RE.match(line.strip()):
            i += 1
            continue
        header_line = i + 1
        open_idx = line.find("{")
        close_idx = match_brace(line, open_idx) if open_idx != -1 else None
        if close_idx is not None:
            # single-line function: body text between the braces
            spans.append(
                {
                    "header_line": header_line,
                    "open_line": header_line,
                    "end_line": header_line,
                    "tok_lines": [line[open_idx + 1 : close_idx]],
                    "tok_first_line": header_line,
                }
            )
            i += 1
            continue
        depth = 0
        started = False
        j = i
        while j < n:
            depth += code_lines[j].count("{") - code_lines[j].count("}")
            if depth > 0:
                started = True
            elif started:
                break
            j += 1
        if not started or j >= n:
            raise ParseError(f"line {header_line}: unterminated function body")
        open_at = next(k for k in range(i, j + 1) if "{" in code_lines[k])
        spans.append(
            {
                "header_line": header_line,
                "open_line": open_at + 1,
                "end_line": j + 1,
                "tok_lines": code_lines[open_at + 1 : j],
                "tok_first_line": open_at + 2,
            }
        )
        i = j + 1
    return spans


# ---------------------------------------------------------------------------
# Selection extraction
# ---------------------------------------------------------------------------


def collect_idents(e: object, out: set[str] | None = None) -> set[str]:
    out = set() if out is None else out
    if isinstance(e, str):
        if "." not in e and not e[:1].isdigit():
            out.add(e)
    elif isinstance(e, list):
        for x in e[1:]:
            collect_idents(x, out)
    return out


def cond_selection_param(cond: object) -> str | None:
    """Return the UI parameter id driving a selection if-chain, e.g. in_gamut."""
    if (
        isinstance(cond, list)
        and len(cond) == 3
        and cond[0] in ("eq", "ne")
        and isinstance(cond[1], str)
        and cond[1] in SELECTION_VARS
    ):
        return cond[1]
    for ident in collect_idents(cond):
        if ident in SELECTION_VARS:
            return ident
    return None




def static_expr(e: object) -> bool:
    """Numbers, bare identifiers, and zero-arg calls count as static values."""
    if isinstance(e, (int, float)):
        return True
    if isinstance(e, str):
        return True
    if isinstance(e, list):
        return len(e) == 1  # zero-arg call, e.g. ["identity"]
    return False


def flatten_selection_body(body: list[dict], src: str) -> dict[str, object]:
    values: dict[str, object] = {}
    if body and body[0].get("note"):
        src = f'{src} note="{body[0]["note"]}"'
    for s in body:
        if s["k"] != "assign":
            raise ParseError(f"selection body @{src}: expected assignments, got {s['k']}")
        for op in s["ops"]:
            if "." in op["tgt"]:
                raise ParseError(f"selection body @{src}: target must be plain: {op['tgt']!r}")
            if op["tgt"] in values:
                raise ParseError(f"selection body @{src}: {op['tgt']!r} assigned twice")
            values[op["tgt"]] = op["expr"]
    return values
    # note: dict values may be numbers, strings or IR lists ["fn"] etc.


def selection_group(stmt: dict) -> tuple[str | None, str | None, list[dict], list[dict]]:
    """Classify one top-level transform statement.

    Returns (group, driving_param_id, preset_entries, rule_stmts).
    Entries: eq-branches with int preset index and static values (the name ->
    value preset table). Rules: everything else (e.g. the creative-white
    `cwp_lm` passthrough), kept as faithful IR.
    """
    if stmt["k"] != "if":
        return None, None, [], []
    param_id = cond_selection_param(stmt["branches"][0]["cond"])
    if param_id is None:
        return None, None, [], []
    group = SELECTION_VARS[param_id]
    entries: list[dict] = []
    rules: list[dict] = []
    for b in stmt["branches"]:
        cond = b["cond"]
        is_eq = (
            isinstance(cond, list)
            and len(cond) == 3
            and cond[0] == "eq"
            and isinstance(cond[2], int)
        )
        values = flatten_selection_body(b["body"], _src_for_branch(stmt, b))
        if is_eq and all(static_expr(v) for v in values.values()):
            entries.append(
                {
                    "index": cond[2],
                    "name": b.get("note"),
                    "src": _src_for_branch(stmt, b),
                    "values": values,
                }
            )
        else:
            rules.append({"cond": cond, "body": b["body"], "src": _src_for_branch(stmt, b)})
    return group, param_id, entries, rules


def _src_for_branch(stmt: dict, b: dict) -> str:
    return b.get("src") or stmt["src"]


def resolve_statements(
    stmts: list[dict],
    scope: set[str],
    fn_sigs: dict[str, dict],
    known_ids: set[str],
    errors: list[str],
    where: str,
) -> None:
    for s in stmts:
        src = s.get("src") or "?"
        k = s["k"]
        if k == "declare":
            for e in s.get("inits", {}).values():
                check_expr(e, scope, fn_sigs, known_ids, errors, where, src)
            scope.update(s["names"])
        elif k == "assign":
            for op in s["ops"]:
                base = op["tgt"].split(".")[0]
                if base not in scope:
                    errors.append(f"{where}@{src}: assignment to undeclared {op['tgt']!r}")
                check_expr(op["expr"], scope, fn_sigs, known_ids, errors, where, src)
        elif k == "return":
            if s.get("expr") is not None:
                check_expr(s["expr"], scope, fn_sigs, known_ids, errors, where, src)
        elif k == "if":
            for b in s["branches"]:
                check_expr(b["cond"], scope, fn_sigs, known_ids, errors, where, src)
                resolve_statements(b["body"], set(scope), fn_sigs, known_ids, errors, where)
            if s.get("els") is not None:
                resolve_statements(s["els"], set(scope), fn_sigs, known_ids, errors, where)
        elif k in ("pass_through_ifdef", "pass_through_if"):
            kept = s.get("kept") or []
            resolve_statements(kept, set(scope), fn_sigs, known_ids, errors, where)
        elif k in ("stage", "select", "skipped_preprocessor"):
            continue
        else:
            raise ParseError(f"{where}@{src}: unknown statement kind {k!r}")


def check_expr(
    e: object,
    scope: set[str],
    fn_sigs: dict[str, dict],
    known_ids: set[str],
    errors: list[str],
    where: str,
    src: str,
) -> None:
    if isinstance(e, (int, float)):
        return
    if isinstance(e, str):
        base = e.split(".")[0]
        if base not in known_ids and base not in scope:
            errors.append(f"{where}@{src}: unresolved symbol {e!r}")
        return
    if not isinstance(e, list):
        raise ParseError(f"{where}@{src}: bad expr type {type(e)}")
    op = e[0]
    args = e[1:]
    if op == "attr":
        if len(args) != 2:
            errors.append(f"{where}@{src}: attr takes base + component")
        check_expr(args[0], scope, fn_sigs, known_ids, errors, where, src)
        return
    if op.startswith("cast_"):
        if len(args) != 1:
            errors.append(f"{where}@{src}: {op} takes 1 arg")
        check_expr(args[0], scope, fn_sigs, known_ids, errors, where, src)
        return
    if op in CONSTRUCTORS:
        want = CONSTRUCTORS[op]
        if len(args) != want:
            errors.append(f"{where}@{src}: {op} arity {len(args)} != {want}")
        for x in args:
            check_expr(x, scope, fn_sigs, known_ids, errors, where, src)
        return
    if op in UNARY_OPS:
        if len(args) != 1:
            errors.append(f"{where}@{src}: {op} takes 1 arg")
        check_expr(args[0], scope, fn_sigs, known_ids, errors, where, src)
        return
    if op in BINARY_OPS:
        if len(args) != 2:
            errors.append(f"{where}@{src}: {op} takes 2 args, got {len(args)}")
        for x in args:
            check_expr(x, scope, fn_sigs, known_ids, errors, where, src)
        return
    if op == "cond":
        if len(args) != 3:
            errors.append(f"{where}@{src}: cond takes 3 args")
        for x in args:
            check_expr(x, scope, fn_sigs, known_ids, errors, where, src)
        return
    if op not in fn_sigs:
        errors.append(f"{where}@{src}: unknown call target {op!r}")
        return
    sig = fn_sigs[op]
    if len(args) != len(sig["args"]):
        errors.append(f"{where}@{src}: call {op}() arity {len(args)} != {len(sig['args'])}")
    for x in args:
        check_expr(x, scope, fn_sigs, known_ids, errors, where, src)


def collect_refs(e: object, out: set[str]) -> None:
    """Base identifiers referenced anywhere in expression e."""
    if isinstance(e, (int, float)):
        return
    if isinstance(e, str):
        out.add(e.split(".")[0])
        return
    if isinstance(e, list):
        if e and isinstance(e[0], str):
            out.add(e[0])  # the call op: user function names matter here
        for x in e[1:]:
            collect_refs(x, out)


def refs_in_stmts(stmts: list[dict], out: set[str]) -> None:
    for s in stmts:
        if s["k"] == "declare":
            for e in s.get("inits", {}).values():
                collect_refs(e, out)
        elif s["k"] == "assign":
            for op in s["ops"]:
                out.add(op["tgt"].split(".")[0])
                collect_refs(op["expr"], out)
        elif s["k"] == "return":
            if s.get("expr") is not None:
                collect_refs(s["expr"], out)
        elif s["k"] == "if":
            for b in s["branches"]:
                collect_refs(b["cond"], out)
                refs_in_stmts(b["body"], out)
            if s.get("els") is not None:
                refs_in_stmts(s["els"], out)


def params_used_in(selection: dict, functions: dict, pipeline: list[dict],
                   parameter_ids: set[str]) -> set[str]:
    used: set[str] = set()

    def visit_stmts(stmts: list[dict]) -> None:
        for s in stmts:
            if s["k"] == "declare":
                walk(s.get("inits", {}).values())
            elif s["k"] == "assign":
                for op in s["ops"]:
                    walk([op["expr"]])
            elif s["k"] == "return":
                if s.get("expr") is not None:
                    walk([s["expr"]])
            elif s["k"] == "if":
                for b in s["branches"]:
                    walk([b["cond"]])
                    visit_stmts(b["body"])
                if s.get("els") is not None:
                    visit_stmts(s["els"])

    def walk(exprs) -> None:
        for e in exprs:
            refs: set[str] = set()
            collect_refs(e, refs)
            used.update(refs & parameter_ids)
            for r in refs:
                if r in functions:
                    visit_fn(r)

    visited_fn: set[str] = set()

    def visit_fn(f: str) -> None:
        if f in visited_fn or f not in functions:
            return
        visited_fn.add(f)
        visit_stmts(functions[f]["body"])

    visit_stmts(pipeline)
    for g in selection.values():
        for e in g["entries"]:
            walk(e["values"].values())
        for r in g["rules"]:
            walk([r["cond"]])
            visit_stmts(r["body"])
    return used


# ---------------------------------------------------------------------------
# Line classification (coverage report)
# ---------------------------------------------------------------------------


def classify_lines(
    raw_lines: list[str], blocks: list[dict], fn_header_lines: dict[int, dict]
) -> list[str]:
    kinds: list[str] = []
    block_at = _line_block_map(blocks)
    for lineno, raw in enumerate(raw_lines, 1):
        stripped = raw.strip()
        if not stripped:
            kinds.append("blank")
            continue
        blk = block_at.get(lineno)
        if blk is not None:
            kinds.append("stage_banner" if blk["banner"] else "prose_comment")
            continue
        work = stripped
        if work.startswith("//"):
            work = work[2:].lstrip()
            if "DEFINE_UI_PARAMS" in work:
                kinds.append("ui_param_commented")
            elif work.startswith("#define"):
                kinds.append("define_commented")
            else:
                kinds.append("line_comment")
            continue
        if work.startswith("#"):
            kinds.append("directive")
            continue
        if "DEFINE_UI_PARAMS" in work:
            kinds.append("ui_param")
            continue
        if "DEFINE_UI_TOOLTIP" in work:
            kinds.append("tooltip")
            continue
        if lineno in fn_header_lines:
            kinds.append("function_header")
            continue
        if work.startswith("typedef"):
            kinds.append("typedef")
            continue
        if work.startswith("__CONSTANT__"):
            kinds.append("constant")
            continue
        code = code_of(raw)
        if code.strip():
            kinds.append("code")
        else:
            kinds.append("UNCLASSIFIED")
    return kinds


def code_of(raw: str) -> str:
    code, _ = strip_line_comment(raw)
    return code


def _line_block_map(blocks: list[dict]) -> dict[int, dict]:
    m: dict[int, dict] = {}
    for b in blocks:
        for ln in range(b["start"], b["end"] + 1):
            m.setdefault(ln, b)
    return m


# ---------------------------------------------------------------------------
# Main conversion
# ---------------------------------------------------------------------------


def convert(dctl_path: Path, tag: str | None, commit: str | None) -> dict:
    raw_text = dctl_path.read_text()
    raw_lines = raw_text.splitlines()
    if raw_lines and raw_lines[0].startswith("#line"):
        raw_lines[0] = ""  # Resolve logging directive, not code

    code_lines, blocks = block_comment_pass(raw_lines)

    line_comments: dict[int, str] = {}
    for lineno, rawline in enumerate(raw_lines, 1):
        _, inline = strip_line_comment(rawline)
        if inline:
            line_comments.setdefault(lineno, inline.strip())

    occurrences, tooltips = parse_ui_params(raw_lines)
    tooltip_by_label: dict[str, list[str]] = {}
    for t in tooltips:
        tooltip_by_label.setdefault(t["label"], []).append(t["text"])
    parameters = merge_parameters(occurrences, tooltip_by_label)

    # ---- constants (matrix defines, other defines, __CONSTANT__ globals)
    constants: dict[str, dict] = {}
    other_defines: list[dict] = []
    for lineno, line in enumerate(code_lines, 1):
        s = line.strip()
        if not s.startswith("#define"):
            continue
        m = MATRIX_DEFINE_RE.match(s)
        if m:
            rows = VEC3_RE.findall(m.group(2))
            if len(rows) != 3:
                raise ParseError(f"line {lineno}: expected 3 matrix rows in {m.group(1)}")
            constants[m.group(1)] = {
                "kind": "m3x3",
                "rows": [[num(v) for v in vec] for vec in rows],
                "literals": [list(vec) for vec in rows],
                "src": str(lineno),
            }
            continue
        dm = re.match(r"^#define\s+([A-Za-z_][A-Za-z0-9_]*)\s*(.*)$", s)
        if not dm:
            raise ParseError(f"line {lineno}: bad define {s!r}")
        other_defines.append(
            {"name": dm.group(1), "body": dm.group(2).strip(), "src": str(lineno)}
        )
    for lineno, line in enumerate(code_lines, 1):
        m = CONSTANT_RE.match(line.strip())
        if m:
            constants[m.group(1)] = {
                "kind": "scalar",
                "value": num(m.group(2)),
                "literals": [m.group(2)],
                "src": str(lineno),
            }

    # ---- functions
    spans = collect_function_spans(code_lines)
    headers_at = {s["header_line"]: s for s in spans}
    functions: dict[str, dict] = {}
    for span in spans:
        header = code_lines[span["header_line"] - 1].strip()
        meta = parse_fn_header(header)
        in_span = [
            b for b in blocks
            if span["open_line"] <= b["start"] and b["end"] <= span["end_line"]
        ]
        toks = tokenize_region(span["tok_lines"], span["tok_first_line"], in_span)
        p = Parser(toks, line_comments)
        body = p.parse_statements()
        if not p.done():
            raise ParseError(f"function {meta['name']}: leftover tokens {p.peek()!r}")
        functions[meta["name"]] = {
            "returns": meta["returns"],
            "args": meta["args"],
            "src": f"{span['header_line']}-{span['end_line']}",
            "body": body,
        }
    if "transform" not in functions:
        raise ParseError("no transform() function found")
    if len(functions["transform"]["args"]) != 7:
        raise ParseError("transform() must have the 7 pixel-argument signature")
    entry_point = {
        "returns": "float3",
        "args": functions["transform"]["args"],
        "src": functions["transform"]["src"],
        "note": "per-pixel entry point; body = spec.pipeline (preset selection "
        "lifted to spec.selection, marked with k='select' entries)",
    }
    pipeline = functions.pop("transform")["body"]

    # ---- lift selection chains out of the pipeline
    selection: dict[str, dict] = {}
    group_order: list[str] = []
    new_pipeline: list[dict] = []
    for s in pipeline:
        group, param_id, entries, rules = selection_group(s)
        if group is None:
            new_pipeline.append(s)
            continue
        if group not in selection:
            selection[group] = {
                "parameter": param_id,
                "entries": [],
                "rules": [],
            }
            group_order.append(group)
        g = selection[group]
        g["entries"].extend(entries)
        g["rules"].extend(rules)
        new_pipeline.append({"k": "select", "src": s["src"], "group": group})
    pipeline = new_pipeline

    # fill preset entry names from UI combo labels where branch notes absent
    for gid, g in selection.items():
        labels = None
        max_index = max((e["index"] for e in g["entries"]), default=-1)
        for o in occurrences:
            if (
                o["id"] == g["parameter"]
                and o.get("option_labels")
                and len(o["option_labels"]) >= max_index + 1
            ):
                labels = o["option_labels"]
        for e in g["entries"]:
            if e["name"] is None and labels is not None:
                idx = e["index"]
                if isinstance(idx, int) and 0 <= idx < len(labels):
                    e["name"] = labels[idx]
        g["src"] = (
            f"{min(int(e['src'].split('-')[0]) for e in g['entries'] + g['rules'])}"
            f"-{max(int(e['src'].split('-')[-1]) for e in g['entries'] + g['rules'])}"
        ) if (g["entries"] or g["rules"]) else None
    selection = {k: selection[k] for k in group_order}

    # ---- verification
    transform_sig = {
        "args": entry_point["args"],
        "returns": "float3",
    }
    fn_sigs = {k: v for k, v in functions.items()}
    known_ids = set(parameters) | set(constants) | {d["name"] for d in other_defines}
    errors: list[str] = []
    for fname, fn in functions.items():
        scope = {a["name"] for a in fn["args"]}
        resolve_statements(fn["body"], scope, fn_sigs, known_ids, errors, fname)
    resolve_statements(
        pipeline, {a["name"] for a in transform_sig["args"]},
        fn_sigs, known_ids, errors, "transform",
    )
    if errors:
        for e in errors[:40]:
            print(f"VERIFY-ERROR: {e}", file=sys.stderr)
        raise SystemExit(f"verification failed: {len(errors)} errors")

    # ---- reports
    referenced: set[str] = set()
    for fn in functions.values():
        refs_in_stmts(fn["body"], referenced)
    refs_in_stmts(pipeline, referenced)
    for g in selection.values():
        for e in g["entries"]:
            for v in e["values"].values():
                collect_refs(v, referenced)
        for r in g["rules"]:
            collect_refs(r["cond"], referenced)
            refs_in_stmts(r["body"], referenced)
    dead_fns = sorted(set(functions) - referenced)
    used_params = params_used_in(selection, functions, pipeline, set(parameters))
    driving_ids = {g["parameter"] for g in selection.values()}
    never_used = set(parameters) - used_params - driving_ids
    stickshift_only = {pid for pid, p in parameters.items() if "active" not in p["modes"]}
    unused_params = sorted(never_used - stickshift_only)

    line_kinds = classify_lines(raw_lines, blocks, headers_at)
    totals: dict[str, int] = {}
    for k in line_kinds:
        totals[k] = totals.get(k, 0) + 1
    unclassified = [str(i + 1) for i, k in enumerate(line_kinds) if k == "UNCLASSIFIED"]
    if unclassified:
        raise SystemExit(f"unclassified lines: {unclassified[:20]}")

    spec = {
        "schema_version": SCHEMA_VERSION,
        "provenance": {
            "source_repo": "https://github.com/jedypod/open-display-transform",
            "tag": tag,
            "commit": commit,
            "file": dctl_path.name,
            "sha256": hashlib.sha256(dctl_path.read_bytes()).hexdigest(),
            "file_header": "\n".join(raw_lines[:9]).strip(),
        },
        "notes": [
            "source line 1 (#line 2) is a Resolve logging directive; ignored",
            "#ifdef INIT_PARAMS branch inside transform() is the parameter "
            "passthrough (each parameter = itself): covered verbatim by "
            "spec.parameters; the pipeline keeps the #else (preset-driven) "
            "branch",
            "selection chains lifted to spec.selection; pipeline marks where "
            "each chain applies with k='select' entries in original order",
            "tonescale_preset chain: the first two branches are bare `if` "
            "statements, not else-if: later statements override earlier ones "
            "for shared tn_* variables. Preset application order is listed "
            "order within each group",
            "look_preset active combo exposes 7 looks; the StickShift combo "
            "(commented block) adds 'Base' as index 7, and the preset branch "
            "for it exists in the code",
        ],
        "ir": {"statement_kinds": STATEMENT_KINDS, "expression_ops": OP_DOC},
        "parameters": parameters,
        "selection": selection,
        "constants": constants,
        "entry_point": entry_point,
        "functions": functions,
        "pipeline": pipeline,
        "reports": {
            "counts": {
                "param_occurrences": len(occurrences),
                "param_ids": len(parameters),
                "active_params": sum(1 for p in parameters.values() if "active" in p["modes"]),
                "stickshift_params": sum(1 for p in parameters.values() if "stickshift" in p["modes"]),
                "tooltips": len(tooltips),
                "matrix_constants": sum(1 for c in constants.values() if c["kind"] == "m3x3"),
                "scalar_constants": sum(1 for c in constants.values() if c["kind"] == "scalar"),
                "other_defines": len(other_defines),
                "functions": len(functions),
                "select_groups": len(selection),
                "select_entries": {k: len(v["entries"]) for k, v in selection.items()},
                "pipeline_statements": len(pipeline),
            },
            "lines": totals,
            "dead_functions": dead_fns,
            "stickshift_params_dropped_with_init_params": len(
                stickshift_only & never_used
            ),
            "params_not_used_in_pipeline": unused_params,
            "errors": 0,
        },
    }
    return spec


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Convert an OpenDRT DCTL into a semantic spec JSON."
    )
    ap.add_argument("dctl", nargs="?", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--tag", default=None)
    ap.add_argument("--commit", default=None)
    ap.add_argument(
        "--print",
        default=None,
        metavar="PATH",
        help="print a spec subtree instead of the summary, e.g. "
        "selection.look or pipeline.11 (path parts walk dicts, ints index lists)",
    )
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    dctl = args.dctl or (root / "reference" / "OpenDRT_v1.1.0.dctl")
    out = args.out or (root / "spec" / f"opendrt-{(args.tag or 'v1.1.0')}.json")
    spec = convert(dctl, args.tag, args.commit)

    if args.print:
        # read-only inspection: do not touch the spec file (provenance args
        # may not have been passed; regeneration is the pixi task's job)
        node: object = spec
        for part in args.print.split("."):
            node = node[int(part)] if isinstance(node, list) else node[part]
        print(json.dumps(node, indent=2, ensure_ascii=False))
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(spec, indent=2, ensure_ascii=False) + "\n"
    out.write_text(text)

    r = spec["reports"]
    print(f"wrote {out} ({len(text) / 1024:.1f} KB)")
    print(json.dumps(r["counts"], indent=2))
    print("line coverage:", json.dumps(r["lines"]))
    print("dead functions:", r["dead_functions"] or "none")
    print()
    print("spot checks:")
    print(
        json.dumps(
            {
                "param tn_con": spec["parameters"]["_tn_con"],
                "param look_preset": spec["parameters"]["look_preset"],
                "look entry Standard": spec["selection"]["look"]["entries"][0],
                "display_encoding entries": [
                    {"name": e["name"],
                     "tn_su": e["values"].get("tn_su"),
                     "display_gamut": e["values"].get("display_gamut"),
                     "eotf": e["values"].get("eotf")}
                    for e in spec["selection"]["display_encoding"]["entries"]
                ],
                "input_gamut mapping": [
                    {"name": e["name"], "value": next(iter(e["values"].values()))}
                    for e in spec["selection"]["input_gamut"]["entries"]
                ],
                "matrix ap0 row 1": spec["constants"]["matrix_ap0_to_xyz"]["rows"][0],
                "pipeline head": spec["pipeline"][:4],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ParseError as e:
        print(f"parse error: {e}", file=sys.stderr)
        sys.exit(1)