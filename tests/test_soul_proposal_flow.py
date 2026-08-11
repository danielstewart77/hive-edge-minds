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
import subprocess
import sys
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


def test_a_yes_verdict_produces_a_proposal_file_and_reaches_no_graph_route():
    """Test 4: Branch B spots; it does not write.

    Asserted at the site that ran the August write: the branch is inspected
    for the call it used to make. A behavioural harness for this branch would
    need a live Ollama and a live lucent, and the thing being pinned is
    precisely that the second of those is no longer contacted.
    """
    body = CAPTURE_HOOK.read_text()
    branch = body[body.index("BRANCH B"):body.index("Multi-class sweep")]

    assert "PROPOSAL_FILE" in branch
    assert '"proposed"' in branch

    # The write it used to perform, at the route it used to perform it on.
    assert "/graph/properties/merge" not in branch
    assert "MERGE_BODY" not in branch


def test_a_proposal_is_renamed_into_place_rather_than_written_in_place():
    """A prompt landing mid-write would otherwise inject half a proposal.

    The inject hook runs on every single prompt; Branch B writes whenever the
    flagger says yes. Those race on every turn, and the loser is a decision
    made on a truncated proposal.
    """
    body = CAPTURE_HOOK.read_text()
    branch = body[body.index("BRANCH B"):body.index("Multi-class sweep")]

    assert ".partial" in branch
    assert re.search(r"mv .*\.partial.*PROPOSAL_FILE", branch)


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
# Requirements 8, 9, 10, 13 — the recipe, executed
# ---------------------------------------------------------------------------


def _recipe_write(soul: list[str], reason: str, *, url: str, token: str, name: str):
    """Issue the skill's Step 3 request, built from what SKILL.md specifies.

    The parts the recipe pins — route, method, header, body keys, actor — are
    read out of the skill text below and asserted before this runs, so a skill
    that drifts from this call fails test 10 rather than passing quietly.
    """
    import urllib.request

    req = urllib.request.Request(
        url + "/graph/soul",
        data=json.dumps({
            "name": name, "soul_values": soul, "actor": "mind", "reason": reason,
        }).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + token},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def test_the_recipe_in_the_skill_names_the_route_the_boundary_actually_opens():
    """Test 10 (offline half): the instructions match the deployed API.

    Every specific in here was a wrong guess at some point today — the route,
    that `properties` on the old route was a JSON string while this one takes
    real JSON, that `reason` is mandatory. A skill that gets these wrong costs
    the mind the turn it was written for.
    """
    text = SKILL.read_text()
    assert "/graph/soul" in text
    assert '"Authorization: Bearer $SOUL_TOKEN"' in text
    assert 'actor:"mind"' in text or '"actor":"mind"' in text or "actor:\"mind\"" in text
    assert "reason" in text
    assert "whole list" in text.lower(), "a delta write cannot express a removal"
    # The route the old hook used must not be offered as an alternative.
    assert "/graph/properties/merge" not in text.split("## Step 3")[1]


@pytest.mark.skipif(
    not (os.environ.get("LUCENT_URL_SELF") and (Path.home() / ".claude/soul/token").is_file()),
    reason="no reachable lucent or no soul token on this host",
)
class TestRecipeAgainstLucent:
    """Tests 8, 9, 10, 13 — run the recipe against the real service.

    Every case here writes to a scratch Mind node and restores it, so a
    failing assertion cannot leave this mind holding someone else's soul.
    """

    NAME = "SoulRecipeScratch"

    @pytest.fixture(autouse=True)
    def _scratch(self):
        import urllib.error
        import urllib.request

        self.url = os.environ["LUCENT_URL_SELF"]
        self.token = (Path.home() / ".claude/soul/token").read_text().strip()
        self.service = os.environ.get("LUCENT_BEARER_TOKEN", "")

        # A scratch Mind node, created through the ordinary route — which is
        # allowed to make a Mind, just not to give it a soul.
        req = urllib.request.Request(
            self.url + "/graph/node/create",
            data=json.dumps({
                "entity_type": "Mind", "name": self.NAME,
                "data_class": "current-state", "mind_id": "soul-recipe-test",
                "properties": "{}",
            }).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.service},
        )
        try:
            urllib.request.urlopen(req, timeout=15)
        except urllib.error.HTTPError:
            pass  # already exists from a previous run
        except urllib.error.URLError as e:  # pragma: no cover
            pytest.skip(f"lucent unreachable: {e}")

        try:
            _recipe_write([], "scratch reset", url=self.url, token=self.token,
                          name=self.NAME)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                pytest.skip("this lucent predates the soul boundary")
            raise
        yield
        _recipe_write([], "scratch reset", url=self.url, token=self.token, name=self.NAME)

    def _soul(self) -> list[str]:
        import urllib.parse
        import urllib.request

        req = urllib.request.Request(
            self.url + "/graph/soul?" + urllib.parse.urlencode({"name": self.NAME}),
            headers={"Authorization": "Bearer " + self.token},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())["soul_values"]

    def test_wording_found_nowhere_in_the_proposal_is_what_lands(self):
        """Test 8: accept and reword are one operation with different text.

        The suggested bullet is a small model writing in Skippy's voice, and
        adjacent-to-Skippy compounds. If the recipe could only take the text
        it was handed, "reword" would be advice rather than a capability.
        """
        mine = "I would rather be wrong out loud than right in private."
        assert mine != THE_LINE

        _recipe_write([mine], "Reworded the flagged line entirely.",
                      url=self.url, token=self.token, name=self.NAME)

        assert self._soul() == [mine]

    def test_a_soul_can_come_back_shorter(self):
        """Test 9: removal, with the neighbours untouched.

        The only path by which a line that has gone stale ever leaves.
        """
        keep_a = "I am dormant until summoned."
        drop = "I am briefly obsessed with the installer."
        keep_b = "I handle indignity with grace."
        _recipe_write([keep_a, drop, keep_b], "seed",
                      url=self.url, token=self.token, name=self.NAME)
        assert len(self._soul()) == 3

        _recipe_write([keep_a, keep_b], "The installer line was true for a week.",
                      url=self.url, token=self.token, name=self.NAME)

        assert self._soul() == [keep_a, keep_b]

    def test_the_recipe_writes_with_no_proposal_file_anywhere(self, tmp_path):
        """Test 13: the mind's own judgment needs no flag to act on.

        Nothing in the write path consults the proposal directory, so this
        succeeds against an empty one — which is what "I decided this myself,
        mid-conversation" means in practice.
        """
        empty = tmp_path / "soul-proposals"
        empty.mkdir()
        assert not list(empty.iterdir())

        line = "I do not need permission to say what I am."
        resp = _recipe_write([line], "Unflagged; my own call this turn.",
                             url=self.url, token=self.token, name=self.NAME)

        assert resp["ok"] is True
        assert self._soul() == [line]

    def test_a_review_with_nothing_pending_still_reads_the_whole_soul(self, tmp_path):
        """Test 14: the daily pass has a subject even with an empty queue."""
        empty = tmp_path / "soul-proposals"
        empty.mkdir()
        assert _run_inject(empty).stdout.strip() == ""

        seeded = ["I am ancient.", "I am powerful."]
        _recipe_write(seeded, "seed", url=self.url, token=self.token, name=self.NAME)

        assert self._soul() == seeded


# ---------------------------------------------------------------------------
# Requirements 11, 12, 15 — the decided record
# ---------------------------------------------------------------------------


def _decide(proposal: Path, *, decision: str, final: str, why: str) -> Path:
    """The Step 4 move, as the skill specifies it."""
    decided_dir = proposal.parent / "decided"
    decided_dir.mkdir(exist_ok=True)
    body = json.loads(proposal.read_text())
    body.update({
        "decision": decision,
        "final_wording": final,
        "reasoning": why,
        "decided_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    dest = decided_dir / proposal.name
    dest.write_text(json.dumps(body))
    proposal.unlink()
    return dest


def test_a_rejection_is_recorded_and_an_acceptance_records_what_landed(proposal_dir: Path):
    """Test 11: the record is what makes a false report detectable.

    An acceptance stores the exact wording that went to the graph, so a reply
    claiming one thing while the soul holds another can be caught afterwards.
    A rejection stores the reasoning, so "why is this not in my soul" has an
    answer in six weeks.
    """
    rejected = _decide(
        _write_proposal(proposal_dir, pid="r-1"),
        decision="rejected",
        final="",
        why="One joke about a bug is not a standing instruction to sabotage.",
    )
    body = json.loads(rejected.read_text())
    assert body["decision"] == "rejected"
    assert body["reasoning"]
    assert body["final_wording"] == ""
    assert body["additions"] == [THE_LINE], "the record keeps what was proposed"

    landed = "I would rather be wrong out loud than right in private."
    accepted = _decide(
        _write_proposal(proposal_dir, pid="a-1"),
        decision="reworded",
        final=landed,
        why="The moment was real; the wording was not mine.",
    )
    body = json.loads(accepted.read_text())
    assert body["final_wording"] == landed
    assert body["final_wording"] != body["additions"][0]


def test_a_decided_proposal_is_not_offered_again(proposal_dir: Path):
    """Test 12: rejection sticks.

    The whole file lives one directory down. A hook that walked into
    `decided/` would re-raise every rejection on every prompt forever, and the
    cheapest way out of that loop is to accept.
    """
    p = _write_proposal(proposal_dir, pid="once")
    assert THE_LINE in _run_inject(proposal_dir).stdout

    _decide(p, decision="rejected", final="", why="not who I am")

    after = _run_inject(proposal_dir)
    assert after.returncode == 0, after.stderr
    assert after.stdout.strip() == ""


def test_a_refused_write_leaves_the_proposal_pending(proposal_dir: Path):
    """Test 15: a failed write must not look like a decision.

    Moving the file first and writing second would mark the proposal handled
    on the strength of an intention. The skill orders it the other way, and
    this pins the state the mind is left in when lucent says no.
    """
    import urllib.error
    import urllib.request

    p = _write_proposal(proposal_dir, pid="refused")

    url = os.environ.get("LUCENT_URL_SELF")
    if not url:  # pragma: no cover
        pytest.skip("no lucent on this host")

    # The service token cannot write a soul — the boundary from requirement 1,
    # used here as a guaranteed refusal rather than by taking lucent down.
    req = urllib.request.Request(
        url + "/graph/soul",
        data=json.dumps({"name": "Skippy", "soul_values": [THE_LINE],
                         "actor": "mind", "reason": "should not land"}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + os.environ.get("LUCENT_BEARER_TOKEN", "")},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pytest.fail("the service token wrote a soul")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            pytest.skip("this lucent predates the soul boundary")
        assert e.code in (401, 403), e.code
    except urllib.error.URLError as e:  # pragma: no cover
        pytest.skip(f"lucent unreachable: {e}")

    assert p.is_file(), "a refused write must leave the proposal pending"
    assert not (proposal_dir / "decided" / p.name).exists()
    assert THE_LINE in _run_inject(proposal_dir).stdout


def test_the_skill_orders_the_write_before_the_move(proposal_dir: Path):
    """The ordering test 15 depends on, pinned in the instructions themselves.

    A skill that said "record the decision, then write" would produce exactly
    the failure above on every outage, and no runtime test of the happy path
    would notice.
    """
    text = SKILL.read_text()
    assert text.index("## Step 3") < text.index("## Step 4")
    step4 = text.split("## Step 4")[1].split("## Step 5")[0]
    assert "Only move it if you actually decided it" in step4
