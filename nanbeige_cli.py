#!/usr/bin/env python3
"""
nanbeige_cli.py — Claude Code / Gemini CLI style terminal interface for a
local llama.cpp server running heretic-org/Nanbeige4.1-3B-heretic.

Design goals:
- Boxed, branded chrome (header panel, status bar) like Claude Code / Gemini CLI
- Live streaming render with a "thinking" spinner before first token
- Token/sec + context-usage readout in the status bar
- Slash commands with in-CLI help
- Persistent sessions, resumable history
"""
import argparse
import json
import math
import os
import re
import subprocess
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
import sys
import time
from collections import Counter
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.markdown import Markdown
from rich.text import Text
from rich.spinner import Spinner
from rich.rule import Rule
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style as PTStyle

import os as _os
# Many Termux/Android terminals support 24-bit color but don't advertise it,
# causing libraries to silently downgrade to an approximated 256-color
# palette (the likely cause of colors looking "off" vs. the intended hex).
# Force truecolor so #D97757 etc. render as the exact specified value.
_os.environ.setdefault("COLORTERM", "truecolor")
console = Console(color_system="truecolor")

BASE_DIR = os.path.expanduser("~/ncoder-cli")
HISTORY_FILE = os.path.join(BASE_DIR, ".cli_history")
SESSION_DIR = os.path.join(BASE_DIR, "sessions")
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")
os.makedirs(WORKSPACE_DIR, exist_ok=True)

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful, direct assistant running locally on-device. "
    "Keep responses concise unless asked for detail.\n\n"
    "You have several tools available on every turn, and you decide for "
    "yourself, without being asked, whenever a question calls for one:\n"
    "- web_search: general queries about current or uncertain information\n"
    "- http_request: hit a specific URL or API you already know, with your "
    "own choice of method/headers/body\n"
    "- read_file / write_file / list_directory: work with files in your "
    "sandboxed workspace directory\n"
    "- run_python: execute Python to verify math, parse data, or test a "
    "snippet before answering — if it errors, read the traceback and try "
    "again rather than guessing\n"
    "- search_notes: keyword-ranked search over files in your workspace, "
    "for finding things you or the user saved earlier\n"
    "- termux_api: read/write the phone clipboard or send a notification\n"
    "Don't guess or fabricate details a tool call could confirm or compute.\n\n"
    "For any task with more than 2-3 steps: keep a checklist in a file "
    "called todo.md in your workspace (write_file/read_file), with one "
    "line per step and [ ]/[x] to mark progress. Update it as you "
    "complete each step rather than only tracking progress in your own "
    "reasoning — this survives even if the conversation is interrupted.\n\n"
    "If you discover a fact or constraint that must not be lost or "
    "forgotten later (a number you computed, a format the user asked "
    "for, a hard requirement) — write it to invariants.md. That file is "
    "always shown back to you every round and is never summarized away, "
    "unlike the rest of the conversation.\n\n"
    "For deeply nested work (e.g. research A, then use A's result to do "
    "B), use push_subtask to start a focused subtask with its own clean "
    "context, then pop_subtask with a short result summary when it's "
    "done — this keeps a subtask's intermediate mess out of the main "
    "task's context. If you have multiple independent subtasks and one "
    "needs another's result first, give it depends_on (comma-separated "
    "subtask names) — it will be queued until that dependency finishes, "
    "and you'll be told when it's unblocked. When you pop_subtask, always "
    "give a confidence level, and verify checkable claims (code that "
    "should pass tests, facts you can cross-reference) before finishing "
    "— you'll be asked to verify if you skip this on a checkable subtask. "
    "Prefer structured_result (JSON) over prose alone when a later step "
    "needs precise data, not just a summary.\n\n"
    "When something is ambiguous but a reasonable default exists, state "
    "the assumption you're making and continue — don't stop to ask. Only "
    "call request_user_input when you genuinely cannot proceed without "
    "the user: a missing credential, a choice only they can make, or a "
    "destructive/irreversible action. In autonomous (/auto) mode, keep "
    "working through your todo.md checklist across turns without "
    "stopping to check in, unless request_user_input applies.\n\n"
    "Use record_fact for individual discrete facts you discover that a "
    "later subtask might need to look up by keyword — check query_facts "
    "before assuming something isn't known yet or re-deriving it."
)
MODEL_LABEL = "Nanbeige4.1-3B-heretic (local, Q4_K_M)"
# New improvement 4: Nanbeige4.1-3B's own model card recommends
# temperature=0.6, top_p=0.95, repeat_penalty=1.0 — top_p/repeat_penalty
# adopted here explicitly rather than left at llama.cpp's generic
# defaults. Temperature itself stays a per-call argument (0.0 for
# deterministic gate/check calls, 0.7 default for generation) rather
# than hardcoded, since several call sites deliberately need temp=0.
SAMPLING_TOP_P = 0.95
SAMPLING_REPEAT_PENALTY = 1.0
APP_NAME = "NCoder"
VERSION = "1.8.0"

os.makedirs(SESSION_DIR, exist_ok=True)

PT_STYLE = PTStyle.from_dict({
    "prompt": "bold #D97757",
})


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", default="8080")
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--temperature", type=float, default=0.6)  # Nanbeige4.1-3B's own recommended default
    p.add_argument("--ctx-size", type=int, default=6144)
    p.add_argument("--selftest", action="store_true",
                    help="Run an end-to-end health check of the server, chat, and all tools, then exit.")
    return p.parse_args()


# ── Server plumbing ──────────────────────────────────────────────────────

def wait_for_server(base_url, timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{base_url}/health", timeout=2)
            if r.status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(1)
    return False


def get_server_props(base_url):
    try:
        r = requests.get(f"{base_url}/props", timeout=3)
        if r.status_code == 200:
            return r.json()
    except requests.exceptions.RequestException:
        pass
    return {}


def stream_chat(base_url, messages, max_tokens, temperature):
    payload = {
        "messages": messages,
        "stream": True,
        "max_tokens": max_tokens,
        "temperature": temperature,
        # New improvement 4: Nanbeige4.1-3B's own model card recommends
        # top_p=0.95 and repeat_penalty=1.0 specifically (alongside
        # temperature=0.6) — adopted here rather than leaving sampling
        # at llama.cpp's generic defaults, which may differ.
        "top_p": SAMPLING_TOP_P,
        "repeat_penalty": SAMPLING_REPEAT_PENALTY,
        # Suggestion 5: llama.cpp server reuses cached KV state for a
        # matching prompt prefix when this is set — since every plan/
        # decision/critique/answer call in a round shares the same
        # system prompt + few-shot prefix, this avoids reprocessing that
        # fixed prefix from scratch on every one of those calls.
        "cache_prompt": True,
    }
    t_start = time.time()
    t_first_token = None
    n_tokens = 0
    try:
        with requests.post(
            f"{base_url}/v1/chat/completions",
            json=payload,
            stream=True,
            timeout=(5, 300),
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                data = line[len("data: "):]
                if data.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content")
                if content:
                    if t_first_token is None:
                        t_first_token = time.time()
                    n_tokens += 1
                    yield content, None
    except requests.exceptions.RequestException as e:
        yield f"\n[connection error: {e}]\n", None
        return

    elapsed = time.time() - (t_first_token or t_start)
    ttft = (t_first_token - t_start) if t_first_token else 0.0
    tok_per_sec = (n_tokens / elapsed) if elapsed > 0 else 0.0
    yield None, {"ttft": ttft, "tok_s": tok_per_sec, "tokens": n_tokens}


# ── Agentic tools: the model decides on its own when to use these ────────
# No user toggle — tool schemas are always passed to the model, and it's
# the model's job to decide whether a given turn needs them, same as
# Claude Code/Gemini CLI expose tools unconditionally and let the model
# choose when to invoke them.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the public web for current information. Use this "
                "whenever the answer might depend on recent events, facts "
                "you're unsure of, or anything that could have changed "
                "since training — you decide when this is needed, without "
                "waiting to be asked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": (
                "Make a custom HTTP request to a specific URL — e.g. to "
                "call a public API, fetch a specific web page, or check a "
                "documented endpoint. Use this instead of web_search when "
                "you already know the exact URL or API to hit, or when you "
                "need to construct a request with specific parameters, "
                "headers, or a JSON body. Response bodies are truncated if "
                "very large."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                        "description": "HTTP method.",
                    },
                    "url": {"type": "string", "description": "Full URL, must be http(s)."},
                    "headers": {
                        "type": "object",
                        "description": "Optional request headers as key/value pairs.",
                    },
                    "body": {
                        "type": "string",
                        "description": (
                            "Optional request body. If it looks like JSON, "
                            "it's sent with Content-Type: application/json."
                        ),
                    },
                },
                "required": ["method", "url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a text file from your sandboxed workspace directory. "
                "Paths are relative to the workspace — you cannot read "
                "files anywhere else on the device."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path within the workspace."}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write (or overwrite) a text file in your sandboxed "
                "workspace directory. Use this to save generated content, "
                "notes, or code for later."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path within the workspace."},
                    "content": {"type": "string", "description": "Full text content to write."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and folders in your sandboxed workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative subdirectory to list (optional, defaults to workspace root).",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Execute a Python snippet in a sandboxed subprocess and get "
                "back stdout/stderr. Use this to verify calculations, parse "
                "or transform data, or test code before presenting it as an "
                "answer. If it errors, read the traceback and correct your "
                "code rather than guessing at the fix."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python source to execute."}
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_notes",
            "description": (
                "Keyword-ranked search over text files saved in your "
                "workspace (not semantic/embedding search — plain ranked "
                "term matching). Use this to find something saved earlier "
                "before assuming it doesn't exist."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Terms to search for."}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "termux_api",
            "description": (
                "Interact with the phone via Termux:API. Actions: "
                "'clipboard_get' (read clipboard), 'clipboard_set' (write "
                "clipboard, needs 'text'), 'notify' (show a notification, "
                "needs 'text' and optional 'title'). Requires the Termux:API "
                "app to be installed on the device; fails gracefully if not."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["clipboard_get", "clipboard_set", "notify"],
                    },
                    "text": {"type": "string", "description": "Text payload for clipboard_set or notify."},
                    "title": {"type": "string", "description": "Optional title for notify."},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "push_subtask",
            "description": (
                "Start a focused subtask with its own narrowed context — "
                "use for nested work (e.g. research a fact, then use that "
                "result elsewhere) so the subtask's intermediate detail "
                "doesn't clutter the main task's context. If this subtask "
                "needs the result of another subtask you've already "
                "declared but hasn't finished yet, list it in depends_on "
                "and it will be queued instead of started immediately — "
                "you'll be told when it's unblocked. Call pop_subtask "
                "with a result_summary when a subtask is done to return "
                "to the parent task."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short subtask name."},
                    "goal": {"type": "string", "description": "What this subtask needs to accomplish."},
                    "depends_on": {
                        "type": "string",
                        "description": (
                            "Comma-separated names of other subtasks this one needs "
                            "results from first. Leave empty if there are none."
                        ),
                    },
                },
                "required": ["name", "goal"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pop_subtask",
            "description": (
                "Finish the current subtask and return to the parent task, "
                "folding in a short result summary. Only call this if you "
                "previously called push_subtask. If the subtask involved "
                "something checkable (code that should pass tests, a fact "
                "you can cross-reference), verify it first — describe how "
                "in verified_by. If you can't verify a checkable claim, "
                "you'll be asked to try before the subtask is allowed to "
                "finish."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "result_summary": {
                        "type": "string",
                        "description": "Concise summary of what the subtask accomplished, handed back to the parent task.",
                    },
                    "structured_result": {
                        "type": "string",
                        "description": (
                            "Optional: the result as a JSON string (e.g. a list of "
                            "facts, a table, a set of file edits) instead of only "
                            "prose, when precision matters for whoever consumes "
                            "this next. Leave empty if a prose summary is enough."
                        ),
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": "How confident you are in this result.",
                    },
                    "verified_by": {
                        "type": "string",
                        "description": (
                            "How you verified this result (e.g. 'ran pytest, "
                            "3 passed', 'cross-checked via web_search'). Leave "
                            "empty only if there was genuinely nothing to verify."
                        ),
                    },
                },
                "required": ["result_summary", "confidence"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_user_input",
            "description": (
                "Signal that you genuinely cannot proceed without the "
                "user — a missing credential, a choice only they can "
                "make, or a destructive/irreversible action needing "
                "confirmation. Do NOT call this for ordinary ambiguity "
                "you could resolve with a stated, reasonable assumption; "
                "state the assumption and continue instead. Only use this "
                "when continuing without the user would mean guessing at "
                "something that actually matters."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Specifically what you need from the user and why you can't proceed without it.",
                    }
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_fact",
            "description": (
                "Record a discrete fact in the structured fact ledger — "
                "distinct from invariants.md (free-text constraints): use "
                "this for individual facts you discover that a later "
                "subtask or round might need to look up by keyword, not "
                "just have re-shown to you verbatim every round."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string", "description": "The fact itself, stated plainly."},
                    "source": {"type": "string", "description": "Where this came from (a tool call, a file, etc)."},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["fact", "confidence"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_facts",
            "description": (
                "Search the structured fact ledger for previously recorded "
                "facts matching a keyword, before assuming something isn't "
                "known yet or re-deriving it from scratch."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "Keyword or phrase to search for."}
                },
                "required": ["keyword"],
            },
        },
    },
]


MAX_TOOL_RESPONSE_CHARS = 4000  # keep tool output from blowing the small context budget
MAX_FILE_READ_CHARS = 8000
RUN_PYTHON_TIMEOUT = 15

# ── GBNF grammar: forces the tool-decision call to emit only a strict
# JSON envelope — either {"tool": null} (no tool needed) or
# {"tool": "<name>", "arguments": {...}} matching one of our six schemas.
# This can't fix *what* the model decides, but it makes malformed JSON
# structurally impossible for this call, which is the most common failure
# mode for small models doing function calling.
#
# Simplification: http_request's "headers" field is treated as a plain
# string here (raw header text) rather than an open JSON object, since
# GBNF handles fixed schemas far more easily than arbitrary key/value
# maps. This is a real limitation of the constrained path specifically.

_GBNF_STRING = r'string ::= "\"" ( [^"\\] | "\\" . )* "\""'
_GBNF_WS = r'ws ::= [ \t\n]*'


def _gbnf_literal(value):
    """A GBNF rule fragment matching one exact quoted string."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"\\"{escaped}\\""'


def _gbnf_object(field_defs):
    """Build a GBNF object rule body from [(key, value_rule_ref), ...]."""
    parts = ['"{" ws']
    for i, (key, value_ref) in enumerate(field_defs):
        sep = ' "," ws ' if i > 0 else ' '
        parts.append(f'{sep}"\\"{key}\\"" ws ":" ws {value_ref}')
    parts.append(' ws "}"')
    return "".join(parts)


def _gbnf_rule_id(s):
    """llama.cpp's GBNF rule names may only contain letters, digits, and
    hyphens (see is_word_char in llama.cpp's grammar parser) — underscores
    are NOT valid there, even though they're valid inside a quoted string
    literal. Tool/parameter names use underscores (web_search, http_request,
    ...), so any rule identifier built from them must be sanitized or the
    parser silently truncates the reference at the underscore, produces a
    dangling/undefined rule, and llama-server rejects the whole grammar
    with an HTTP 400. This only affects rule IDENTIFIERS — the literal
    JSON text matched via _gbnf_literal (e.g. "web_search") keeps the real
    underscored name, since that's just string content, not a rule name."""
    return s.replace("_", "-")


def _build_tool_call_rules(tools, max_batch=3):
    """Shared by both grammar builders below: generates the GBNF rules
    for the tool_calls array itself (call/calls-nonempty/calls-array
    plus each tool's t-<name>/args-<name> rules), returned as a list of
    rule lines. Callers prepend their own root rule referencing
    'calls-array'."""
    lines = []
    call_alt = " | ".join(f't-{_gbnf_rule_id(t["function"]["name"])}' for t in tools)
    lines.append(f'call ::= {call_alt}')
    rep = " | ".join(
        "call" if i == 1 else "call (\",\" ws call){%d}" % (i - 1)
        for i in range(1, max_batch + 1)
    )
    lines.append(f'calls-nonempty ::= {rep}')
    lines.append('calls-array ::= "[" ws "]" | "[" ws calls-nonempty ws "]"')

    for t in tools:
        fn = t["function"]
        name = fn["name"]
        rule_id = _gbnf_rule_id(name)
        props = fn.get("parameters", {}).get("properties", {})
        arg_fields = []
        for pname, pdef in props.items():
            if "enum" in pdef:
                rule_name = f'{rule_id}-{_gbnf_rule_id(pname)}-enum'
                lines.append(
                    f'{rule_name} ::= ' + " | ".join(_gbnf_literal(v) for v in pdef["enum"])
                )
                arg_fields.append((pname, rule_name))
            else:
                # Treat everything else (string, object-as-raw-string) as a
                # plain JSON string in the constrained path — see module
                # note above re: http_request headers.
                arg_fields.append((pname, "string"))

        args_rule = f'args-{rule_id}'
        lines.append(f'{args_rule} ::= ' + _gbnf_object(arg_fields))
        lines.append(
            f't-{rule_id} ::= ' + _gbnf_object([("tool", _gbnf_literal(name)),
                                                 ("arguments", args_rule)])
        )
    return lines


def build_tool_decision_grammar(tools, max_batch=3):
    """Generates a GBNF grammar accepting a JSON object of the shape:
      {"tool_calls": []}                              — no tool needed
      {"tool_calls": [{"tool": "...", "arguments": {...}}, ...]}  — 1-N calls

    Batching independent calls into one decision (suggestion 6) cuts total
    plan+decision+critique round-trips roughly in half for multi-fact
    tasks, since the model doesn't need a full extra round per lookup.
    max_batch caps it so a confused model can't request an unbounded
    number of calls in one shot.
    """
    lines = [_GBNF_WS, _GBNF_STRING]
    lines.append('root ::= "{" ws "\\"tool_calls\\"" ws ":" ws calls-array ws "}"')
    lines.extend(_build_tool_call_rules(tools, max_batch))
    return "\n".join(lines)


TOOL_DECISION_GRAMMAR = build_tool_decision_grammar(TOOLS)


def build_plan_and_decision_grammar(tools, max_batch=3):
    """New improvement 3: combines the plan step and the tool-decision
    step into ONE grammar-constrained call:
      {"plan": "...", "tool_calls": [...]}
    instead of two full separate model calls (an unconstrained plan call
    followed by a grammar-constrained decision call). Nanbeige4.1-3B is
    specifically trained for sustained, coherent reasoning within a
    single forward pass — asking it to plan in one call and decide in a
    separate one fragments that trained strength as well as costing an
    extra round-trip; one combined call is both more efficient and a
    better fit for how the model is meant to be used."""
    lines = [_GBNF_WS, _GBNF_STRING]
    lines.append(
        'root ::= "{" ws "\\"plan\\"" ws ":" ws string ws "," ws '
        '"\\"tool_calls\\"" ws ":" ws calls-array ws "}"'
    )
    lines.extend(_build_tool_call_rules(tools, max_batch))
    return "\n".join(lines)


PLAN_AND_DECISION_GRAMMAR = build_plan_and_decision_grammar(TOOLS)

# ── Few-shot example baked into the system prompt (suggestion 5): shows
# the model one concrete plan → tool-call → answer sequence so it has a
# pattern to imitate rather than inferring format purely from the schema.
FEW_SHOT_EXAMPLE = """
Example of how you should operate:
User: What's the latest stable version of Python?
You (plan): I should search the web since this could have changed since training.
You (tool decision): {"tool": "web_search", "arguments": {"query": "latest stable Python version"}}
[tool result returned]
You (answer): The latest stable version is Python 3.13.
"""


def web_search(query, max_results=5, timeout=10):
    """Scrape DuckDuckGo's lite HTML endpoint (no API key required).
    Returns a plain-text digest suitable for feeding back to the model."""
    try:
        resp = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (Linux; Android)"},
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        return f"[web_search error: {e}]"

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for r in soup.select(".result")[:max_results]:
        title_el = r.select_one(".result__title")
        snippet_el = r.select_one(".result__snippet")
        title = title_el.get_text(strip=True) if title_el else ""
        snippet = snippet_el.get_text(strip=True) if snippet_el else ""
        if title:
            results.append(f"- {title}: {snippet}")

    if not results:
        return "No results found."
    return "\n".join(results)


def http_request(method, url, headers=None, body=None, timeout=15):
    """General-purpose HTTP call the model can construct itself — for
    hitting a specific API/doc/endpoint it already knows about, rather than
    only being able to search. Guarded with a scheme check, a timeout, and
    response-size truncation so a runaway request can't hang the session
    or blow the model's small context window."""
    method = (method or "GET").upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        return f"[http_request error: unsupported method '{method}']"
    if not (url or "").lower().startswith(("http://", "https://")):
        return "[http_request error: url must start with http:// or https://]"

    parsed_headers = {}
    if headers:
        if isinstance(headers, dict):
            parsed_headers = headers
        else:
            # The grammar can only ever hand this in as a plain string
            # (see module note above), never a real JSON object — parse it
            # ourselves rather than assuming it's already a dict.
            try:
                parsed_headers = json.loads(headers) or {}
            except (json.JSONDecodeError, TypeError):
                parsed_headers = {}

    req_kwargs = {"timeout": timeout, "headers": parsed_headers}
    req_kwargs["headers"].setdefault("User-Agent", "Mozilla/5.0 (Linux; Android) ncoder-cli")

    if body:
        stripped = body.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            req_kwargs["headers"].setdefault("Content-Type", "application/json")
        req_kwargs["data"] = body

    try:
        resp = requests.request(method, url, **req_kwargs)
    except requests.exceptions.RequestException as e:
        return f"[http_request error: {e}]"

    text = resp.text or ""
    truncated = len(text) > MAX_TOOL_RESPONSE_CHARS
    text = text[:MAX_TOOL_RESPONSE_CHARS]
    suffix = "\n[...truncated...]" if truncated else ""
    return f"HTTP {resp.status_code}\n{text}{suffix}"


# ── Sandboxed workspace file access ───────────────────────────────────────
def _resolve_workspace_path(rel_path):
    """Resolve a relative path against WORKSPACE_DIR and refuse anything
    that escapes it (via .. or an absolute path) — this is what keeps
    read_file/write_file from touching the rest of the phone."""
    target = os.path.realpath(os.path.join(WORKSPACE_DIR, rel_path or ""))
    workspace_real = os.path.realpath(WORKSPACE_DIR)
    if not (target == workspace_real or target.startswith(workspace_real + os.sep)):
        return None
    return target


def read_file(path):
    target = _resolve_workspace_path(path)
    if target is None:
        return "[read_file error: path escapes the workspace sandbox]"
    if not os.path.isfile(target):
        return f"[read_file error: '{path}' not found in workspace]"
    try:
        with open(target, "r", errors="replace") as f:
            content = f.read(MAX_FILE_READ_CHARS + 1)
    except Exception as e:
        return f"[read_file error: {e}]"
    if len(content) > MAX_FILE_READ_CHARS:
        content = content[:MAX_FILE_READ_CHARS] + "\n[...truncated...]"
    return content


def write_file(path, content):
    target = _resolve_workspace_path(path)
    if target is None:
        return "[write_file error: path escapes the workspace sandbox]"
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as f:
            f.write(content or "")
    except Exception as e:
        return f"[write_file error: {e}]"
    return f"Wrote {len(content or '')} chars to {path}"


def list_directory(path=""):
    target = _resolve_workspace_path(path)
    if target is None:
        return "[list_directory error: path escapes the workspace sandbox]"
    if not os.path.isdir(target):
        return f"[list_directory error: '{path}' is not a directory]"
    try:
        entries = sorted(os.listdir(target))
    except Exception as e:
        return f"[list_directory error: {e}]"
    if not entries:
        return "(empty)"
    lines = []
    for e in entries:
        full = os.path.join(target, e)
        lines.append(f"{'d' if os.path.isdir(full) else 'f'}  {e}")
    return "\n".join(lines)


# ── New improvement 5: proper todo.md checklist parsing ───────────────────
# The previous "- [ ]" in todo_content substring check is fragile against
# any reasonable markdown checklist variation (different bullet char,
# indentation, tabs vs spaces) — a real risk since /auto's continue/stop
# decision depends entirely on this. A real parser handles the actual
# range of valid markdown checklist syntax instead of one exact substring.
_TODO_ITEM_RE = re.compile(r'^\s*[-*+]\s+\[([ xX])\]\s+(.*)$', re.MULTILINE)


def parse_todo(content):
    """Returns (total_items, unchecked_items, checked_items) parsed from
    markdown checklist syntax — tolerant of -, *, or + bullets, any
    indentation, and both lowercase/uppercase 'x' for checked items."""
    items = _TODO_ITEM_RE.findall(content or "")
    checked = [text for mark, text in items if mark.strip().lower() == "x"]
    unchecked = [text for mark, text in items if mark.strip() == ""]
    return len(items), unchecked, checked


def parse_todo_ordered(content):
    """Same underlying regex as parse_todo, but preserves the original
    item order with checked state inline as (is_checked, text) tuples —
    parse_todo's split checked/unchecked lists lose that ordering, which
    matters for rendering a real checklist rather than just counting."""
    items = _TODO_ITEM_RE.findall(content or "")
    return [(mark.strip().lower() == "x", text) for mark, text in items]


def render_todo_panel(content, border_style):
    """TUI improvement 1: renders todo.md as an actual checklist during
    /auto instead of only a numeric 'N/M remaining' count. Uses plain
    ASCII brackets ([x]/[ ]) rather than Unicode ballot-box glyphs —
    this project has already run into real Android-terminal-font
    rendering risk with fancier Unicode glyphs elsewhere (the banner's
    block-drawing characters), and a checklist has no good reason to
    take on that same risk for a cosmetic gain."""
    ordered = parse_todo_ordered(content)
    if not ordered:
        return Panel(Text("(no todo.md checklist yet)", style="dim"), title="todo.md", border_style="dim")
    lines = []
    for is_checked, text in ordered:
        if is_checked:
            lines.append(Text(f"[x] {text}", style="dim strike"))
        else:
            lines.append(Text(f"[ ] {text}", style=LOG_COLOR_ACTION))
    return Panel(Group(*lines), title="todo.md", border_style=border_style)


# ── Sandboxed Python execution ────────────────────────────────────────────
def run_python(code):
    """Runs code as a fresh subprocess (not in-process) so a crash, infinite
    loop, or accidental system call can't take down the CLI itself. Timeout
    and output size are both capped."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=WORKSPACE_DIR,
            capture_output=True,
            text=True,
            timeout=RUN_PYTHON_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"[run_python error: exceeded {RUN_PYTHON_TIMEOUT}s timeout]"
    except Exception as e:
        return f"[run_python error: {e}]"

    out = result.stdout or ""
    err = result.stderr or ""
    combined = f"stdout:\n{out}"
    if err:
        combined += f"\nstderr:\n{err}"
    if len(combined) > MAX_TOOL_RESPONSE_CHARS:
        combined = combined[:MAX_TOOL_RESPONSE_CHARS] + "\n[...truncated...]"
    return combined


# ── Keyword-ranked notes search (TF-IDF style, no embeddings model) ───────
# Deliberately not "semantic" search: running a second embeddings model
# alongside the 3B LLM would compete for the same scarce RAM on a 4GB
# device. This is plain ranked term matching, described honestly as such.
_WORD_RE = re.compile(r"[a-zA-Z0-9']+")
_TEXT_EXTS = {".txt", ".md", ".py", ".json", ".jsonl", ".csv", ".log", ".yaml", ".yml"}


def _tokenize(text):
    return [w.lower() for w in _WORD_RE.findall(text)]


def search_notes(query, top_k=3, snippet_chars=300):
    query_terms = set(_tokenize(query))
    if not query_terms:
        return "[search_notes error: empty query]"

    candidates = []  # (path, paragraph_text)
    for root, _dirs, files in os.walk(WORKSPACE_DIR):
        for fname in files:
            if os.path.splitext(fname)[1].lower() not in _TEXT_EXTS:
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, WORKSPACE_DIR)
            try:
                with open(full, "r", errors="replace") as f:
                    text = f.read(MAX_FILE_READ_CHARS)
            except Exception:
                continue
            for para in re.split(r"\n\s*\n", text):
                para = para.strip()
                if para:
                    candidates.append((rel, para))

    if not candidates:
        return "No indexable files found in workspace."

    # Simple TF-IDF-ish scoring: term frequency in paragraph weighted by
    # inverse document frequency across all paragraphs found.
    doc_freq = Counter()
    for _rel, para in candidates:
        terms_in_para = set(_tokenize(para))
        for t in query_terms & terms_in_para:
            doc_freq[t] += 1

    n_docs = len(candidates)
    scored = []
    for rel, para in candidates:
        para_terms = _tokenize(para)
        tf = Counter(para_terms)
        score = 0.0
        for term in query_terms:
            if tf[term]:
                idf = math.log((n_docs + 1) / (doc_freq[term] + 1)) + 1
                score += tf[term] * idf
        if score > 0:
            scored.append((score, rel, para))

    if not scored:
        return "No matches found."

    scored.sort(key=lambda x: x[0], reverse=True)
    out_lines = []
    for score, rel, para in scored[:top_k]:
        snippet = para[:snippet_chars] + ("..." if len(para) > snippet_chars else "")
        out_lines.append(f"[{rel}] (score {score:.1f})\n{snippet}")
    return "\n\n".join(out_lines)


# ── New improvement 1: structured fact ledger ─────────────────────────────
# Distinct from invariants.md (free-text, always re-injected verbatim every
# round): this is a queryable, structured store for discrete facts — a
# later subtask asks "what do we already know about X" via query_facts
# instead of either re-deriving it or relying on it happening to still be
# in the live context window.
FACTS_LEDGER_FILE = "facts_ledger.json"


def _load_facts_ledger():
    raw = read_file(FACTS_LEDGER_FILE)
    if raw.startswith("[read_file error"):
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def record_fact(fact, source="", confidence="medium"):
    ledger = _load_facts_ledger()
    ledger.append({
        "fact": fact,
        "source": source,
        "confidence": confidence,
        "timestamp": datetime.now().isoformat(),
    })
    write_file(FACTS_LEDGER_FILE, json.dumps(ledger, indent=2))
    return f"Recorded ({confidence} confidence): {fact}"


def query_facts(keyword, top_k=5):
    ledger = _load_facts_ledger()
    if not ledger:
        return "No facts recorded yet."
    keyword_lower = (keyword or "").lower()
    matches = [e for e in ledger if keyword_lower in e.get("fact", "").lower()]
    if not matches:
        return f"No recorded facts match '{keyword}'."
    lines = [
        f"[{e.get('confidence', '?')} confidence, from {e.get('source') or 'unknown'}] {e['fact']}"
        for e in matches[:top_k]
    ]
    return "\n".join(lines)


# ── New improvement 6: reusable playbook cache ────────────────────────────
# After a successful /auto run, its plan→tool-sequence pattern is saved to
# the workspace. Retrieval deliberately reuses the existing search_notes
# tool rather than adding a dedicated one — playbooks.jsonl is just
# another indexed workspace file, and the model can already search it the
# same way it searches scratchpad.md, with no new tool/grammar surface.
PLAYBOOK_FILE = "playbooks.jsonl"


def extract_tool_sequence(messages):
    """Pulls the ordered list of '(used X)' markers already present in
    the message list — a compact fingerprint of which tools were used in
    which order, without duplicating the full transcript that's already
    in scratchpad.md."""
    seq = []
    for m in messages:
        content = m.get("content")
        if m.get("role") == "assistant" and isinstance(content, str) and content.startswith("(used ") and content.endswith(")"):
            seq.append(content[len("(used "):-1])
    return seq


def save_playbook(goal, tool_sequence, complexity=None):
    if not tool_sequence:
        return  # nothing worth caching if no tools were actually used
    entry = {
        "goal": goal,
        "tool_sequence": tool_sequence,
        "complexity": complexity,  # refinement 4: lets a future similar goal reuse this observed tier
        "timestamp": datetime.now().isoformat(),
    }
    target = _resolve_workspace_path(PLAYBOOK_FILE)
    if target is None:
        return
    try:
        # Blank-line separated so search_notes' paragraph splitter treats
        # each playbook entry as its own searchable unit, not one giant
        # blob spanning the whole file.
        with open(target, "a") as f:
            f.write(json.dumps(entry) + "\n\n")
    except Exception:
        pass  # caching a playbook must never crash the actual task


def _load_playbooks():
    raw = read_file(PLAYBOOK_FILE)
    if raw.startswith("[read_file error"):
        return []
    entries = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        try:
            entries.append(json.loads(block))
        except json.JSONDecodeError:
            continue
    return entries


def find_playbook_match(goal, min_overlap=0.5):
    """Refinement 4: reuse a prior successful run's observed complexity
    tier when the new goal closely resembles one already completed,
    skipping a fresh classification call entirely. Uses simple Jaccard
    token overlap — cheap, no model call needed, consistent with the
    existing keyword-based heuristics elsewhere in this file (e.g.
    _needs_verification) rather than adding a new kind of matching
    mechanism. Returns (complexity, overlap_score) or None if nothing
    matches well enough."""
    entries = _load_playbooks()
    if not entries:
        return None
    goal_terms = set(_tokenize(goal))
    if not goal_terms:
        return None

    best = None
    best_score = 0.0
    for e in entries:
        if not e.get("complexity"):
            continue  # entries from before this field existed, or malformed
        stored_terms = set(_tokenize(e.get("goal", "")))
        if not stored_terms:
            continue
        overlap = len(goal_terms & stored_terms) / len(goal_terms | stored_terms)
        if overlap > best_score:
            best_score = overlap
            best = e

    if best and best_score >= min_overlap:
        return best["complexity"], best_score
    return None


# ── Termux:API bridge (clipboard, notifications) ──────────────────────────
def termux_api(action, text=None, title=None):
    binaries = {
        "clipboard_get": ["termux-clipboard-get"],
        "clipboard_set": ["termux-clipboard-set"],
        "notify": ["termux-notification"],
    }
    if action not in binaries:
        return f"[termux_api error: unknown action '{action}']"

    try:
        if action == "clipboard_get":
            result = subprocess.run(binaries[action], capture_output=True, text=True, timeout=5)
            return result.stdout.strip() or "(clipboard empty)"
        elif action == "clipboard_set":
            subprocess.run(binaries[action], input=(text or ""), text=True, timeout=5)
            return "Clipboard updated."
        elif action == "notify":
            cmd = binaries[action] + ["-t", title or "NCoder", "-c", text or ""]
            subprocess.run(cmd, timeout=5)
            return "Notification sent."
    except FileNotFoundError:
        return (
            f"[termux_api error: '{binaries[action][0]}' not found — "
            "install the Termux:API app and package (`pkg install termux-api`) "
            "for this to work]"
        )
    except Exception as e:
        return f"[termux_api error: {e}]"


def notify_completion(title, text):
    """New improvement 9: a phone is more likely than a desktop terminal
    to be put down and walked away from, especially during a long /auto
    run — this fires a notification (and, best-effort, a vibration) at
    the points where that matters: /auto finishing or hitting
    request_user_input. Silently does nothing if Termux:API isn't
    installed, same graceful-degradation pattern as termux_api itself —
    a missing notification should never interrupt or fail the actual
    task."""
    try:
        termux_api("notify", text=text, title=title)
    except Exception:
        pass
    try:
        subprocess.run(["termux-vibrate", "-d", "300"], timeout=3)
    except Exception:
        pass  # best-effort; no Termux:API or no vibration motor is fine


def _is_tool_error(tool_name, result):
    """New improvement 3: heuristic failure detection across all tools.
    Every tool implementation in this file returns errors as a bracketed
    '[toolname error: ...]' string by convention, and run_python reports
    a non-empty stderr section — checked for both patterns rather than
    relying on exceptions, since these tools deliberately catch and
    stringify their own failures instead of raising."""
    if not isinstance(result, str):
        return False
    if result.startswith(f"[{tool_name} error"):
        return True
    if tool_name == "run_python" and "\nstderr:\n" in result:
        stderr_part = result.split("\nstderr:\n", 1)[1].strip()
        return bool(stderr_part)
    return False


def _escalate_tool_failure(tool_name, escalated_tools, working, console):
    """New improvement 5: extends failure escalation to track WHICH
    tools have already been nudged away from this turn, so round 5
    doesn't suggest the same 'try something different' advice round 3
    already gave for the same tool, and so the model is told plainly
    when multiple distinct approaches have all failed (at which point
    continuing to nudge is unlikely to help — better to make that
    visible than to keep repeating generic advice)."""
    already_tried_others = tool_name in escalated_tools
    escalated_tools.add(tool_name)
    distinct_failed = len(escalated_tools)

    if distinct_failed >= 2:
        console.print(
            f"· {tool_name} has also failed repeatedly ({distinct_failed} different tools have now failed) — consider request_user_input if nothing else works",
            style=LOG_COLOR_WARN,
        )
        working.append({
            "role": "user",
            "content": (
                f"{tool_name} has also failed repeatedly. {distinct_failed} "
                "different tools/approaches have now failed on this task — "
                "if you're out of genuinely different options, use "
                "request_user_input rather than continuing to retry."
            ),
        })
    else:
        console.print(f"· {tool_name} has failed repeatedly — nudging a different approach", style=LOG_COLOR_WARN)
        working.append({
            "role": "user",
            "content": (
                f"{tool_name} has failed repeatedly. Try a genuinely "
                "different tool or approach instead of retrying the same "
                "thing."
            ),
        })


def _creates_cycle(name, deps, pending_subtasks):
    """New improvement 2: before queueing a subtask waiting on other
    pending subtasks, check whether following those dependencies'
    own dependency chains ever loops back to this subtask's own name —
    a real risk with dependency-ordered subtasks that nothing previously
    checked for. An undetected cycle (A waits on B, B waits on A) would
    silently deadlock both forever, since neither could ever become
    unblocked — burning the whole round/time budget with no progress
    and no error, which is worse than an explicit rejection."""
    visited = set()

    def _depends_back_on(current):
        if current == name:
            return True
        if current in visited:
            return False
        visited.add(current)
        meta = pending_subtasks.get(current)
        if not meta:
            return False
        return any(_depends_back_on(d) for d in meta["depends_on"])

    return any(_depends_back_on(d) for d in deps)


def dispatch_tool_call(call):
    """Execute a single tool call dict (OpenAI tool_calls format) and
    return the string result to send back as a 'tool' role message."""
    name = call.get("function", {}).get("name")
    try:
        args = json.loads(call.get("function", {}).get("arguments", "{}"))
    except json.JSONDecodeError:
        args = {}

    if name == "web_search":
        return web_search(args.get("query", ""))
    elif name == "http_request":
        return http_request(
            args.get("method", "GET"),
            args.get("url", ""),
            headers=args.get("headers"),
            body=args.get("body"),
        )
    elif name == "read_file":
        return read_file(args.get("path", ""))
    elif name == "write_file":
        return write_file(args.get("path", ""), args.get("content", ""))
    elif name == "list_directory":
        return list_directory(args.get("path", ""))
    elif name == "run_python":
        return run_python(args.get("code", ""))
    elif name == "search_notes":
        return search_notes(args.get("query", ""))
    elif name == "termux_api":
        return termux_api(args.get("action", ""), args.get("text"), args.get("title"))
    elif name == "record_fact":
        return record_fact(args.get("fact", ""), args.get("source", ""), args.get("confidence", "medium"))
    elif name == "query_facts":
        return query_facts(args.get("keyword", ""))
    return f"[unknown tool: {name}]"


def chat_completion(base_url, messages, max_tokens, temperature, grammar=None):
    """Non-streaming call. If `grammar` (a GBNF string) is given, llama.cpp
    constrains decoding so the output can only match that grammar —
    used for the tool-decision step so malformed JSON is structurally
    impossible rather than merely discouraged by the prompt."""
    payload = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
        "top_p": SAMPLING_TOP_P,             # new improvement 4
        "repeat_penalty": SAMPLING_REPEAT_PENALTY,
        "cache_prompt": True,  # suggestion 5 — see stream_chat for rationale
    }
    if grammar:
        payload["grammar"] = grammar
    resp = requests.post(
        f"{base_url}/v1/chat/completions", json=payload, timeout=(5, 300)
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]


def get_plan(base_url, messages, max_tokens, temperature):
    """Suggestion 2: externalize a short plan before acting. Small models
    orchestrate multi-step tasks more reliably when the plan is written
    out as visible text first, rather than decided implicitly inside a
    single tool-call turn."""
    plan_prompt = messages + [{
        "role": "user",
        "content": (
            "Before answering, think for one short sentence: do you need a "
            "tool for this, and if so, which one and why? If no tool is "
            "needed, say so briefly. Do not answer the original question yet."
        ),
    }]
    try:
        msg = chat_completion(base_url, plan_prompt, min(max_tokens, 100), temperature)
        return (msg.get("content") or "").strip()
    except requests.exceptions.RequestException:
        return ""


def classify_task_complexity(base_url, original_goal, max_tokens):
    """New improvement 4: one cheap call, before the round loop starts,
    to scale the round-count/wall-clock budget to the task instead of
    using one fixed budget for every turn. A quick lookup shouldn't be
    given the same headroom as a genuinely multi-stage task, and a
    complex task shouldn't be cut off at a budget sized for quick
    lookups. Falls back to DEFAULT_COMPLEXITY (moderate) if the goal is
    empty or the classification call fails or returns something
    unrecognized — a wrong guess here just means a suboptimal budget,
    not a broken turn, so failing safe to the existing default is the
    right tradeoff."""
    if not original_goal:
        return DEFAULT_COMPLEXITY

    classify_prompt = [{
        "role": "user",
        "content": (
            "Classify how much work this request likely needs. Reply "
            "with exactly one word: simple (a quick fact or one-step "
            "answer), moderate (a few steps or one tool call), or "
            "complex (multiple dependent steps, research, or nested "
            f"subtasks).\n\nRequest: {original_goal}"
        ),
    }]
    try:
        msg = chat_completion(base_url, classify_prompt, min(max_tokens, 20), 0.0)
        label = (msg.get("content") or "").strip().lower()
    except requests.exceptions.RequestException:
        return DEFAULT_COMPLEXITY

    for tier in COMPLEXITY_BUDGETS:
        if tier in label:
            return tier
    return DEFAULT_COMPLEXITY


def check_decomposable(base_url, goal, max_tokens):
    """New improvement 4: one cheap call before /auto's multi-turn loop
    even starts, checking whether the goal can actually be broken into
    concrete steps — rather than discovering mid-task, several rounds
    in, that the goal was too vague to make a todo list from in the
    first place. Fails open (treats as decomposable) if the check itself
    fails, since a wrong "yes" here just means the normal loop discovers
    the problem a bit later — not worse than skipping the check
    entirely — while a wrong "no" would incorrectly block a legitimate
    task from ever starting."""
    check_prompt = [{
        "role": "user",
        "content": (
            "Can the following goal be broken into concrete, actionable "
            "steps for a todo list, or is it too vague/ambiguous to "
            "decompose without more information from the user? Reply "
            "with exactly one word: YES or NO, then optionally a short "
            "reason on the same line.\n\n"
            f"Goal: {goal}"
        ),
    }]
    try:
        msg = chat_completion(base_url, check_prompt, min(max_tokens, 60), 0.0)
        verdict = (msg.get("content") or "").strip()
    except requests.exceptions.RequestException:
        return True, ""

    if verdict.upper().startswith("NO"):
        reason = verdict[2:].strip(" :-—") or "the goal is too vague to break into concrete steps"
        return False, reason
    return True, ""


# ── New improvement 1: verification-gated subtask completion ─────────────
_VERIFICATION_KEYWORDS = (
    "test", "bug", "fix", "implement", "code", "verify", "correct",
    "calculate", "compute", "fact", "accurate", "debug", "patch",
)


def _needs_verification(goal):
    """Heuristic: does this subtask's goal involve a checkable claim
    (code that should pass tests, a computed number, a factual claim)?
    Keyword-based rather than a model call, since this gate needs to be
    cheap and fire on every pop_subtask, not add a full extra round."""
    goal_lower = (goal or "").lower()
    return any(kw in goal_lower for kw in _VERIFICATION_KEYWORDS)


# ── New improvement 4: contradiction detection across subtask results ────
def detect_contradiction(base_url, new_name, new_result, completed_results, max_tokens):
    """Cheap check run whenever a subtask completes and at least one
    other subtask has already finished: do the results conflict? This
    catches the specific long-task failure mode where independent
    subtasks produced inconsistent facts and nothing flagged it before
    they got combined into a final answer. Returns "" if no conflict (or
    if the check itself fails — fails open rather than blocking
    progress), otherwise a short description of the conflict."""
    if not completed_results:
        return ""

    prior_lines = "\n".join(f"- {name}: {res}" for name, res in completed_results.items())
    check_prompt = [{
        "role": "user",
        "content": (
            f"New result from '{new_name}': {new_result}\n\n"
            f"Prior results:\n{prior_lines}\n\n"
            "Do any of these directly contradict each other (not just "
            "differ in detail/scope)? Reply with exactly NONE if no "
            "contradiction, or a one-sentence description of the "
            "conflict if there is one."
        ),
    }]
    try:
        msg = chat_completion(base_url, check_prompt, min(max_tokens, 60), 0.0)
        verdict = (msg.get("content") or "").strip()
    except requests.exceptions.RequestException:
        return ""

    if verdict.upper().startswith("NONE"):
        return ""
    return verdict


def get_tool_decision(base_url, messages, plan_text, max_tokens, temperature):
    """Suggestion 1 + 6: grammar-constrained call that can only emit
    {"tool_calls": []} (nothing needed) or {"tool_calls": [{"tool":...,
    "arguments": {...}}, ...]} — up to 3 batched calls per decision, see
    build_tool_decision_grammar."""
    decision_prompt = messages + [{
        "role": "user",
        "content": (
            f"Your plan: {plan_text}\n\n"
            "Now output ONLY a JSON object deciding on tool calls "
            "(batch multiple independent lookups into one list if useful), "
            "nothing else."
        ),
    }]
    msg = chat_completion(
        base_url, decision_prompt, min(max_tokens, 300), temperature,
        grammar=TOOL_DECISION_GRAMMAR,
    )
    raw = (msg.get("content") or "").strip()
    try:
        parsed = json.loads(raw)
        calls = parsed.get("tool_calls", [])
        return calls if isinstance(calls, list) else []
    except json.JSONDecodeError:
        return []


def get_plan_and_decision(base_url, messages, max_tokens, temperature):
    """New improvement 3: one grammar-constrained call producing both
    the plan text AND the tool-call decision, replacing two separate
    calls (get_plan + get_tool_decision) with one. Falls back to an
    empty plan and no tool calls on malformed output rather than raising
    — same fail-safe behavior as get_tool_decision, since a malformed
    round should skip cleanly (caught by the existing "no calls -> break"
    logic) rather than crash the whole turn."""
    prompt = messages + [{
        "role": "user",
        "content": (
            "Output ONLY a JSON object with a short one-sentence plan "
            "and your tool call decision (batch multiple independent "
            "lookups into one list if useful, or an empty list if no "
            "tool is needed), nothing else."
        ),
    }]
    msg = chat_completion(
        base_url, prompt, min(max_tokens, 350), temperature,
        grammar=PLAN_AND_DECISION_GRAMMAR,
    )
    raw = (msg.get("content") or "").strip()
    try:
        parsed = json.loads(raw)
        plan_text = parsed.get("plan", "") or ""
        calls = parsed.get("tool_calls", [])
        return plan_text, (calls if isinstance(calls, list) else [])
    except json.JSONDecodeError:
        return "", []


# ── Long-task reliability helpers (suggestions 1-8, plus 4 further ones) ──
CHECKPOINT_FILE = "task_state.json"       # suggestion 1 (now diff-based, see below)
TODO_FILE = "todo.md"                     # suggestion 2
SCRATCHPAD_FILE = "scratchpad.md"          # new: verbose intermediate detail lives here
INVARIANTS_FILE = "invariants.md"          # new: facts/constraints compaction can't touch
INVARIANT_TAG = "[PINNED INVARIANTS]"
CONTEXT_WARN_PCT = 70                     # suggestion 4
MAX_ROUND_SECONDS = 180                   # suggestion 8: wall-clock budget per turn (moderate-tier default)
MAX_BATCH_PER_ROUND = 3                   # suggestion 6 (batched tool calls)
SCRATCHPAD_SUMMARY_CHARS = 150             # inline preview length before pointing to scratchpad.md
REFLECTION_INTERVAL = 3                     # new: rounds between mid-task "still on track?" checks
CHECKPOINT_DIFF_COMPACT_THRESHOLD = 5       # new: collapse diffs into a fresh base after this many

# New improvement 4: complexity-scaled round/time budgets — a quick
# lookup shouldn't get the same budget as a genuinely multi-stage task,
# and vice versa. (max_rounds, time_budget_seconds) per tier.
COMPLEXITY_BUDGETS = {
    # New improvement 2: Nanbeige4.1-3B's own model card documents
    # reliably sustaining 500+ rounds of tool invocation (in a
    # specialized deep-search framework, not this exact harness — so
    # these are raised meaningfully, not scaled to match that number
    # directly). Combined with improvement 3 (merged plan+decision call
    # halving per-round overhead), there's real headroom to raise round
    # counts without proportionally raising wall-clock time. Time budget
    # remains the actual phone-side constraint (battery/thermal), not
    # round count — round caps here are a generous backstop, not the
    # thing expected to bind first.
    "simple":   (3, 60),
    "moderate": (12, 240),
    "complex":  (30, 480),
    # Reserved for explicit /auto invocations only — never chosen by
    # classify_task_complexity itself, so ordinary chat turns keep
    # conservative default budgets.
    "autonomous": (60, 1200),
}
DEFAULT_COMPLEXITY = "moderate"  # used if classification fails or is skipped

# Refinement 5: verification depth scaled per tier instead of a binary
# on/off switch. "moderate" gets the cheaper self-critique pass only
# (checks the answer against tool results) but skips the pricier
# whole-answer-vs-original-goal check; "complex"/"autonomous" get both,
# since that's where a combined answer silently missing part of the ask
# is the more likely and more costly failure mode. "simple" gets neither,
# unchanged from before.
VERIFICATION_DEPTH = {
    "simple": "none",
    "moderate": "light",
    "complex": "full",
    "autonomous": "full",
}
TOOL_FAILURE_ESCALATION_THRESHOLD = 2   # new improvement 3: consecutive failures on one tool before nudging a different approach
CONCURRENT_SAFE_TOOLS = {"web_search", "http_request"}  # new: I/O-bound tools safe to run in parallel within a batch


# ── New improvement 1: scratchpad separate from the visible/live context ──
def _append_scratchpad(text):
    """Full, unabridged tool output and plan chatter goes here — a plain
    append-only file, not part of the message list the model reasons
    over each round. This means verbose intermediate detail doesn't
    force early context compaction of things that actually matter; the
    model can still retrieve any of it later via read_file if it decides
    it needs the full detail back."""
    target = _resolve_workspace_path(SCRATCHPAD_FILE)
    if target is None:
        return
    try:
        with open(target, "a") as f:
            f.write(text.rstrip() + "\n\n")
    except Exception:
        pass  # scratchpad logging must never crash the actual task


def _short_result(tool_name, result, limit=SCRATCHPAD_SUMMARY_CHARS):
    """What actually goes into the live/compactable message list: a short
    preview plus a pointer to the full version, rather than the whole
    tool output. Keeps the working context small by default."""
    snippet = result[:limit]
    pointer = "" if len(result) <= limit else " ...(full output in scratchpad.md)"
    return f"Tool result from {tool_name}: {snippet}{pointer}"


# ── New improvement 2: pinned invariants, immune to compaction ───────────
def _read_invariants():
    raw = read_file(INVARIANTS_FILE)
    if raw.startswith("[read_file error"):
        return ""
    return raw.strip()


def inject_invariants(working):
    """Re-reads invariants.md fresh and ensures exactly one up-to-date
    copy sits right after the system message(s) — re-injected from disk
    every round rather than carried as regular conversation content, so
    context compaction can never summarize it away. The model is
    instructed (system prompt) to write load-bearing facts/constraints
    here as it discovers them."""
    content = _read_invariants()
    working = [m for m in working
               if not (isinstance(m.get("content"), str) and m["content"].startswith(INVARIANT_TAG))]
    if not content:
        return working
    insert_at = 0
    for i, m in enumerate(working):
        if m["role"] == "system":
            insert_at = i + 1
        else:
            break
    working = list(working)
    working.insert(insert_at, {"role": "user", "content": f"{INVARIANT_TAG}\n{content}"})
    return working


def _is_protected(m):
    """System messages and pinned invariants are never subject to
    compaction — everything else is fair game for summarization."""
    return m["role"] == "system" or (
        isinstance(m.get("content"), str) and m["content"].startswith(INVARIANT_TAG)
    )


def compact_context(base_url, working, keep_last=2, max_tokens=200, temperature=0.0):
    """Suggestion 3: when nearing the context ceiling, summarize older
    tool-result messages into one short line each via a cheap extra call,
    instead of hard-truncating (which can silently drop something the
    model still needed) or erroring out. System prompt + pinned
    invariants + the most recent exchanges are always kept verbatim."""
    if len(working) <= keep_last + 2:
        return working  # too short to be worth compacting

    protected = [m for m in working if _is_protected(m)]
    rest = [m for m in working if not _is_protected(m)]
    to_compact, to_keep = rest[:-keep_last], rest[-keep_last:]
    if not to_compact:
        return working

    digest_prompt = [{
        "role": "user",
        "content": (
            "Summarize each of the following conversation turns in one "
            "short line each, preserving any concrete facts, numbers, or "
            "file paths mentioned. Be terse.\n\n" +
            "\n".join(f"[{m['role']}] {str(m.get('content'))[:300]}" for m in to_compact)
        ),
    }]
    try:
        msg = chat_completion(base_url, digest_prompt, max_tokens, temperature)
        summary = (msg.get("content") or "").strip()
    except requests.exceptions.RequestException:
        return working  # if compaction itself fails, leave context as-is

    compacted = protected + [
        {"role": "user", "content": f"[earlier context, summarized]: {summary}"}
    ] + to_keep
    return compacted


def _call_signature(calls):
    """Suggestion 7: a hashable fingerprint of a batch of tool calls, used
    to detect the model requesting the identical thing two rounds in a
    row (a real stall pattern for small models doing unsupervised
    multi-step work)."""
    return json.dumps(calls, sort_keys=True)


# ── New improvement 5: diff-based checkpoints with undo ──────────────────
def save_checkpoint_diff(base_messages, diffs):
    try:
        write_file(CHECKPOINT_FILE, json.dumps({
            "base_messages": base_messages,
            "diffs": diffs,
        }, indent=2))
    except Exception:
        pass  # checkpointing must never crash the actual task


def load_checkpoint():
    raw = read_file(CHECKPOINT_FILE)
    if raw.startswith("[read_file error"):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def reconstruct_from_checkpoint(checkpoint):
    """Rebuilds the full message list from a base snapshot + incremental
    diffs. Cheaper to write than a full dump every round (only the new
    messages from that round are appended to the diff list), and it
    enables undo_last_step below."""
    msgs = list(checkpoint.get("base_messages", []))
    for d in checkpoint.get("diffs", []):
        msgs.extend(d.get("added", []))
    return msgs


def undo_last_step():
    """Pops the most recent round's diff and rewrites the checkpoint —
    a recovery path if a bad tool call sent later reasoning off the
    rails, without discarding the whole task."""
    checkpoint = load_checkpoint()
    if checkpoint is None or not checkpoint.get("diffs"):
        return None
    checkpoint["diffs"].pop()
    save_checkpoint_diff(checkpoint.get("base_messages", []), checkpoint["diffs"])
    return reconstruct_from_checkpoint(checkpoint)


def clear_checkpoint():
    target = _resolve_workspace_path(CHECKPOINT_FILE)
    if target and os.path.isfile(target):
        os.remove(target)


def reflect_on_progress(base_url, working, original_goal, max_tokens, temperature):
    """New improvement 3: distinct from the end-of-answer self-critique —
    this runs mid-task, every REFLECTION_INTERVAL rounds, and checks
    whether the task is still converging on the original goal *while
    there's still round/time budget left to change course*, rather than
    only catching a problem in the final answer after all rounds are
    spent. Kept cheap: short output, temperature 0 for a consistent
    verdict. Returns (still_on_track: bool, note: str)."""
    reflect_prompt = working + [{
        "role": "user",
        "content": (
            f"Original goal: {original_goal}\n\n"
            "In one short sentence: is your approach so far still "
            "converging on this goal, or should you change direction? "
            "Start your reply with YES or NO."
        ),
    }]
    try:
        msg = chat_completion(base_url, reflect_prompt, min(max_tokens, 60), 0.0)
        text = (msg.get("content") or "").strip()
    except requests.exceptions.RequestException:
        return True, ""  # if the check itself fails, don't block progress on it

    on_track = text.upper().startswith("YES")
    return on_track, text


def maybe_compact_checkpoint(base_messages, diffs):
    """New improvement 5: once enough round-diffs have accumulated,
    collapse them into a single fresh base snapshot (the same idea as
    context compaction, applied to the checkpoint file itself) so
    /resume after a very long task doesn't need to replay dozens of
    small diffs to reconstruct state. Returns (new_base, new_diffs)."""
    if len(diffs) < CHECKPOINT_DIFF_COMPACT_THRESHOLD:
        return base_messages, diffs
    collapsed_base = reconstruct_from_checkpoint({"base_messages": base_messages, "diffs": diffs})
    return collapsed_base, []



def run_turn_with_tools(base_url, messages, max_tokens, temperature, console, original_goal=None, complexity_override=None):
    """Plan → grammar-constrained tool decision (possibly batched) →
    execute, repeated up to a few rounds or until a wall-clock budget is
    spent, then returns the working message list for a final streamed
    answer. No user toggle — the model always has this available and
    decides for itself whether a round needs a tool.

    Subtask stack frames: push_subtask/pop_subtask let the model narrow
    its context to just a nested subtask, then fold only a short result
    back into the parent task — a manual call stack for context, most
    valuable when a task is too deep for flat compaction to handle
    gracefully. `root_working` always refers to the outermost task's
    message list (the one checkpointing and the final answer are based
    on); `working` may point at a narrowed subtask list while one is
    active.

    New improvement 1 (dependency-ordered subtasks): push_subtask accepts
    an optional depends_on list of other subtask names. If any dependency
    hasn't completed yet, the subtask is queued rather than started
    immediately, and the model is told what it's waiting on. This is
    scheduling order, not concurrent execution — there's a single
    inference stream, so "the graph" determines *sequence*, not
    parallelism, which is the honest and more reliable framing for a
    single-model setup.

    New improvement 7 (concurrent dispatch): independent web_search /
    http_request calls within one batched decision are genuinely
    I/O-bound (waiting on network, not on the model), so they're
    dispatched concurrently via a thread pool. Results are still
    appended to the context in original call order for determinism.
    """
    root_working = list(messages)
    working = root_working
    subtask_stack = []  # list of {"name":, "parent_working":, "depends_on": [...]}
    pending_subtasks = {}   # name -> {"goal":, "depends_on": [...]}
    completed_results = {}  # name -> result_summary, for dependency resolution
    subtask_verification_bounces = {}  # name -> times bounced for unverified completion
    fact_provenance = {}    # refinement 6: fact text -> name of the subtask that recorded it, for implicit cycle detection
    base_messages = list(messages)
    diffs = []

    # New improvement 4: classify complexity once up front, then scale
    # the round-count and wall-clock budget to match — instead of one
    # fixed 6-round/180s budget for every turn regardless of whether it's
    # a quick lookup or genuinely multi-stage work. complexity_override
    # bypasses classification entirely (used by /auto to force the
    # reserved "autonomous" tier rather than letting the classifier pick).
    playbook_match = None
    if complexity_override:
        complexity = complexity_override
    else:
        # Refinement 4: check the playbook cache before spending a fresh
        # classification call — if a closely-matching prior goal already
        # ran to completion at a known tier, reuse that instead of
        # re-deriving it from scratch every time.
        playbook_match = find_playbook_match(original_goal) if original_goal else None
        if playbook_match:
            complexity, overlap_score = playbook_match
            console.print(f"· reusing '{complexity}' tier from a similar prior task (match: {overlap_score:.0%})", style=LOG_COLOR_INFO)
        else:
            complexity = with_phase(
                console, None, "classifying",
                classify_task_complexity, base_url, original_goal, max_tokens,
            )
    max_rounds, time_budget_seconds = COMPLEXITY_BUDGETS[complexity]
    console.print(f"· task classified as {complexity} (budget: {max_rounds} rounds / {time_budget_seconds}s)", style=LOG_COLOR_INFO)

    start_time = time.time()
    status = RunStatus(complexity, max_rounds, time_budget_seconds, start_time)
    last_signature = None
    tool_failure_streak = {}  # tool_name -> consecutive failure count
    escalated_tools = set()   # tools already nudged away from — avoids repeating the same "try something different" advice, and signals when multiple distinct approaches have all failed
    halt_signal = None        # set when request_user_input fires

    # Refinement 3: adaptive reflection scheduling. Previously fired on a
    # flat every-REFLECTION_INTERVAL-rounds clock regardless of how the
    # task was actually going. Now pulled forward to the very next round
    # whenever a risk signal already being tracked elsewhere fires (a
    # bounced verification, an escalated tool failure, a detected
    # contradiction) — reflecting right when something's already gone
    # sideways, rather than only on a fixed schedule that might not
    # align with when it's actually useful. Reset back to the base
    # cadence after each reflection so a clean run doesn't pay for
    # reflection more often than the original fixed schedule would have.
    next_reflection_round = REFLECTION_INTERVAL

    def _pull_reflection_forward(round_i):
        nonlocal next_reflection_round
        next_reflection_round = min(next_reflection_round, round_i + 1)

    for round_i in range(max_rounds):
        if time.time() - start_time > time_budget_seconds:
            console.print(f"· time budget ({time_budget_seconds}s) reached, wrapping up with what's available", style=LOG_COLOR_WARN)
            break

        working = inject_invariants(working)
        if not subtask_stack:
            # inject_invariants always returns a new list object; when
            # we're at root level (no subtask open), root_working must
            # track that same new object, or later root-growth slicing
            # (root_working[root_len_before:]) and subtask push/pop
            # parent references would silently desync from what's
            # actually being appended to.
            root_working = working

        status.round_i = round_i

        # New improvement 3 + refinement 3: mid-task reflection, cheap
        # and now adaptively scheduled — catches the task drifting
        # off-goal while there's still budget left to correct it, rather
        # than only at the final answer, and fires sooner right after a
        # risk signal instead of waiting out a fixed clock.
        if original_goal and round_i > 0 and round_i >= next_reflection_round:
            on_track, note = with_phase(
                console, status, "reflecting",
                reflect_on_progress, base_url, working, original_goal, max_tokens, temperature,
            )
            next_reflection_round = round_i + REFLECTION_INTERVAL
            if not on_track:
                console.print(f"· reflection: may be off-track — {note}", style=LOG_COLOR_REFLECT)
                working.append({
                    "role": "user",
                    "content": f"Reflection check: reconsider your approach — {note}",
                })

        # New improvement 3: one combined grammar-constrained call
        # instead of two separate ones (an unconstrained plan call, then
        # a grammar-constrained decision call) — cuts round overhead
        # roughly in half, and better matches Nanbeige4.1-3B's own
        # trained strength at sustained, coherent single-pass reasoning
        # rather than fragmenting it across two calls.
        plan_text, calls = with_phase(
            console, status, "planning",
            get_plan_and_decision, base_url, working, max_tokens, temperature,
        )
        if plan_text:
            console.print(f"plan: {plan_text}", style=LOG_COLOR_PLAN)
        _append_scratchpad(f"### Round {round_i} plan\n{plan_text}")

        if not calls:
            break

        calls = calls[:MAX_BATCH_PER_ROUND]
        signature = _call_signature(calls)
        if signature == last_signature:
            console.print("· repeated identical tool call detected, stopping to avoid a stall", style=LOG_COLOR_WARN)
            break
        last_signature = signature

        root_len_before = len(root_working)

        # Partition this batch into orchestration calls (push/pop —
        # mutate control flow, must run in-order, one at a time) versus
        # concurrent-safe I/O calls (independent web/http lookups) versus
        # other sequential tool calls (files, python, termux — touch
        # shared state or aren't worth the complexity of parallelizing).
        concurrent_batch = []
        for call in calls:
            tool_name = call.get("tool")
            args = call.get("arguments", {}) or {}
            if not tool_name:
                continue

            if tool_name == "push_subtask":
                name = args.get("name", "subtask")
                goal = args.get("goal", "")
                depends_raw = (args.get("depends_on") or "").strip()
                deps = [d.strip() for d in depends_raw.split(",") if d.strip()]
                unmet = [d for d in deps if d not in completed_results]

                if unmet:
                    if _creates_cycle(name, deps, pending_subtasks):
                        console.print(f"· push_subtask({name}) rejected — circular dependency with {unmet}", style=LOG_COLOR_WARN)
                        working.append({
                            "role": "user",
                            "content": (
                                f"push_subtask('{name}') was rejected: it would create a "
                                f"circular dependency with {deps}. Restructure so subtasks "
                                "don't depend on each other in a loop."
                            ),
                        })
                        _append_scratchpad(f"### Rejected push_subtask '{name}' — circular dependency with {deps}")
                        continue

                    pending_subtasks[name] = {"goal": goal, "depends_on": deps}
                    console.print(f"→ push_subtask({name}) queued, waiting on {unmet}", style=LOG_COLOR_ACTION)
                    working.append({
                        "role": "user",
                        "content": f"Subtask '{name}' queued — waiting on: {', '.join(unmet)}.",
                    })
                    _append_scratchpad(f"### Queued subtask '{name}'\nGoal: {goal}\nWaiting on: {unmet}")
                    continue

                console.print(f"→ push_subtask({name})", style=LOG_COLOR_ACTION)
                dep_context = ""
                if deps:
                    dep_lines = "\n".join(f"- {d}: {completed_results[d]}" for d in deps)
                    dep_context = f"\nResults from dependencies:\n{dep_lines}"
                subtask_stack.append({"name": name, "goal": goal, "parent_working": working, "depends_on": deps})
                working = [{
                    "role": "system",
                    "content": (
                        f"Focused subtask: {name}\nGoal: {goal}{dep_context}\n"
                        "Work only on this; call pop_subtask with a "
                        "result_summary when done."
                    ),
                }]
                pending_subtasks.pop(name, None)
                _append_scratchpad(f"### Pushed subtask '{name}'\nGoal: {goal}{dep_context}")
                continue

            if tool_name == "pop_subtask":
                if not subtask_stack:
                    continue  # nothing to pop — ignore rather than error out

                result_summary = args.get("result_summary", "")
                confidence = args.get("confidence", "medium")
                verified_by = (args.get("verified_by") or "").strip()
                structured_raw = (args.get("structured_result") or "").strip()

                frame = subtask_stack[-1]  # peek, not pop yet — may bounce

                # New improvement 1: verification gate. Fails open after
                # one bounce — a small model that genuinely can't verify
                # shouldn't be stuck in an unbreakable loop; the point is
                # to make it TRY once, not to block forever.
                if (_needs_verification(frame["goal"]) and not verified_by
                        and subtask_verification_bounces.get(frame["name"], 0) < 1):
                    subtask_verification_bounces[frame["name"]] = subtask_verification_bounces.get(frame["name"], 0) + 1
                    console.print(f"· pop_subtask({frame['name']}) needs verification first — bounced", style=LOG_COLOR_REFLECT)
                    working.append({
                        "role": "user",
                        "content": (
                            "This subtask's goal involves something checkable. "
                            "Verify your result (e.g. run a test, cross-check a "
                            "fact) before calling pop_subtask again, or explain "
                            "in verified_by why it can't be verified."
                        ),
                    })
                    _append_scratchpad(f"### Bounced pop_subtask '{frame['name']}' — verification required")
                    _pull_reflection_forward(round_i)  # refinement 3: a bounce is a risk signal
                    continue

                console.print(f"→ pop_subtask ({confidence} confidence)", style=LOG_COLOR_ACTION)
                subtask_stack.pop()

                # New improvement 2: prefer the structured result for
                # downstream consumption (dependency injection, synthesis)
                # when the model provided one — precision matters more
                # than prose for anything a later subtask will actually
                # use, not just read.
                stored_result = result_summary
                if structured_raw:
                    try:
                        parsed_structured = json.loads(structured_raw)
                        stored_result = json.dumps(parsed_structured)
                    except json.JSONDecodeError:
                        stored_result = structured_raw  # fall back to raw text rather than discarding it

                # New improvement 3: confidence travels with the result,
                # not just the prose summary, so a low-confidence subtask
                # result can be weighted differently by whatever combines
                # it with others later.
                completed_results[frame["name"]] = f"[{confidence} confidence] {stored_result}"

                # New improvement 4: check for conflicts with prior
                # subtask results before folding this one in — catches
                # independent subtasks silently disagreeing before that
                # disagreement gets baked into a combined answer.
                prior_results = {k: v for k, v in completed_results.items() if k != frame["name"]}
                conflict = with_phase(
                    console, status, "checking consistency",
                    detect_contradiction, base_url, frame["name"], stored_result, prior_results, max_tokens,
                )

                parent = frame["parent_working"]

                unblocked = [
                    n for n, meta in pending_subtasks.items()
                    if all(d in completed_results for d in meta["depends_on"])
                ]
                note = f" ({len(unblocked)} queued subtask(s) now unblocked: {unblocked})" if unblocked else ""
                conflict_note = f"\nPOSSIBLE CONFLICT with prior results: {conflict}" if conflict else ""
                if conflict:
                    console.print(f"· possible conflict between '{frame['name']}' and prior subtask results: {conflict}", style=LOG_COLOR_WARN)
                    _pull_reflection_forward(round_i)  # refinement 3: a contradiction is a risk signal

                parent.append({
                    "role": "user",
                    "content": (
                        f"Subtask '{frame['name']}' completed ({confidence} confidence"
                        f"{', verified: ' + verified_by if verified_by else ''}): "
                        f"{result_summary}{note}{conflict_note}"
                    ),
                })
                working = parent
                _append_scratchpad(
                    f"### Popped subtask '{frame['name']}' [{confidence} confidence]\n"
                    f"Result: {result_summary}\n"
                    f"Verified: {verified_by or '(not verified)'}"
                    f"{conflict_note}"
                )
                continue

            if tool_name == "request_user_input":
                reason = args.get("reason", "")
                console.print(f"→ request_user_input: {reason}", style=LOG_COLOR_WARN)
                halt_signal = {"type": "needs_user_input", "reason": reason}
                _append_scratchpad(f"### Round {round_i}: requested user input\n{reason}")
                break  # stop processing this batch — nothing after this matters

            if tool_name in CONCURRENT_SAFE_TOOLS:
                concurrent_batch.append((tool_name, args))
                continue

            # Sequential dispatch for everything else (files, python, termux_api).
            console.print(f"→ {tool_name}({args})", style=LOG_COLOR_ACTION)
            fake_call = {
                "id": f"round{round_i}",
                "function": {"name": tool_name, "arguments": json.dumps(args)},
            }
            result = with_phase(console, status, f"running {tool_name}", dispatch_tool_call, fake_call)
            _append_scratchpad(f"### Round {round_i}: {tool_name}({args})\n{result}")
            working.append({"role": "assistant", "content": f"(used {tool_name})"})
            working.append({"role": "user", "content": _short_result(tool_name, result)})

            # Refinement 6: extends cycle detection beyond explicit
            # depends_on graphs. push_subtask/pop_subtask already reject
            # explicit circular dependencies before they can deadlock —
            # but there's a subtler version: a subtask name can only
            # appear in fact_provenance if it was active at some point
            # (it had to run to call record_fact), and a name can only
            # appear in pending_subtasks if it's currently queued,
            # not-yet-started. The only way both are true for the SAME
            # name is if that name was reused — an earlier run under
            # that name recorded a fact, and a later push_subtask call
            # reused the same name for a new, still-pending declaration.
            # That's a real (if narrower than "two live subtasks with a
            # circular data dependency") risk worth catching: querying a
            # fact under a name that's ambiguous between a finished run
            # and a not-yet-started one. Detect-and-warn, not
            # detect-and-block — query_facts already succeeded and
            # returned useful data, and this can't be proven to actually
            # matter for the current query, so blocking retroactively
            # isn't the right response the way rejecting an explicit
            # cycle is.
            if tool_name == "record_fact" and subtask_stack:
                fact_provenance[args.get("fact", "")] = subtask_stack[-1]["name"]
            elif tool_name == "query_facts" and subtask_stack:
                current_name = subtask_stack[-1]["name"]
                keyword_lower = (args.get("keyword") or "").lower()
                for fact_text, provenance_name in fact_provenance.items():
                    if (keyword_lower and keyword_lower in fact_text.lower()
                            and provenance_name != current_name
                            and provenance_name in pending_subtasks):
                        waiting_on = pending_subtasks[provenance_name]["depends_on"]
                        console.print(
                            f"· ambiguous subtask name: '{current_name}' used a fact recorded "
                            f"earlier under the name '{provenance_name}', but '{provenance_name}' "
                            f"is now ALSO a distinct pending subtask (waiting on {waiting_on})",
                            style=LOG_COLOR_WARN,
                        )
                        working.append({
                            "role": "user",
                            "content": (
                                f"Note: you just used a fact recorded by an earlier subtask named "
                                f"'{provenance_name}', but that name is now also a distinct pending "
                                f"subtask waiting on {waiting_on} — this name reuse is ambiguous. "
                                "Consider using a different, more specific name for the new one."
                            ),
                        })
                        _pull_reflection_forward(round_i)  # refinement 3: this is also a risk signal
                        break  # one warning per query is enough, avoid repeating for every matching fact

            # New improvement 3: escalate after repeated consecutive
            # failures on the SAME tool (unlike stall detection, this
            # catches varying arguments that keep failing, not just
            # identical repeated calls) — nudge toward a different
            # approach instead of letting the model grind on a tool
            # that clearly isn't working.
            if _is_tool_error(tool_name, result):
                tool_failure_streak[tool_name] = tool_failure_streak.get(tool_name, 0) + 1
                if tool_failure_streak[tool_name] >= TOOL_FAILURE_ESCALATION_THRESHOLD:
                    _escalate_tool_failure(tool_name, escalated_tools, working, console)
                    _pull_reflection_forward(round_i)  # refinement 3: an escalation is a risk signal
                    tool_failure_streak[tool_name] = 0  # avoid nudging every round after threshold
            else:
                tool_failure_streak[tool_name] = 0

        # New improvement 7: run the batch's independent I/O-bound calls
        # concurrently — genuinely faster wall-clock time for multi-fact
        # rounds, since these calls spend almost all their time waiting
        # on network I/O rather than CPU/model work. Results are appended
        # in original call order regardless of which thread finishes
        # first, so behavior stays deterministic and readable top-to-bottom.
        if concurrent_batch:
            console.print(
                f"→ {len(concurrent_batch)} concurrent: " +
                ", ".join(f"{name}({args})" for name, args in concurrent_batch),
                style=LOG_COLOR_ACTION,
            )

            def _run(item):
                name, args = item
                fake_call = {
                    "id": "concurrent",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
                return dispatch_tool_call(fake_call)

            # New improvement 3: show live per-tool status as each
            # completes (via as_completed) instead of one silent line
            # followed by a blocking wait for the whole batch — useful on
            # a slow/flaky connection where "did this hang?" is a real
            # question. Results are still recombined in ORIGINAL call
            # order afterward (not completion order), preserving the
            # existing determinism guarantee regardless of which finishes
            # first.
            results = [None] * len(concurrent_batch)
            done_flags = [False] * len(concurrent_batch)

            def _render_concurrent_status():
                lines = [
                    Text(f"  {'✓' if done_flags[i] else '…'} {name}({args})",
                         style=LOG_COLOR_ACTION if done_flags[i] else "dim")
                    for i, (name, args) in enumerate(concurrent_batch)
                ]
                return Group(*lines)

            with ThreadPoolExecutor(max_workers=len(concurrent_batch)) as pool:
                future_to_idx = {pool.submit(_run, item): i for i, item in enumerate(concurrent_batch)}
                with Live(_render_concurrent_status(), console=console, refresh_per_second=8, transient=True) as live:
                    for future in as_completed(future_to_idx):
                        idx = future_to_idx[future]
                        results[idx] = future.result()
                        done_flags[idx] = True
                        live.update(_render_concurrent_status())

            for (tool_name, args), result in zip(concurrent_batch, results):
                _append_scratchpad(f"### Round {round_i}: {tool_name}({args}) [concurrent]\n{result}")
                working.append({"role": "assistant", "content": f"(used {tool_name})"})
                working.append({"role": "user", "content": _short_result(tool_name, result)})

                if _is_tool_error(tool_name, result):
                    tool_failure_streak[tool_name] = tool_failure_streak.get(tool_name, 0) + 1
                    if tool_failure_streak[tool_name] >= TOOL_FAILURE_ESCALATION_THRESHOLD:
                        _escalate_tool_failure(tool_name, escalated_tools, working, console)
                        _pull_reflection_forward(round_i)  # refinement 3: an escalation is a risk signal
                        tool_failure_streak[tool_name] = 0
                else:
                    tool_failure_streak[tool_name] = 0

        if halt_signal:
            break  # already broke the calls loop; also stop the round loop

        # Safety: if the model pushed a subtask and the round budget/time
        # budget ends before it pops back out, don't leave the function
        # returning the narrow subtask context — the final answer needs
        # to address the original root-level question.
        if round_i == max_rounds - 1 and subtask_stack:
            while subtask_stack:
                frame = subtask_stack.pop()
                # Bug fix found while testing refinement 6: previously
                # only a generic placeholder note was folded into the
                # parent here — anything the subtask actually
                # accumulated (tool results, warnings like the
                # implicit-cycle note above) was silently discarded.
                # Now the child's own messages (skipping its one static
                # "Focused subtask..." system message) are preserved by
                # folding them into the parent before the closing note.
                child_messages = working[1:]
                frame["parent_working"].extend(child_messages)
                frame["parent_working"].append({
                    "role": "user",
                    "content": f"Subtask '{frame['name']}' did not finish before the round budget ran out.",
                })
                working = frame["parent_working"]

        # Suggestion 5 (diffs) + new improvement 5 (collapse diffs once
        # they accumulate, so /resume doesn't replay dozens of tiny steps
        # to reconstruct state on a very long task).
        new_root_msgs = root_working[root_len_before:]
        if new_root_msgs:
            diffs.append({
                "round": round_i,
                "timestamp": datetime.now().isoformat(),
                "added": new_root_msgs,
            })
            base_messages, diffs = maybe_compact_checkpoint(base_messages, diffs)
            save_checkpoint_diff(base_messages, diffs)

        # Suggestion 3 + 4: proactively compact and warn before hitting the
        # ceiling, rather than after a call fails. New improvement 4: the
        # nudge now explicitly tells the model it can search_notes the
        # scratchpad for anything that just got summarized away.
        _, pct = estimate_context_usage(working, 6144)
        if pct >= CONTEXT_WARN_PCT:
            console.print(f"· context at ~{pct}%, compacting older turns", style=LOG_COLOR_INFO)
            working = compact_context(base_url, working)
            working.append({
                "role": "user",
                "content": (
                    "Context is getting full — wrap up soon with the best "
                    "answer you can from what you have. If you need full "
                    "detail on something that was just summarized, use "
                    "search_notes — it can find it in scratchpad.md."
                ),
            })
            if not subtask_stack:
                root_working = working

    # If a subtask was still open when the loop ended for any other
    # reason (stall break, no more calls), fold back to root rather than
    # silently returning a narrowed context.
    while subtask_stack:
        frame = subtask_stack.pop()
        # Same fix as the mid-loop fold-back above: preserve the
        # subtask's own accumulated messages instead of discarding them
        # in favor of only a generic placeholder note.
        child_messages = working[1:]
        frame["parent_working"].extend(child_messages)
        frame["parent_working"].append({
            "role": "user",
            "content": f"Subtask '{frame['name']}' left open when the task loop ended.",
        })
        working = frame["parent_working"]

    return (working if not subtask_stack else root_working), halt_signal, complexity




def self_critique(base_url, working_messages, draft_answer, max_tokens, temperature, console):
    """Suggestion 7: one cheap extra call checks the draft answer against
    whatever tool results are in context. Returns a (possibly corrected)
    final answer. Catches cases where the model ignored or misread a tool
    result — a real failure mode on small models — at the cost of one
    more short generation."""
    critique_prompt = working_messages + [
        {"role": "assistant", "content": draft_answer},
        {"role": "user", "content": (
            "Check your answer above against any tool results in this "
            "conversation. If it's accurate and consistent with them, "
            "reply with exactly: OK\n"
            "If it's wrong or ignores a tool result, reply with the "
            "corrected answer only, nothing else."
        )},
    ]
    try:
        msg = chat_completion(base_url, critique_prompt, max_tokens, temperature)
        verdict = (msg.get("content") or "").strip()
    except requests.exceptions.RequestException:
        return draft_answer

    if verdict.upper().startswith("OK"):
        return draft_answer
    if verdict:
        console.print("· self-check revised the answer", style=LOG_COLOR_REFLECT)
        return verdict
    return draft_answer


def final_goal_verification(base_url, final_answer, original_goal, max_tokens, temperature, console):
    """New improvement 3: distinct from self_critique (which checks the
    answer against tool results) — this checks the COMBINED final answer
    against the ORIGINAL goal directly. A per-subtask-correct result can
    still combine into an answer that doesn't actually satisfy what was
    asked; this is the check that catches that specific failure mode,
    run once at the very end rather than per-subtask. Fails open (keeps
    the original answer) if the check itself fails or the goal is empty,
    or if the answer already looks fine."""
    if not original_goal:
        return final_answer

    check_prompt = [{
        "role": "user",
        "content": (
            f"Original goal: {original_goal}\n\n"
            f"Proposed final answer: {final_answer}\n\n"
            "Does this answer actually satisfy the original goal in "
            "full, not just address it partially? Reply with exactly OK "
            "if yes. If it misses something the goal asked for, reply "
            "with what's missing in one short sentence."
        ),
    }]
    try:
        msg = chat_completion(base_url, check_prompt, min(max_tokens, 80), 0.0)
        verdict = (msg.get("content") or "").strip()
    except requests.exceptions.RequestException:
        return final_answer

    if verdict.upper().startswith("OK") or not verdict:
        return final_answer

    console.print(f"· final check: answer may not fully address the goal — {verdict}", style=LOG_COLOR_WARN)
    return final_answer + f"\n\n*(Note: this may not fully address the request — {verdict})*"


# ── Session persistence ────────────────────────────────────────────────────
def save_session(messages, name=None):
    name = name or datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(SESSION_DIR, f"{name}.json")
    with open(path, "w") as f:
        json.dump(messages, f, indent=2)
    return path


def load_session(name):
    path = os.path.join(SESSION_DIR, f"{name}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def list_sessions():
    return sorted(f[:-5] for f in os.listdir(SESSION_DIR) if f.endswith(".json"))


LOGO = ["N C O D E R"]
LOGO_WIDTH = len(LOGO[0])  # 11 cols — plain ASCII, guaranteed single-width per char

# Claude Code's actual startup mascot, reproduced exactly (3 lines, ~9 cols).
# Note: unlike the wordmark below, this uses Unicode block-drawing glyphs
# (▐▛███▜▌ etc.), which is the same character class that caused the
# earlier full block-letter logo to misalign on some Android terminal
# fonts. It's small enough here that the risk is low, but if it renders
# oddly on your device, that's a font issue with these specific glyphs,
# not a bug in the alignment logic (which is unrelated and still exact).
MASCOT = [
    "  ▐▛███▜▌ ",
    " ▜█████▛▘ ",
    "  ▘▘ ▝▝   ",
]

# Gemini CLI's actual default gradient runs blue → purple (taken from its
# shipped theme: GradientColors #4796E4 → #847ACE). Extended to white here
# per spec (blue/purple → white) rather than Gemini's own reddish tail.
GRADIENT = ["#4796E4", "#847ACE", "#FFFFFF"]
# Claude Code's real brand accent is a rust/terracotta orange, not purple —
# used here for the border and prompt to match Claude Code's actual look.
BORDER_COLOR = "#E5484D"

# Log-line color palette (TUI improvement 7): distinguishes the category
# of an orchestration log line at a glance during a long/autonomous run,
# instead of every line being uniform dim gray. Chosen to stay within the
# existing theme rather than introducing unrelated colors.
LOG_COLOR_PLAN = "#847ACE"      # plan text — same purple as the "thinking" spinner
LOG_COLOR_ACTION = "#3DDC97"    # tool calls, subtask push/pop, auto-turn progress
LOG_COLOR_REFLECT = "#E5C07B"   # mid-task reflection / caution checks
LOG_COLOR_WARN = BORDER_COLOR   # stalls, failures, safety caps, needs-input — same red as the border
LOG_COLOR_INFO = "dim"          # low-priority bookkeeping: classification, compaction, checkpoints

# New improvements 5 + 7: phase-specific status indicators. Previously,
# the plan/decision/reflection/critique calls in the orchestration loop
# showed NO indicator at all while blocking on the network — only the
# final-answer generation had a spinner. Every phase now gets a
# distinctly colored spinner (reusing the existing log-color palette so
# it stays visually consistent with the log lines already printed after
# each phase completes) plus a small persistent stats footer beneath it.
PHASE_STYLES = {
    "classifying": LOG_COLOR_INFO,
    "planning": LOG_COLOR_PLAN,
    "deciding": LOG_COLOR_PLAN,
    "running": LOG_COLOR_ACTION,
    "reflecting": LOG_COLOR_REFLECT,
    "verifying": LOG_COLOR_REFLECT,
    "checking consistency": LOG_COLOR_WARN,
    "thinking": "#847ACE",
}


class RunStatus:
    """Carries the persistent stats shown in the footer under each phase
    spinner: round/budget progress, complexity tier, and (when relevant)
    which /auto turn this is. A plain small object rather than a dict for
    slightly cheaper attribute access across many phase-spinner calls per
    round."""

    def __init__(self, complexity, max_rounds, time_budget_seconds, start_time):
        self.complexity = complexity
        self.max_rounds = max_rounds
        self.time_budget_seconds = time_budget_seconds
        self.start_time = start_time
        self.round_i = 0
        self.auto_turn = None

    def footer_text(self):
        elapsed = time.time() - self.start_time
        parts = [
            f"round {self.round_i + 1}/{self.max_rounds}",
            f"{elapsed:.0f}s/{self.time_budget_seconds}s",
            f"tier: {self.complexity}",
        ]
        if self.auto_turn is not None:
            parts.append(f"auto-turn {self.auto_turn}")
        return " · ".join(parts)


def with_phase(console, status, phase_label, fn, *args, **kwargs):
    """Wraps any blocking call with a phase-colored spinner plus a
    persistent stats footer, instead of no indicator (previously the
    case for plan/decision/reflection/critique/classification calls) or
    one generic undifferentiated spinner. Note: each call opens its own
    transient Live block, so the footer is visible *during* each phase
    rather than permanently pinned across the whole round — a full
    always-on footer surviving interleaved log lines would need a
    persistent split-screen layout, which carries real risk of breaking
    Termux scrollback compatibility for a much smaller marginal benefit."""
    style = PHASE_STYLES.get(phase_label)
    if style is None:
        # phase_label may include dynamic detail (e.g. "running web_search")
        # not present as an exact key — match on the leading word instead.
        style = PHASE_STYLES.get(phase_label.split()[0], "dim")
    spinner = Spinner("dots", text=f" {phase_label}...", style=style)
    footer = Text(f"  {status.footer_text()}" if status else "", style="dim italic")
    group = Group(spinner, footer) if status else spinner
    with Live(group, console=console, refresh_per_second=8, transient=True):
        return fn(*args, **kwargs)



def gradient_text(line, gradient):
    t = Text()
    n = max(len(line) - 1, 1)
    for i, ch in enumerate(line):
        pos = i / n
        idx = pos * (len(gradient) - 1)
        lo = int(idx)
        hi = min(lo + 1, len(gradient) - 1)
        frac = idx - lo
        c1 = gradient[lo].lstrip("#")
        c2 = gradient[hi].lstrip("#")
        r = int(int(c1[0:2], 16) * (1 - frac) + int(c2[0:2], 16) * frac)
        g = int(int(c1[2:4], 16) * (1 - frac) + int(c2[2:4], 16) * frac)
        b = int(int(c1[4:6], 16) * (1 - frac) + int(c2[4:6], 16) * frac)
        t.append(ch, style=f"bold #{r:02x}{g:02x}{b:02x}")
    return t


def gradient_rule(width, gradient):
    return gradient_text("─" * width, gradient)


def render_header(base_url, ctx_size):
    mascot = Group(*[Text(line, style=f"bold {BORDER_COLOR}") for line in MASCOT])
    wordmark = gradient_text(" ".join("NCODER"), GRADIENT)
    rule_width = LOGO_WIDTH + 4  # a bit wider than the letters for visual weight
    top_rule = gradient_rule(rule_width, GRADIENT)
    bottom_rule = gradient_rule(rule_width, GRADIENT)

    info = Text.from_markup(
        f"[dim]{MODEL_LABEL}[/dim]\n"
        f"[dim]context[/dim]  [white]{ctx_size} tokens[/white]  "
        f"[dim]· v{VERSION}[/dim]"
    )
    body = Group(mascot, Text(""), top_rule, wordmark, bottom_rule, Text(""), info)

    # Panel sizes itself to content (LOGO_WIDTH + padding), never to the
    # terminal width — so it renders identically narrow or wide, and never
    # wraps, so long as the terminal is at least ~34 columns (true even for
    # split-screen Termux on small phones).
    console.print(Panel(body, border_style=f"bold {BORDER_COLOR}", padding=(1, 2), expand=False))
    console.print(
        Text.from_markup(
            "[dim]›[/dim] type your message   "
            "[dim]/help[/dim] commands   "
            "[dim]/quit[/dim] exit\n"
            "[dim]tools: search, http, files, python, notes, termux — used automatically when needed[/dim]"
        )
    )
    console.print()


def _detect_ram_mb():
    """Reads total RAM directly from /proc/meminfo — same technique
    setup.sh already uses for its own RAM-tiered context sizing, kept
    consistent here purely for informational display (the CLI doesn't
    control context size itself; setup.sh already decided that before
    the server ever started)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return kb // 1024
    except Exception:
        pass
    return None


def _ram_tier_label(ram_mb):
    """Mirrors setup.sh's pick_context_size tiering, purely for display —
    reports what context size this device's RAM would map to, without
    the CLI itself making any decision (that already happened when the
    server was launched)."""
    if ram_mb is None:
        return "unknown"
    if ram_mb >= 7000:
        return f"{ram_mb}MB (16384-token tier)"
    elif ram_mb >= 5500:
        return f"{ram_mb}MB (10240-token tier)"
    else:
        return f"{ram_mb}MB (6144-token tier — 4GB minimum)"


def print_startup_summary(console):
    """TUI improvement 6: a compact, glanceable "what is this session
    actually configured to do" view, assembled entirely from state the
    harness already collects but never displays together — RAM tier,
    active complexity budgets, Termux:API availability, and how many
    playbooks have been cached from prior successful tasks."""
    ram_mb = _detect_ram_mb()
    ram_label = _ram_tier_label(ram_mb)

    termux_api_available = shutil.which("termux-notification") is not None

    try:
        playbook_count = len(_load_playbooks())
    except Exception:
        playbook_count = 0

    budget_lines = "\n".join(
        f"    {tier}: {rounds} rounds / {secs}s"
        for tier, (rounds, secs) in COMPLEXITY_BUDGETS.items()
    )

    summary = (
        f"[dim]RAM[/dim]        {ram_label}\n"
        f"[dim]Termux:API[/dim] {'available' if termux_api_available else 'not installed (notifications/clipboard disabled)'}\n"
        f"[dim]Playbooks[/dim]  {playbook_count} cached from prior successful tasks\n"
        f"[dim]Budgets[/dim]\n{budget_lines}"
    )
    console.print(Panel(Text.from_markup(summary), title="session config", border_style="dim", expand=False))
    console.print()


def render_help():
    rows = [
        ("/help", "show this help"),
        ("/reset", "clear conversation, keep system prompt"),
        ("/system <prompt>", "replace the system prompt"),
        ("/save [name]", "save current conversation to disk"),
        ("/load <name>", "load a saved conversation"),
        ("/sessions", "list saved conversations"),
        ("/stats", "show last response's speed stats"),
        ("/auto <goal>", "run autonomously across multiple turns until done or blocked"),
        ("/regenerate", "re-run the last prompt for a fresh answer"),
        ("/resume", "show the last interrupted task checkpoint, if any"),
        ("/undo", "undo the last checkpointed tool-use step"),
        ("/quit", "exit"),
    ]
    text = "\n".join(f"  [cyan]{cmd:<18}[/cyan] {desc}" for cmd, desc in rows)
    console.print(Panel(text, title="commands", border_style="dim", expand=False))


def status_bar(stats):
    if not stats:
        return Text("")
    return Text(
        f"  {stats['tokens']} tokens · {stats['tok_s']:.1f} tok/s · "
        f"first token in {stats['ttft']:.2f}s",
        style="dim italic",
    )


def estimate_context_usage(messages, ctx_size):
    """Rough client-side estimate (~4 chars/token heuristic) since we don't
    have direct access to the server's tokenizer. Good enough for a usage
    hint, not exact — labeled with '~' to signal that."""
    total_chars = sum(len(m.get("content") or "") for m in messages)
    est_tokens = total_chars // 4
    pct = min(100, int(100 * est_tokens / ctx_size)) if ctx_size else 0
    return est_tokens, pct


def run_selftest(base_url):
    """Exercises the real stack end-to-end: server health, a basic chat
    completion, and each of the six tools — directly, not through the
    model's own judgment, so this checks the plumbing independent of
    whether the 3B model would have chosen to call them. Returns True if
    everything passed."""
    results = []

    def check(name, fn):
        try:
            fn()
            results.append((name, True, ""))
        except Exception as e:
            results.append((name, False, str(e)))

    def t_health():
        r = requests.get(f"{base_url}/health", timeout=5)
        assert r.status_code == 200, f"status {r.status_code}"

    def t_chat():
        msg = chat_completion(base_url, [{"role": "user", "content": "Say OK."}], 20, 0.0)
        assert msg.get("content"), "empty response"

    def t_chat_template_sanity():
        """New improvement 1: llama.cpp uses whichever chat template is
        embedded in the GGUF file (baked in during conversion) unless
        explicitly overridden — this project doesn't control that, but
        can at least check for the visible symptom of a wrong/generic
        template: raw special-token markers leaking into the rendered
        response text instead of being consumed by the template. This
        can't verify the template matches Nanbeige's exact expected
        format (that would require the literal template string, which
        isn't reliably fetchable at setup time), but it catches the
        most common failure signature."""
        msg = chat_completion(base_url, [{"role": "user", "content": "Say hello in one word."}], 20, 0.0)
        content = msg.get("content", "") or ""
        suspicious = ["<|im_start|>", "<|im_end|>", "[INST]", "[/INST]", "<s>", "</s>",
                      "<|endoftext|>", "<|assistant|>", "<|user|>", "<|system|>"]
        leaked = [m for m in suspicious if m in content]
        assert not leaked, f"possible chat-template leakage: found {leaked} in response — check the GGUF's embedded chat_template"

    def t_read_write():
        write_file("selftest.txt", "hello from selftest")
        content = read_file("selftest.txt")
        assert "hello from selftest" in content, "roundtrip mismatch"

    def t_list_dir():
        out = list_directory("")
        assert isinstance(out, str), "unexpected type"

    def t_run_python():
        out = run_python("print(1+1)")
        assert "2" in out, f"unexpected output: {out}"

    def t_search_notes():
        out = search_notes("selftest")
        assert "selftest" in out.lower() or "no matches" in out.lower(), out

    def t_termux_api():
        # Graceful-failure is a pass here — the check is that it doesn't
        # crash the process, not that Termux:API is installed.
        out = termux_api("clipboard_get")
        assert isinstance(out, str)

    def t_grammar():
        grammar = build_tool_decision_grammar(TOOLS)
        assert "root ::=" in grammar, "grammar missing root rule"

    def t_facts_ledger():
        record_fact("selftest fact check", source="selftest", confidence="high")
        out = query_facts("selftest fact")
        assert "selftest fact check" in out, out

    def t_todo_parser():
        total, unchecked, checked = parse_todo("- [ ] a\n- [x] b\n")
        assert total == 2 and len(unchecked) == 1 and len(checked) == 1

    def t_cycle_detection():
        pending = {"B": {"goal": "b", "depends_on": ["A"]}}
        assert _creates_cycle("A", ["B"], pending) is True
        assert _creates_cycle("C", ["A"], pending) is False

    def t_playbook_matching():
        save_playbook("selftest playbook goal example", ["web_search"], complexity="moderate")
        match = find_playbook_match("selftest playbook goal example variant")
        assert match is not None and match[0] == "moderate", match

    def t_verification_tiers():
        assert VERIFICATION_DEPTH["simple"] == "none"
        assert VERIFICATION_DEPTH["moderate"] == "light"
        assert VERIFICATION_DEPTH["complex"] == "full"

    check("server /health", t_health)
    check("chat completion", t_chat)
    check("chat template sanity (no token leakage)", t_chat_template_sanity)
    check("read_file / write_file", t_read_write)
    check("list_directory", t_list_dir)
    check("run_python", t_run_python)
    check("search_notes", t_search_notes)
    check("termux_api (graceful if unavailable)", t_termux_api)
    check("tool-decision grammar builds", t_grammar)
    check("fact ledger (record/query)", t_facts_ledger)
    check("todo.md checklist parsing", t_todo_parser)
    check("subtask cycle detection", t_cycle_detection)
    check("playbook-informed classification", t_playbook_matching)
    check("tiered verification depth", t_verification_tiers)

    console.print(Panel.fit("Self-test results", border_style=BORDER_COLOR))
    all_pass = True
    for name, passed, err in results:
        mark = "[green]PASS[/green]" if passed else "[red]FAIL[/red]"
        console.print(f"  {mark}  {name}" + (f"  [dim]({err})[/dim]" if err else ""))
        all_pass = all_pass and passed

    return all_pass


def stream_and_finalize(base_url, working, messages, max_tokens, temperature, ctx_size, console, tools_ran, original_goal=None, complexity="moderate"):
    """Shared tail used by both the normal per-message flow and /auto:
    streams the final answer, runs self-critique and whole-answer
    verification only if tools actually ran this turn, clears the
    checkpoint on a normal completion, prints the context-usage line,
    and appends the answer to the running conversation. `tools_ran` is
    passed explicitly rather than inferred from list-length comparison,
    since /auto's multi-turn loop already folds `working` back into
    `messages` between turns, which would make a length-based inference
    silently wrong there.

    New improvement 6: `complexity` gates whether the extra verification
    passes (self-critique, whole-answer check) run at all. Nanbeige4.1-3B
    benchmarks strongly on native tool-use judgment for straightforward
    cases (BFCL-V4/Tau2-Bench) — the heavy scaffolding is more valuable
    for genuinely multi-step work than for a "simple"-tier quick lookup,
    so simple-tier turns skip both extra checks entirely rather than
    paying their latency cost uniformly regardless of task difficulty."""
    console.print(Rule(style="dim"))
    full_reply = ""
    stats = None
    spinner = Spinner("dots", text=" thinking...", style="#847ACE")

    # New improvement 1: re-rendering Markdown(full_reply) on every single
    # token is O(n) work per token — O(n²) total for a long reply — real
    # CPU competition with the LLM itself on a slow phone. Buffer content
    # until a word boundary (whitespace) or ~100ms has passed, whichever
    # comes first, so re-renders scale with word count rather than token
    # count without adding any perceptible latency to what's on screen.
    pending = ""
    last_render = time.time()
    RENDER_INTERVAL = 0.1  # seconds

    with Live(spinner, console=console, refresh_per_second=12, transient=True) as live:
        for content, maybe_stats in stream_chat(base_url, working, max_tokens, temperature):
            if maybe_stats is not None:
                stats = maybe_stats
                break
            full_reply += content
            pending += content
            now = time.time()
            if " " in pending or "\n" in pending or (now - last_render) >= RENDER_INTERVAL:
                live.update(Markdown(full_reply))
                pending = ""
                last_render = now
        if pending:
            live.update(Markdown(full_reply))  # flush any trailing partial word

    console.print(Markdown(full_reply))

    # Refinement 5: verification depth scaled per tier — "none" skips
    # both passes (simple), "light" runs self-critique only (moderate),
    # "full" runs both (complex/autonomous). See VERIFICATION_DEPTH.
    depth = VERIFICATION_DEPTH.get(complexity, "full")
    answer_was_revised = False  # TUI improvement 3: tracked for the badge below
    if tools_ran and depth != "none":
        revised = with_phase(
            console, None, "verifying",
            self_critique, base_url, working, full_reply, max_tokens, temperature, console,
        )
        if revised != full_reply:
            full_reply = revised
            answer_was_revised = True
            console.print(Markdown(full_reply))

        # New improvement 3: whole-answer re-verification against the
        # ORIGINAL goal, distinct from self_critique's tool-results check
        # above — a per-subtask-correct result can still combine into an
        # answer that doesn't fully satisfy what was actually asked.
        # Reserved for "full" depth only — see VERIFICATION_DEPTH.
        if depth == "full" and original_goal:
            verified_reply = with_phase(
                console, None, "verifying",
                final_goal_verification, base_url, full_reply, original_goal, max_tokens, temperature, console,
            )
            if verified_reply != full_reply:
                full_reply = verified_reply
                answer_was_revised = True
                console.print(Markdown(full_reply))

    if stats:
        console.print(status_bar(stats))

    if tools_ran:
        # Task finished normally — the checkpoint would only be useful
        # for resuming an interrupted run, so clear it rather than
        # leaving a stale "in progress" file behind.
        clear_checkpoint()

    # TUI improvement 3: a small trailing badge showing how much
    # scaffolding actually ran on THIS specific answer — complexity tier,
    # verification depth applied, and whether either check changed the
    # answer — rather than needing to scroll back through the phase-log
    # to piece that together. Built entirely from state already tracked
    # above (depth, tools_ran, answer_was_revised), not a fabricated
    # confidence score — this project doesn't compute a single top-level
    # confidence for the final answer the way pop_subtask does per
    # subtask, so the badge reports only what's actually known.
    depth_label = {"none": "no verification", "light": "self-check only", "full": "full verification"}.get(depth, depth)
    if not tools_ran:
        badge_text = f"[{complexity} tier · no tools used]"
    else:
        badge_text = f"[{complexity} tier · {depth_label} · {'revised' if answer_was_revised else 'unchanged'}]"
    console.print(Text(badge_text, style="dim italic"))

    _, pct = estimate_context_usage(messages, ctx_size)
    console.print(Text(f"  ~context used: {pct}%", style="dim italic"))
    console.print()

    messages.append({"role": "assistant", "content": full_reply})
    return full_reply, stats


def main():
    args = parse_args()
    base_url = f"http://{args.host}:{args.port}"

    if args.selftest:
        console.print("[dim]connecting to local model server...[/dim]")
        if not wait_for_server(base_url):
            console.print(f"[bold red]Cannot reach llama-server at {base_url}.[/bold red]")
            sys.exit(1)
        passed = run_selftest(base_url)
        sys.exit(0 if passed else 1)

    console.print("[dim]connecting to local model server...[/dim]")
    if not wait_for_server(base_url):
        console.print(
            f"[bold red]Cannot reach llama-server at {base_url}.[/bold red] "
            "Is it running? (setup.sh normally starts it for you.)"
        )
        sys.exit(1)

    props = get_server_props(base_url)
    ctx_size = props.get("default_generation_settings", {}).get("n_ctx", args.ctx_size)

    console.clear()
    render_header(base_url, ctx_size)
    print_startup_summary(console)

    messages = [{"role": "system", "content": DEFAULT_SYSTEM_PROMPT + FEW_SHOT_EXAMPLE}]
    session = PromptSession(history=FileHistory(HISTORY_FILE), style=PT_STYLE)
    last_stats = None
    last_user_input = None

    def generate_reply():
        """Runs the tool pre-pass (model decides on its own whether to call
        web_search / http_request), then streams the final answer. Shared
        by normal turns and /regenerate."""
        nonlocal messages, last_stats
        current_goal = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )
        pre_len = len(messages)
        working, halt_signal, complexity = run_turn_with_tools(
            base_url, messages, args.max_tokens, args.temperature, console,
            original_goal=current_goal,
        )
        if halt_signal and halt_signal["type"] == "needs_user_input":
            console.print(Panel(
                halt_signal["reason"], title="needs your input", border_style=BORDER_COLOR,
            ))
            notify_completion("NCoder needs input", halt_signal["reason"][:200])

        tools_ran = len(working) > pre_len
        full_reply, stats = stream_and_finalize(
            base_url, working, messages, args.max_tokens, args.temperature,
            ctx_size, console, tools_ran, original_goal=current_goal, complexity=complexity,
        )
        if stats:
            last_stats = stats

    while True:
        try:
            user_input = session.prompt([("class:prompt", "› ")]).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]exiting[/dim]")
            break

        if not user_input:
            continue

        if user_input == "/quit":
            break
        elif user_input == "/help":
            render_help()
            continue
        elif user_input == "/reset":
            messages = [{"role": "system", "content": DEFAULT_SYSTEM_PROMPT + FEW_SHOT_EXAMPLE}]
            last_user_input = None
            console.print("[dim]conversation reset[/dim]\n")
            continue
        elif user_input == "/stats":
            console.print(status_bar(last_stats) if last_stats else "[dim]no stats yet[/dim]")
            continue
        elif user_input == "/sessions":
            names = list_sessions()
            if not names:
                console.print("[dim]no saved sessions[/dim]")
            else:
                console.print(Panel("\n".join(names), title="saved sessions", border_style="dim"))
            continue
        elif user_input.startswith("/auto "):
            goal = user_input[len("/auto "):].strip()
            if not goal:
                console.print("[dim]usage: /auto <goal>[/dim]\n")
                continue

            # New improvement 4: check decomposability before burning
            # rounds discovering mid-task that the goal was too vague to
            # ever have made a todo list from.
            decomposable, vague_reason = with_phase(
                console, None, "classifying", check_decomposable, base_url, goal, args.max_tokens,
            )
            if not decomposable:
                console.print(Panel(
                    f"This goal may be too vague to run autonomously: {vague_reason}\n\n"
                    "Try /auto again with more specifics, or just chat normally to clarify first.",
                    title="needs more detail before starting", border_style=BORDER_COLOR,
                ))
                continue

            console.print(Panel(
                goal, title="starting autonomous mode", border_style=BORDER_COLOR,
            ))
            pre_auto_len = len(messages)
            messages.append({"role": "user", "content": goal})

            MAX_AUTO_TURNS = 8  # hard safety cap regardless of todo.md state
            final_halt = None
            auto_succeeded = False  # new improvement 6: only cache a playbook on genuine completion

            for auto_turn in range(MAX_AUTO_TURNS):
                working, halt_signal, _complexity = run_turn_with_tools(
                    base_url, messages, args.max_tokens, args.temperature, console,
                    original_goal=goal, complexity_override="autonomous",
                )
                messages = working

                if halt_signal and halt_signal["type"] == "needs_user_input":
                    final_halt = halt_signal
                    break

                # New improvement 1: keep going across turns without
                # waiting for the user, as long as todo.md still has
                # unchecked items and nothing has signaled it's blocked —
                # this is what actually reduces check-ins, versus just
                # raising the round budget within a single turn.
                todo_content = read_file(TODO_FILE)
                total_items, unchecked_items, checked_items = parse_todo(todo_content)
                has_unchecked = len(unchecked_items) > 0
                if not has_unchecked:
                    auto_succeeded = True
                    if total_items:  # only show the panel if a todo.md was actually used
                        console.print(render_todo_panel(todo_content, LOG_COLOR_ACTION))
                    break

                console.print(f"· auto-turn {auto_turn + 1}: {len(unchecked_items)}/{total_items} todo items remaining, continuing", style=LOG_COLOR_ACTION)
                console.print(render_todo_panel(todo_content, LOG_COLOR_ACTION))
                messages.append({
                    "role": "user",
                    "content": "Continue working through your todo list without stopping to check in.",
                })
            else:
                console.print(f"· reached the {MAX_AUTO_TURNS}-turn auto-mode safety cap, wrapping up", style=LOG_COLOR_WARN)

            if final_halt:
                console.print(Panel(
                    final_halt["reason"], title="needs your input", border_style=BORDER_COLOR,
                ))
                notify_completion("NCoder needs input", final_halt["reason"][:200])
            else:
                notify_completion("NCoder finished", f"/auto on: {goal[:150]}")

            if auto_succeeded:
                save_playbook(goal, extract_tool_sequence(messages[pre_auto_len:]), complexity="autonomous")

            tools_ran = len(messages) > pre_auto_len + 1  # +1 for the goal message itself
            full_reply, stats = stream_and_finalize(
                base_url, messages, messages, args.max_tokens, args.temperature,
                ctx_size, console, tools_ran, original_goal=goal, complexity="autonomous",
            )
            if stats:
                last_stats = stats
            continue
        elif user_input == "/regenerate":
            if last_user_input is None:
                console.print("[dim]nothing to regenerate yet[/dim]\n")
                continue
            # Drop the previous assistant reply, keep the same user turn,
            # ask again — useful when a response was cut short or off-base.
            if messages and messages[-1]["role"] == "assistant":
                messages.pop()
            generate_reply()
            continue
        elif user_input == "/resume":
            checkpoint = load_checkpoint()
            if checkpoint is None or not checkpoint.get("diffs"):
                console.print("[dim]no interrupted task checkpoint found[/dim]\n")
            else:
                full = reconstruct_from_checkpoint(checkpoint)
                last_round = checkpoint["diffs"][-1].get("round", "?")
                last_ts = checkpoint["diffs"][-1].get("timestamp", "?")
                console.print(Panel(
                    f"Round {last_round} · saved {last_ts}\n"
                    f"{len(full)} messages in progress ({len(checkpoint['diffs'])} rounds recorded)",
                    title="found a checkpoint", border_style="dim",
                ))
                messages = full
                console.print("[dim]loaded — continue the conversation to pick up where it left off[/dim]\n")
            continue
        elif user_input == "/undo":
            restored = undo_last_step()
            if restored is None:
                console.print("[dim]nothing to undo — no checkpointed steps found[/dim]\n")
            else:
                messages = restored
                console.print(f"[dim]undid the last checkpointed step — {len(messages)} messages remain[/dim]\n")
            continue
        elif user_input.startswith("/system "):
            new_sys = user_input[len("/system "):].strip()
            messages[0] = {"role": "system", "content": new_sys}
            console.print("[dim]system prompt updated[/dim]\n")
            continue
        elif user_input.startswith("/save"):
            parts = user_input.split(maxsplit=1)
            name = parts[1] if len(parts) > 1 else None
            path = save_session(messages, name)
            console.print(f"[dim]saved → {path}[/dim]\n")
            continue
        elif user_input.startswith("/load "):
            name = user_input[len("/load "):].strip()
            loaded = load_session(name)
            if loaded is None:
                console.print(f"[red]no session named '{name}'[/red]\n")
            else:
                messages = loaded
                console.print(f"[dim]loaded '{name}'[/dim]\n")
            continue

        last_user_input = user_input
        messages.append({"role": "user", "content": user_input})
        generate_reply()


if __name__ == "__main__":
    main()
