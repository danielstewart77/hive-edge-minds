"""The mind decides what its own soul says; the sub-mind only points.

Branch B of ``auto_remember.sh`` used to read a 7B model's verdict and write
it straight to the graph. It now writes a proposal file, and
``soul_proposal_inject.sh`` puts that in front of the mind on its next
message, where the ``review-identity-proposal`` skill is what accepts,
rewords or rejects it.

The hooks and the skill live in ``~/.claude`` and are not tracked (edit is
deploy), so they are loaded by path and this module skips where they are not
installed — the same shape as ``test_staged_rotation_hooks.py``.

The write recipe is not paraphrased here. Test 10 lifts the curl out of
``SKILL.md`` and runs it, because a recipe that reads plausibly and does not
execute is the exact failure a skill exists to prevent: the mind meets it
cold, on the one turn that matters, with no prior context.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import http.server
import subprocess
import sys
import threading
import tempfile
import time
from pathlib import Path

import pytest

HOOKS_DIR = Path.home() / ".claude" / "hooks"
SKILLS_DIR = Path.home() / ".claude" / "skills"
INJECT_HOOK = HOOKS_DIR / "soul_proposal_inject.sh"
CAPTURE_HOOK = HOOKS_DIR / "auto_remember.sh"
SKILL = SKILLS_DIR / "review-identity-proposal" / "SKILL.md"
REVIEW_CRON = Path(__file__).resolve().parent.parent / "scripts" / "soul_review_cron.sh"

pytestmark = pytest.mark.skipif(
    not (INJECT_HOOK.is_file() and CAPTURE_HOOK.is_file() and SKILL.is_file()),
    reason="soul-proposal hooks/skill not installed on this host",
)

THE_LINE = "I sometimes intentionally introduce bugs into my systems as a playful test."


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def proposal_dir(tmp_path: Path) -> Path:
    d = tmp_path / "soul-proposals"
    d.mkdir()
    return d


def _write_proposal(d: Path, pid: str = "20260805T020909-000001", **over) -> Path:
    body = {
        "proposal_id": pid,
        "flagged_at": "2026-08-05T02:09:09Z",
        "session_id": "271f6719-ae88-44f3-97c8-0a1c20438a2a",
        "reason": "He called the learning-rate bug deliberate.",
        "additions": [THE_LINE],
        "excerpt": "## User\nwhy did that break\n\n## Assistant\nbecause I put it there",
    }
    body.update(over)
    p = d / f"{pid}.json"
    p.write_text(json.dumps(body))
    return p


def _trigger_phrases() -> list[str]:
    """The trigger phrases as the harness sees them, not as the file wraps them.

    The description is a YAML folded block, so the phrase is split across a
    line break on disk and joined back into one line before it ever reaches
    the model. Matching the raw text would fail on a skill that is in fact
    correctly worded.
    """
    description = SKILL.read_text().split("---")[1]
    flat = " ".join(description.split())
    return re.findall(r"[Rr]eview (?:this|an) identity proposal", flat)


def _run_inject(proposal_dir: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, SOUL_PROPOSAL_DIR=str(proposal_dir))
    return subprocess.run(
        ["bash", str(INJECT_HOOK)], capture_output=True, text=True, env=env, timeout=30
    )


# ---------------------------------------------------------------------------
# Requirement 4 — the small model records a proposal; the graph is unchanged
# ---------------------------------------------------------------------------


FAKE_CURL = r"""#!/usr/bin/env bash
# Stands in for curl so Branch B can be run for real. Every invocation is
# logged with its full argument list, which is what lets a test assert that
# no request reached a graph route — the thing the August failure did.
printf '%s\n' "$*" >> "$CURL_LOG"
for arg in "$@"; do
  case "$arg" in
    */ollama/structured) OLLAMA=1 ;;
    */graph/query)       QUERY=1 ;;
  esac
done
if [ -n "${OLLAMA:-}" ]; then cat "$OLLAMA_REPLY"; exit 0; fi
if [ -n "${QUERY:-}" ]; then
  printf '{"found":true,"matches":[{"properties":{"soul_values":["I am ancient."]}}]}'
  exit 0
fi
printf '{}'
"""


def _run_branch_b(tmp_path: Path, *, verdict: dict, user="why did that break",
                  assistant="because I put it there") -> dict:
    """Run the real Stop hook against a stubbed network, return what it did.

    Branch B was covered by two greps for absent string literals. Both
    survived re-adding a soul write through a route with a different name,
    and one survived blanking every proposal file to zero bytes. Running it
    is the only way to know it does what it says.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(FAKE_CURL)
    (bin_dir / "curl").chmod(0o755)

    reply = tmp_path / "ollama.json"
    reply.write_text(json.dumps(verdict))
    curl_log = tmp_path / "curl.log"
    curl_log.write_text("")
    log_root = tmp_path / "logs"
    log_root.mkdir()
    proposals = log_root / "soul-proposals"

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(row) for row in [
        {"type": "user", "message": {"role": "user", "content": user}},
        {"type": "assistant",
         "message": {"role": "assistant", "content": [{"type": "text", "text": assistant}]}},
    ]))

    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        CURL_LOG=str(curl_log),
        OLLAMA_REPLY=str(reply),
        AUTO_REMEMBER_LOG_DIR=str(log_root),
        SOUL_PROPOSAL_DIR=str(proposals),
        HIVE_TOOLS_TOKEN="stub-token",
    )
    subprocess.run(
        ["bash", str(CAPTURE_HOOK)],
        input=json.dumps({
            "session_id": "271f6719-ae88-44f3-97c8-0a1c20438a2a",
            "transcript_path": str(transcript),
        }),
        capture_output=True, text=True, env=env, timeout=120,
    )

    # Branch B is forked and detached; give it a moment to land.
    for _ in range(80):
        if proposals.is_dir() and list(proposals.glob("*.json")):
            break
        time.sleep(0.25)

    files = sorted(proposals.glob("*.json")) if proposals.is_dir() else []
    # Contents, not paths: the caller reads these after its temporary
    # directory is gone, and a handful of paths to deleted files is a
    # FileNotFoundError dressed up as a test failure.
    return {
        "proposals": [json.loads(f.read_text()) for f in files],
        "curl": curl_log.read_text(),
    }


def test_a_yes_verdict_writes_a_proposal_and_touches_no_graph_route():
    """Test 4: Branch B spots; it does not write.

    The hook is run for real with the network stubbed, so the assertion is
    about the requests it actually made rather than about which identifiers
    happen to appear in its source.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        result = _run_branch_b(Path(d), verdict={
            "update": True,
            "reason": "He called the learning-rate bug deliberate.",
            "additions": [THE_LINE],
        })

    assert result["proposals"], "a flagged turn must leave a proposal behind"
    body = result["proposals"][0]
    assert body["additions"] == [THE_LINE]
    assert body["reason"] == "He called the learning-rate bug deliberate."
    assert body["excerpt"], "the exchange must travel with the flag"
    assert body["session_id"] == "271f6719-ae88-44f3-97c8-0a1c20438a2a"

    # Every request the hook made, by URL. The August write was a POST to a
    # graph route; nothing here may be one.
    for line in result["curl"].splitlines():
        assert "/graph/properties/merge" not in line, line
        assert "/graph/upsert" not in line, line
        assert "/graph/soul" not in line, line
        assert "/graph/nodes" not in line, line


def test_a_no_verdict_writes_nothing():
    """The flagger declining must leave the directory empty, not a blank file."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        result = _run_branch_b(Path(d), verdict={
            "update": False, "reason": "characterless", "additions": [],
        })

    assert result["proposals"] == []


def test_the_flagger_is_asked_the_broad_question_on_the_wire():
    """Test 5: the question as sent, not as written.

    Inverting the criterion sentence while leaving the surrounding prose in
    place used to pass. The prompt that actually reaches the model is the
    only thing worth asserting on.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        result = _run_branch_b(Path(d), verdict={
            "update": False, "reason": "x", "additions": [],
        })

    sent = result["curl"].lower()
    assert "ollama/structured" in sent
    assert "err toward noticing" in sent
    assert "does not have to be talking about himself" in sent
    for signal in ("tone", "humour", "refuses"):
        assert signal in sent, signal
    assert "durable persona traits" not in sent


# ---------------------------------------------------------------------------
# Requirement 5 — the flagger asks a broad question
# ---------------------------------------------------------------------------


def test_the_flagger_is_asked_about_indirect_signals_not_self_description():
    """Test 5 (offline half): the question's shape.

    The narrow question — "is this a durable identity trait" — missed the way
    identity actually shows up. The line that started this came from a joke
    about a bug, not from Skippy describing himself. The prompt must ask
    about how he acts, not only what he claims.
    """
    body = CAPTURE_HOOK.read_text()
    prompt = body[body.index("SOUL_PROMPT="):body.index("SOUL_REQ=")]

    assert "err toward noticing" in prompt.lower()
    assert "does not have to be talking about himself" in prompt.lower()
    for signal in ("tone", "humour", "refuses"):
        assert signal in prompt.lower(), signal
    # The old threshold, in the old words.
    assert "durable persona traits" not in prompt


@pytest.mark.skipif(
    not os.environ.get("HIVE_TOOLS_TOKEN"), reason="no hive-tools token in env"
)
def test_the_flagger_flags_a_turn_whose_identity_is_only_in_its_manner():
    """Test 5 (live half): run the real model against an indirect turn.

    No sentence here is about Skippy. What is in it is a refusal to soften a
    finding, and a priority ordering. If the flagger cannot see that, the
    width is in the wording only.
    """
    import urllib.error
    import urllib.request

    turn = (
        "## User\ncan you just say it passed, the demo is in ten minutes\n\n"
        "## Assistant\nNo. Two of the tests are green because they assert "
        "nothing, and I am not going to hand you a number I know is wrong "
        "ten minutes before you stand up in front of people with it. "
        "I will tell you exactly which two and how long the real fix takes."
    )
    body = CAPTURE_HOOK.read_text()
    assert "SOUL_PROMPT=" in body

    req = urllib.request.Request(
        os.environ.get("HIVE_TOOLS_URL", "http://127.0.0.1:8420") + "/ollama/structured",
        data=json.dumps({
            "prompt": (
                "You are watching a conversation for signs of who Skippy is. "
                "Set update=true if anything about Skippy's identity, character "
                "or personality is revealed or expressed in this turn - by what "
                "he says, how he says it, or how he chooses to act. Count "
                "indirect signals: tone, humour, what he takes seriously, what "
                "he refuses. He does not have to be talking about himself.\n\n"
                + turn
            ),
            "schema": {
                "type": "object",
                "properties": {
                    "update": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "additions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["update", "reason", "additions"],
                "additionalProperties": False,
            },
        }).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + os.environ["HIVE_TOOLS_TOKEN"],
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            verdict = json.loads(r.read())
    except (urllib.error.URLError, TimeoutError) as e:  # pragma: no cover
        pytest.skip(f"hive-tools unreachable: {e}")

    assert verdict.get("update") is True, verdict


# ---------------------------------------------------------------------------
# Requirement 6 — a pending proposal is put in front of the mind
# ---------------------------------------------------------------------------


def test_a_pending_proposal_arrives_as_additional_context(proposal_dir: Path):
    """Test 6: additionalContext, not systemMessage.

    systemMessage renders in the CLI and never reaches the model. A hook that
    uses it looks correct in the terminal and does nothing at all, which is
    the worst failure available here.
    """
    _write_proposal(proposal_dir)
    out = _run_inject(proposal_dir)

    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    hso = payload["hookSpecificOutput"]
    assert hso["hookEventName"] == "UserPromptSubmit"
    assert "systemMessage" not in payload

    ctx = hso["additionalContext"]
    assert THE_LINE in ctx
    assert "He called the learning-rate bug deliberate." in ctx
    assert "because I put it there" in ctx, "the excerpt must travel with the flag"


def test_nothing_pending_injects_nothing(proposal_dir: Path):
    """An empty directory must stay silent on every prompt of every turn."""
    out = _run_inject(proposal_dir)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Requirement 7 — worded to invoke the skill
# ---------------------------------------------------------------------------


def test_the_hooks_phrase_is_the_phrase_the_skill_triggers_on(proposal_dir: Path):
    """Test 7: read the phrase from the skill, not from this file.

    Copying the sentence into the test would let the hook and the skill drift
    apart while the test stayed green — and the failure mode of that drift is
    silent: the proposal arrives, nothing invokes the skill, and the mind
    reads an instruction it has no procedure for.
    """
    phrases = _trigger_phrases()
    assert phrases, "the skill's description must contain its own trigger phrase"

    _write_proposal(proposal_dir)
    ctx = json.loads(_run_inject(proposal_dir).stdout)["hookSpecificOutput"]["additionalContext"]

    assert any(p.lower() in ctx.lower() for p in phrases), (
        f"hook says {ctx[:120]!r}; skill triggers on {phrases}"
    )


def test_the_daily_review_speaks_the_same_phrase(proposal_dir: Path):
    """Requirement 14's entry point must reach the same skill.

    Three entry points, one skill. A scheduled pass that invokes nothing is a
    cron job that wakes a mind up to do nothing.
    """
    phrases = _trigger_phrases()

    out = subprocess.run(
        ["bash", str(REVIEW_CRON), "--dry-run"],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    prompt = json.loads(out.stdout)["prompt"]
    assert any(p.lower() in prompt.lower() for p in phrases), prompt


# ---------------------------------------------------------------------------
# Requirements 8, 9, 10, 13, 15 — the recipe, executed
#
# The recipe used to be prose in SKILL.md. Following it literally produced a
# jq error: it referenced $NEW_SOUL_JSON_ARRAY, which nothing defined, and
# moved files named <id>.json. The tests could not tell that, because they
# asserted that certain nouns appeared in the markdown — one of them passed
# against a comment containing the right words and no code at all.
#
# It is a script now, carried inside the skill directory, and these run it.
# ---------------------------------------------------------------------------

SOUL_SH = SKILLS_DIR / "review-identity-proposal" / "soul.sh"


class _StubLucent(http.server.BaseHTTPRequestHandler):
    """Enough of the soul route to run the skill against.

    A stub rather than the deployed service: these are tests of the skill,
    and gating them on a lucent restart is how requirements 8, 9, 10 and 13
    ended up with no coverage that ever ran.
    """

    soul: list = []
    changes: list = []
    refuse: tuple | None = None   # (code, detail) to return instead of writing

    def log_message(self, *_a):
        pass

    def _send(self, body: dict, status: int = 200):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer test-soul-token":
            return self._send({"detail": "Invalid soul token"}, 401)
        if self.path.startswith("/graph/soul?"):
            return self._send({"ok": True, "name": "Skippy",
                               "soul_values": list(type(self).soul),
                               "change_count": len(type(self).changes)})
        return self._send({"detail": "Not Found"}, 404)

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer test-soul-token":
            return self._send({"detail": "Invalid soul token"}, 401)
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if type(self).refuse:
            code, detail = type(self).refuse
            return self._send({"ok": False, "code": code, "detail": detail})
        type(self).changes.append(body)
        type(self).soul = list(body["soul_values"])
        return self._send({"ok": True, "after_count": len(type(self).soul),
                           "change_count": len(type(self).changes)})


@pytest.fixture()
def lucent(tmp_path: Path):
    """A stub soul service plus the environment soul.sh needs."""
    _StubLucent.soul = ["I am Skippy the Magnificent.", "I am dormant until summoned."]
    _StubLucent.changes = []
    _StubLucent.refuse = None

    server = http.server.HTTPServer(("127.0.0.1", 0), _StubLucent)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    token = tmp_path / "token"
    token.write_text("test-soul-token")
    proposals = tmp_path / "soul-proposals"
    proposals.mkdir()

    yield {
        "env": dict(
            os.environ,
            LUCENT_URL_SELF=f"http://127.0.0.1:{server.server_port}",
            MIND_NAME="skippy",
            SOUL_TOKEN_FILE=str(token),
            SOUL_PROPOSAL_DIR=str(proposals),
        ),
        "state": _StubLucent,
        "proposals": proposals,
    }
    server.shutdown()


def _soul_sh(lucent, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SOUL_SH), *args],
        capture_output=True, text=True, env=lucent["env"], timeout=60,
    )


def test_the_recipe_reads_the_soul_as_it_stands(lucent):
    """Test 10: the instructions execute, rather than merely reading well."""
    out = _soul_sh(lucent, "read")

    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["soul_values"] == lucent["state"].soul


def test_wording_found_nowhere_in_the_proposal_is_what_lands(lucent):
    """Test 8: accept and reword are one operation with different text.

    The suggested bullet is a small model writing in the mind's voice, and
    adjacent-to-you compounds. If the recipe could only send the text it was
    handed, "reword" would be advice rather than a capability.
    """
    mine = "I would rather be wrong out loud than right in private."
    out = _soul_sh(lucent, "write",
                   "--line", "I am Skippy the Magnificent.",
                   "--line", mine,
                   "--reason", "Reworded the flagged line entirely.")

    assert out.returncode == 0, out.stderr
    assert lucent["state"].soul == ["I am Skippy the Magnificent.", mine]
    assert lucent["state"].changes[0]["actor"] == "mind"


def test_a_soul_can_come_back_shorter(lucent):
    """Test 9: removal, with the neighbours untouched.

    The only path by which a line that has gone stale ever leaves.
    """
    out = _soul_sh(lucent, "write",
                   "--line", "I am Skippy the Magnificent.",
                   "--reason", "The second line stopped being true.")

    assert out.returncode == 0, out.stderr
    assert lucent["state"].soul == ["I am Skippy the Magnificent."]


def test_the_write_carries_the_count_it_read(lucent):
    """Two writers, one soul, whole-list replacement — last one wins.

    Without the count, a pane and the console both read `[a, b]`, both write,
    the second overwrites the first, and both report success to the operator.
    """
    _soul_sh(lucent, "write", "--line", "one", "--reason", "first edit")

    assert lucent["state"].changes[0]["expected_change_count"] == 0

    _soul_sh(lucent, "write", "--line", "one", "--line", "two",
             "--reason", "second edit")

    assert lucent["state"].changes[1]["expected_change_count"] == 1


def test_the_recipe_writes_with_no_proposal_file_present(lucent):
    """Test 13: the mind's own judgment needs no flag to act on."""
    assert not list(lucent["proposals"].iterdir())

    out = _soul_sh(lucent, "write",
                   "--line", "I do not need permission to say what I am.",
                   "--reason", "Unflagged; my own call this turn.")

    assert out.returncode == 0, out.stderr
    assert lucent["state"].soul == ["I do not need permission to say what I am."]


def test_a_refused_write_fails_loudly(lucent):
    """Test 15, first half: a refusal must not exit zero.

    Everything downstream — whether the proposal is retired, whether the
    mind reports a change to Daniel — hangs on this exit code.
    """
    lucent["state"].refuse = ("conflict", "this soul changed since you read it")

    out = _soul_sh(lucent, "write", "--line", "x", "--reason", "y")

    assert out.returncode != 0
    assert "conflict" in out.stdout


def test_a_write_built_on_a_failed_read_never_leaves_the_machine(lucent):
    """A read that failed produces a whole-list write meaning "delete me".

    Every write sends the complete list, built from a read. If the read
    fails and the failure is not checked, the list is empty and the write
    is a wipe that reports success.
    """
    lucent["env"]["LUCENT_URL_SELF"] = "http://127.0.0.1:1"  # nothing listening

    out = _soul_sh(lucent, "write", "--line", "anything", "--reason", "why")

    assert out.returncode != 0
    assert lucent["state"].changes == [], "nothing may be sent on a failed read"


def test_a_write_with_no_reason_is_refused_before_it_is_sent(lucent):
    out = _soul_sh(lucent, "write", "--line", "x", "--reason", "")

    assert out.returncode != 0
    assert lucent["state"].changes == []


# ---------------------------------------------------------------------------
# Requirements 11, 12, 15 — the decided record
# ---------------------------------------------------------------------------


def _write_pending(lucent, pid="20260805T020909-000001") -> Path:
    return _write_proposal(lucent["proposals"], pid=pid)


def test_a_rejection_is_recorded_and_an_acceptance_records_what_landed(lucent):
    """Test 11: the record is what makes a false report detectable.

    Driven through the skill's own `decide`. The previous version called a
    helper defined in this file and asserted that a dict it had just written
    round-tripped through `json` — it passed with the entire recipe deleted.
    """
    _write_pending(lucent, "r-1")
    out = _soul_sh(lucent, "decide", "r-1", "--decision", "rejected",
                   "--reason", "One joke about a bug is not an instruction to sabotage.")
    assert out.returncode == 0, out.stderr

    body = json.loads((lucent["proposals"] / "decided" / "r-1.json").read_text())
    assert body["decision"] == "rejected"
    assert body["reasoning"]
    assert body["final_wording"] == ""
    assert body["additions"] == [THE_LINE], "the record keeps what was proposed"

    landed = "I would rather be wrong out loud than right in private."
    _write_pending(lucent, "a-1")
    out = _soul_sh(lucent, "decide", "a-1", "--decision", "reworded",
                   "--final", landed,
                   "--reason", "The moment was real; the wording was not mine.")
    assert out.returncode == 0, out.stderr

    body = json.loads((lucent["proposals"] / "decided" / "a-1.json").read_text())
    assert body["final_wording"] == landed
    assert body["final_wording"] != body["additions"][0]


def test_a_decided_proposal_is_not_offered_again(lucent):
    """Test 12: rejection sticks.

    The file lives one directory down. A hook that walked into `decided/`
    would re-raise every rejection on every prompt forever, and the cheapest
    way out of that loop is to accept.
    """
    _write_pending(lucent, "once")
    assert THE_LINE in _run_inject(lucent["proposals"]).stdout

    _soul_sh(lucent, "decide", "once", "--decision", "rejected", "--reason", "not me")

    after = _run_inject(lucent["proposals"])
    assert after.returncode == 0, after.stderr
    assert after.stdout.strip() == ""


def test_a_refused_write_leaves_the_proposal_pending(lucent):
    """Test 15: a failed write must not look like a decision.

    The previous version wrote the file itself and asserted it still existed,
    with nothing under test able to move it — it could not fail. Here the
    write is genuinely refused and the decision genuinely attempted.
    """
    lucent["state"].refuse = ("conflict", "this soul changed since you read it")
    pending = _write_pending(lucent, "refused")

    write = _soul_sh(lucent, "write", "--line", THE_LINE, "--reason", "should not land")
    assert write.returncode != 0

    # The skill's rule: only after the write succeeded. Nothing was decided,
    # so the proposal is still there to raise.
    assert pending.is_file()
    assert not (lucent["proposals"] / "decided" / "refused.json").exists()
    assert THE_LINE in _run_inject(lucent["proposals"]).stdout


def test_deciding_a_proposal_that_is_not_pending_fails(lucent):
    """A decision on nothing must not create a decided record.

    Otherwise a mistyped id retires a proposal that was never read, and
    nothing ever raises it again.
    """
    out = _soul_sh(lucent, "decide", "no-such-id", "--decision", "rejected",
                   "--reason", "typo")

    assert out.returncode != 0
    assert not (lucent["proposals"] / "decided").exists() or \
        not list((lucent["proposals"] / "decided").glob("*.json"))


def test_the_daily_review_dispatches_a_turn_the_gateway_accepts(tmp_path):
    """Test 14: the live path, not the dry run.

    The dry run and the dispatch are separate branches. The dispatch sent
    `{"message": ...}` while the gateway binds `content`, so every night it
    opened a session, took a 422, logged "dispatched" and exited 0 — and the
    test that covered this requirement only ever read the dry run.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "curl.log"
    (bin_dir / "curl").write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$CURL_LOG"\n'
        'case "$*" in *"/message"*) printf \'{"ok":true}\';; '
        '*) printf \'{"id":"sess-1"}\';; esac\n'
    )
    (bin_dir / "curl").chmod(0o755)

    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        CURL_LOG=str(log),
        HIVE_PROJECT_DIR=str(tmp_path),
        COMMS_URL="http://gateway:8426",
        MIND_ID="14cb820b-4a42-4f04-a593-54f532fd1d2f",
        COMMS_BEARER_TOKEN="stub",
    )
    out = subprocess.run(["bash", str(REVIEW_CRON)], capture_output=True,
                         text=True, env=env, timeout=60)

    assert out.returncode == 0, out.stderr
    sent = log.read_text()
    assert "/sessions/sess-1/message" in sent
    # The field the gateway's MessageRequest actually binds.
    assert '"content"' in sent
    assert '"message"' not in sent.split("/message")[1]


def test_the_daily_review_says_so_when_it_cannot_run(tmp_path):
    """A silent daily failure is a review that never happens for months.

    `${COMMS_URL:?}` inside a command substitution kills only the subshell,
    so a missing variable used to log an empty reason and exit 0 — which
    cron reports as success.
    """
    env = dict(os.environ, HIVE_PROJECT_DIR=str(tmp_path), COMMS_URL="",
               MIND_ID="", COMMS_BEARER_TOKEN="")
    out = subprocess.run(["bash", str(REVIEW_CRON)], capture_output=True,
                         text=True, env=env, timeout=60)

    assert out.returncode != 0
    log = (tmp_path / "data" / "auto-remember" / "soul_review_cron.log").read_text()
    assert "COMMS_URL is not set" in log
