"""Offline tests for the stay-embeddable platform contract.

The bug this exists to prevent: a generated app added clickjacking protection (correct,
unasked-for, good practice) and the builder's Site tab went to the browser's grey "refused
to connect". The app was serving 200s and its /health was green the whole time. Nothing in
the platform noticed, because nothing in the platform had ever been told the builder frames
the app.

Three properties are worth pinning down:

1. **The verdict is right for the header combinations that actually occur.** It has to catch
   `X-Frame-Options` even when the CSP is correct (browsers enforce both, so a stray DENY
   overrides a good frame-ancestors), and it must not be generous with the ones it does not
   recognise — guessing "probably fine" puts a dead grey frame back on the screen with the
   UI insisting everything is well.
2. **The rule reaches every prompt that writes code.** Codegen, the agentic build loop, the
   assistant's reasoning call and the assistant's coding agent are four different prompts in
   two different runtimes; the contract is only kept if it is in all of them.
3. **The rule names the real origin, as a requirement.** A previous platform rule used a
   made-up example value and the model copied the example into a real endpoint.

    python3 -m tests.test_embeddable
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []


def check(name, cond, why=""):
    (PASS if cond else FAIL).append((name, why))


def main() -> int:
    from server import introspect as I

    # ---- 1. the verdict ---------------------------------------------------
    cases = [
        # (headers, expected embeddable, what it is)
        ({}, True, "no framing headers at all"),
        ({"content-security-policy": "default-src 'self'"}, True, "a CSP with no frame-ancestors"),
        ({"content-security-policy": "frame-ancestors 'self' " + I.BUILDER_ORIGIN}, True,
         "exactly the policy the contract asks for"),
        ({"content-security-policy": "frame-ancestors https://*.osmike.com"}, True, "a wildcard host"),
        ({"content-security-policy": "frame-ancestors *"}, True, "allow anything"),
        ({"content-security-policy": "frame-ancestors builderapps.osmike.com"}, True,
         "a source with the scheme omitted"),
        ({"content-security-policy": "frame-ancestors 'none'"}, False, "the observed live bug"),
        ({"content-security-policy": "frame-ancestors 'self'"}, False,
         "'self' is the APP's origin, not the builder's"),
        ({"content-security-policy": "frame-ancestors https://evil.com"}, False, "someone else's origin"),
        ({"content-security-policy": "frame-ancestors http://builderapps.osmike.com"}, False,
         "right host, wrong scheme"),
        ({"x-frame-options": "DENY"}, False, "XFO alone"),
        ({"x-frame-options": "SAMEORIGIN"}, False, "SAMEORIGIN cannot name another origin"),
        # The combination that matters most: a CORRECT CSP is still overridden by XFO,
        # because the browser applies both. An app that "fixed" only the CSP stays broken.
        ({"x-frame-options": "DENY",
          "content-security-policy": "frame-ancestors 'self' " + I.BUILDER_ORIGIN}, False,
         "a correct CSP does not rescue a stray X-Frame-Options"),
    ]
    for headers, expected, what in cases:
        v = I._verdict(headers)
        check(f"verdict: {what}", v["embeddable"] is expected,
              f"got embeddable={v['embeddable']}, reason={v['reason']!r}")
    check("a blocked verdict always explains itself in words",
          all(I._verdict(h)["reason"].strip() for h, e, _ in cases if not e))
    check("a blocked verdict names the origin the app must allow",
          all(I.BUILDER_ORIGIN in I._verdict(h)["reason"] for h, e, _ in cases if not e))

    # ---- 2. the rule is in every prompt that writes code -------------------
    from server.harness import codegen
    marker = "frame-ancestors 'self' https://builderapps.osmike.com"
    check("codegen's NO_SAAS_RULE carries the contract",
          marker in codegen.NO_SAAS_RULE and "X-Frame-Options" in codegen.NO_SAAS_RULE)
    check("it is numbered rule 9, alongside 6-8",
          "9. PLATFORM CONTRACT" in codegen.NO_SAAS_RULE)
    check("PLATFORM_CONTRACTS carries both platform rules",
          marker in codegen.PLATFORM_CONTRACTS and "/health" in codegen.PLATFORM_CONTRACTS)

    from server.harness import agentic
    check("the agentic build loop's system prompt carries it",
          marker in agentic._system_prompt("feature"),
          "per-feature codegen is where an app's headers get written")

    rsrc = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "server", "assistant_runtime.py")).read()
    check("the assistant's reasoning prompt carries it",
          "codegen.PLATFORM_CONTRACTS" in rsrc and rsrc.count("codegen.PLATFORM_CONTRACTS") >= 2,
          "once into the reason() system prompt, once into the container's /context")

    bsrc = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "assistant-runtime", "beat.py")).read()
    check("the coding agent's grounding takes the rules from /context",
          'platform_rules' in bsrc and "rules" in bsrc,
          "baked into the image = only live after a docker build; from /context = live now")

    # ---- 3. the wording cannot be copied as a placeholder ------------------
    check("the contract states the origin as a requirement, not a sample",
          "not a placeholder" in codegen.EMBED_CONTRACT)
    check("the contract explains WHY X-Frame-Options is not an option",
          "ALLOW-FROM" in codegen.EMBED_CONTRACT)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for name, why in FAIL:
        print(f"  FAIL {name}" + (f": {why}" if why else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
