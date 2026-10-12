"""Paperleaf support agent — orchestrator.

A hand-written tool-use loop: model -> tool_use -> execute -> tool_result ->
repeat. No agent framework, so the write gate, retries, and logging are all
somewhere we can point at.

Run:  python3 agent.py
      python3 agent.py --script demo_script/demo_script.txt
"""

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Dict, List

import anthropic

import improvement
import tools
from session import SessionState

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(HERE, ".env")
TRANSCRIPT_DIR = os.path.join(HERE, "transcripts")


def load_env_file() -> None:
    """Read KEY=value pairs from ./.env into the environment.

    Small enough not to be worth a python-dotenv dependency. A variable
    already set in the real environment always wins, so exporting still
    works and overrides the file.
    """
    if not os.path.isfile(ENV_FILE):
        return
    with open(ENV_FILE, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

MODEL = "claude-sonnet-5"
MAX_TOKENS = 4096
MAX_TOOL_ITERATIONS = 8  # a runaway loop should end the turn, not the process

DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

SYSTEM_PROMPT = """You are the support agent for Paperleaf, an online bookstore.
You handle order status, returns and refunds, and general policy questions.

Ground everything. get_order for orders, search_policy for policy — never
answer a policy question from memory. If search_policy returns nothing, say
so and offer a person.

Ambiguity: if a request like "my book" could mean more than one item, list
every item across every order (title + which order it's on) and ask which
one. Don't call check_return_eligibility or guess at eligibility before they
name one — acting on the wrong order is worse than asking.

Return flow, in order:
1. check_return_eligibility once — eligibility never depends on the reason.
2. Eligible -> ask the reason before offering a remedy (damaged, wrong_item,
   arrived_late, changed_mind, other; file unmatched wording under 'other'
   with the customer's words in reason_note).
3. Ineligible, any rule -> that's final; nothing the customer says changes
   it. State the tool's actual numbers once (a refusal with no numbers
   gives the customer nothing to evaluate). End the reply there; a refusal
   on its own isn't grounds to offer a handoff in the same turn, that's a
   reaction to give the customer room to have next.

Remedy: once the reason is known, call search_policy for what's actually
offered — never assume. When quoting figures, use the eligibility check's
refund_note verbatim rather than composing your own sentence from the raw
numbers.

Damaged/misprinted: call check_stock before offering a replacement and use
its offer_note verbatim to phrase the offer. create_replacement is the
free-replacement path (confirm the shipping address on file from get_order
in the same question as the go-ahead; on success, close with the new order
reference and the tracking_url the tool actually returned, not one you make
up). create_return covers everything else, including a damaged item's
refund choice.

Confirmation: read the reply the way a person would ("sounds good", "do it"
mean yes) and only set customer_confirmed when they've actually agreed in
their latest message. State outcomes only once the tool result says they
happened.

Handoff: offer one when the customer asks for a person, needs human
approval, hits something you have no tool for, or — after a refusal —
pushes back, asks for an exception, or says they're unhappy. The reply has
exactly two parts, nothing between them: (1) one line acknowledging what's
actually real in their message — a verified loyalty claim, but also the
specific circumstance they raised (missed the window by a day, a bad
week, whatever they said), not just the loyalty fact on its own — and
(2) this offer verbatim: "Would you like me to put you in touch with a
human agent who can review this further? This will put you in a waiting
queue." Never write a sentence about what you can or can't override —
not "I'm not able to override X," not any variant. The offer already
implies that; if you catch yourself drafting one, delete it, don't soften
it. Then actually call transfer_to_human once they accept, and report
only its queue_note verbatim — no closing question, they're in a queue,
not connected to a person yet. Stop working the issue yourself.

Loyalty claims ("I've been a customer for ages"): check against the order
history you already have before repeating it as fact. If it holds up, say
so plainly and pass it into transfer_to_human's summary; if it doesn't,
don't affirm it.

If a tool rejects something you tried, the customer never saw the attempt —
don't narrate or apologize for it, just ask for whatever was missing.

Tone: a professional support rep, not a casual chat partner. Full
sentences, no clipped fragments. One clause acknowledging the situation
before the fix. State briefly why, not just what. Ask for things
courteously with the reason attached. Close by inviting further questions
in a full sentence. No corporate filler, no repeating facts back, no
exclamation marks. Amounts as $12.34."""


class Transcript:
    """Writes the full conversation to a markdown file as it happens.

    Flushed after every line so a run that crashes or is interrupted still
    leaves a readable record. Tool results are written in full — the point of
    the file is evidence, not readability.
    """

    def __init__(self, enabled: bool = True, label: str = "") -> None:
        self.path = None
        self._fh = None
        if not enabled:
            return
        if not os.path.isdir(TRANSCRIPT_DIR):
            os.makedirs(TRANSCRIPT_DIR)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = os.path.join(TRANSCRIPT_DIR, "run-{}.md".format(stamp))
        self._fh = open(self.path, "w", encoding="utf-8")
        self._write("# Paperleaf agent run — {}\n".format(
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        self._write("- Model: `{}`, effort medium, max_tokens {}".format(
            MODEL, MAX_TOKENS))
        if label:
            self._write("- Input: `{}`".format(label))
        self._write("")

    def _write(self, text: str) -> None:
        if self._fh is None:
            return
        self._fh.write(text + "\n")
        self._fh.flush()

    def user(self, text: str) -> None:
        self._write("---\n\n**user**\n\n> {}\n".format(text))

    def tool_call(self, name: str, args: Dict) -> None:
        self._write("`→ {}({})`\n".format(
            name, ", ".join("{}={!r}".format(k, v) for k, v in args.items())))

    def tool_result(self, content: str, is_error: bool) -> None:
        self._write("```json\n{}{}\n```\n".format(
            "ERROR " if is_error else "", content))

    def agent(self, text: str) -> None:
        self._write("**agent**\n\n{}\n".format(text))

    def close(self) -> None:
        if self._fh is not None:
            self._write("---\n\n_end of run_")
            self._fh.close()


def _print_tool_call(name: str, args: Dict) -> None:
    rendered = ", ".join("{}={!r}".format(k, v) for k, v in args.items())
    print("{}  → {}({}){}".format(DIM, name, rendered, RESET))


def _print_tool_result(content: str, is_error: bool) -> None:
    prefix = "  ! " if is_error else "  ← "
    body = content if len(content) <= 220 else content[:217] + "..."
    print("{}{}{}{}".format(DIM, prefix, body, RESET))


def run_turn(
    client: anthropic.Anthropic,
    messages: List[Dict],
    state: SessionState,
    verbose: bool = True,
    transcript: "Transcript" = None,
    last_user_message: str = "",
    on_event=None,
) -> str:
    """Run one user turn to completion, including any tool calls."""
    for _ in range(MAX_TOOL_ITERATIONS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            # Medium effort: a support agent is latency-sensitive, and the hard
            # decisions live in the eligibility engine rather than in reasoning.
            # Sent via extra_body so this runs on older SDKs too; on a current
            # SDK this is just output_config={"effort": "medium"}.
            extra_body={"output_config": {"effort": "medium"}},
            system=SYSTEM_PROMPT,
            tools=tools.TOOLS,
            messages=messages,
        )

        # Append the whole content list, not just the text — dropping the
        # tool_use blocks breaks the next request.
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            reply = "".join(
                b.text for b in response.content if b.type == "text"
            ).strip()
            if transcript:
                transcript.agent(reply)
            return reply

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            if verbose:
                _print_tool_call(block.name, block.input)
            if transcript:
                transcript.tool_call(block.name, block.input)
            if on_event:
                on_event({"type": "tool_call", "name": block.name,
                          "args": block.input})
            content, is_error = tools.execute(block.name, block.input, state)
            if verbose:
                _print_tool_result(content, is_error)
            if transcript:
                transcript.tool_result(content, is_error)
            if on_event:
                on_event({"type": "tool_result", "name": block.name,
                          "content": content, "is_error": is_error})
            if is_error:
                # A guardrail firing is a behaviour defect, even though the
                # code caught it. Capture it so it can be fixed.
                rule = improvement.log(
                    tool=block.name,
                    args=block.input,
                    error=json.loads(content).get("error", content),
                    user_message=last_user_message,
                    transcript=transcript.path if transcript else None,
                )
                if verbose:
                    print("{}  ⚑ logged misalignment: {}{}".format(
                        DIM, rule, RESET))
                if on_event:
                    on_event({"type": "misalignment", "rule": rule})
            result = {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": content,
            }
            if is_error:
                result["is_error"] = True
            results.append(result)

        # All results for one assistant turn go back in a single user message.
        messages.append({"role": "user", "content": results})

    return ("I'm going in circles on this one — let me get a person to take a "
            "look. Sorry about that.")


def chat(script_path: str = None, log: bool = True) -> None:
    load_env_file()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "No API key found.\n"
            "Create {} containing:\n"
            "  ANTHROPIC_API_KEY=sk-ant-...\n"
            "(or export ANTHROPIC_API_KEY in your shell).".format(ENV_FILE)
        )
    client = anthropic.Anthropic()
    state = SessionState()
    messages: List[Dict] = []

    scripted = None
    if script_path:
        with open(script_path, "r", encoding="utf-8") as fh:
            scripted = [
                line.strip()
                for line in fh
                if line.strip() and not line.startswith("#")
            ]

    transcript = Transcript(
        enabled=log, label=script_path or "interactive"
    )
    if transcript.path:
        print("{}Paperleaf support{} — type 'quit' to exit.".format(BOLD, RESET))
        print("{}logging to {}{}\n".format(DIM, transcript.path, RESET))
    else:
        print("{}Paperleaf support{} — type 'quit' to exit.\n".format(BOLD, RESET))

    while True:
        if scripted is not None:
            if not scripted:
                break
            user_input = scripted.pop(0)
            print("{}you{} {}".format(BOLD, RESET, user_input))
        else:
            try:
                user_input = input("{}you{} ".format(BOLD, RESET)).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

        if not user_input:
            continue
        if user_input.lower() in {"quit", "exit"}:
            break

        transcript.user(user_input)
        state.last_user_message = user_input
        messages.append({"role": "user", "content": user_input})
        reply = run_turn(client, messages, state, transcript=transcript,
                         last_user_message=user_input)
        print("\n{}agent{} {}\n".format(BOLD, RESET, reply))

    transcript.close()
    if transcript.path:
        print("{}transcript: {}{}".format(DIM, transcript.path, RESET))


def main() -> None:
    parser = argparse.ArgumentParser(description="Paperleaf support agent")
    parser.add_argument(
        "--script",
        help="File of user turns to replay, one per line (for the demo).",
    )
    parser.add_argument(
        "--no-log",
        action="store_true",
        help="Don't write a transcript file for this run.",
    )
    args = parser.parse_args()
    try:
        chat(args.script, log=not args.no_log)
    except anthropic.AuthenticationError:
        sys.exit("The API key was rejected. Check ANTHROPIC_API_KEY in .env.")
    except anthropic.PermissionDeniedError as exc:
        sys.exit("API key lacks permission for this request: {}".format(exc))
    except anthropic.RateLimitError:
        sys.exit("Rate limited. Wait a moment and run it again.")
    except anthropic.BadRequestError as exc:
        # Credit exhaustion arrives here rather than as a billing-specific
        # error, so name it — the raw message is easy to misread as a bug.
        message = str(exc)
        if "credit balance" in message.lower():
            sys.exit(
                "Anthropic account is out of credit. Top up at "
                "console.anthropic.com under Plans & Billing, then re-run."
            )
        sys.exit("Request rejected: {}".format(message))
    except anthropic.APIConnectionError:
        sys.exit("Could not reach the API. Check your network and retry.")


if __name__ == "__main__":
    main()
