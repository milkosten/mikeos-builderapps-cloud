"""Offline tests for the assistant capability model (phase 29).

The two properties these exist to protect are the ones that make assistants safe to ship:

1. **Roles are OPEN-ENDED.** Nothing in the model may reject a role because it is not in a
   list. If someone ever "tidies" `role` into an enum, "Expense management assistant" stops
   being expressible and the whole design collapses into four hardcoded personas.

2. **Capabilities, not role names, are what is enforced.** A "Security" assistant whose SOUL
   insists it may refactor the codebase still cannot write, because `require()` looks only at
   the granted set. That is the single choke point, and a capability the runtime does not
   know how to enforce must never be storable as if it were a grant.

    python3 -m tests.test_assistants
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import assistants as A  # noqa: E402

_passed = 0
_failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
    else:
        _failed.append(f"{name}{(': ' + detail) if detail else ''}")


def denied(assistant: dict, cap: str) -> bool:
    try:
        A.require(assistant, cap)
        return False
    except A.Denied:
        return True


def main() -> int:
    # ---- 1. roles are open-ended -----------------------------------------
    for role in ["Product Owner", "Security assistant", "Expense management assistant",
                 "SEO assistant", "GDPR compliance officer", "on-call", "Väktare", "🐙"]:
        check(f"role {role!r} produces a usable SOUL",
              len(A.default_soul(role, role, "")) > 100)
        check(f"role {role!r} slugs to a safe repo path",
              "/" not in A.slug(role) and ".." not in A.slug(role) and A.slug(role) != "")
    check("templates are a list of pre-fills, not a closed set",
          len(A.template_list()) == 6 and all(t.get("soul_md") for t in A.template_list()))
    check("no template locks a role", all(isinstance(t["role"], str) and t["role"]
                                          for t in A.TEMPLATES))

    # ---- 2. capabilities are what is enforced ----------------------------
    security = {"id": 1, "role": "Security", "capabilities": ["read_repo", "comment"]}
    check("granted capability passes", not denied(security, "read_repo"))
    check("ungranted edit_code is refused", denied(security, "edit_code"))
    check("ungranted commit_push is refused", denied(security, "commit_push"))
    check("ungranted request_deploy is refused", denied(security, "request_deploy"))

    # A role name must buy nothing. A "Developer" with no grants can do nothing at all.
    dev_no_caps = {"id": 2, "role": "Developer", "capabilities": []}
    check("a role name grants nothing on its own",
          all(denied(dev_no_caps, c) for c in A.CAPABILITY_IDS))

    # ...and an arbitrary role WITH grants can do exactly those things.
    seo = {"id": 3, "role": "SEO assistant", "capabilities": ["read_repo", "comment"]}
    check("an invented role is first-class", not denied(seo, "comment"))
    check("an invented role is still bounded", denied(seo, "edit_code"))

    # ---- 3. an unknown capability can never look like a grant ------------
    check("unknown capability ids are dropped, not stored",
          A.sanitize_capabilities(["read_repo", "become_root", "edit_code"])
          == ["read_repo", "edit_code"])
    check("sanitize is order-canonical and de-duplicating",
          A.sanitize_capabilities(["comment", "read_repo", "comment"])
          == ["read_repo", "comment"])
    check("a non-list is not a capability set", A.sanitize_capabilities("read_repo") == [])
    forged = {"id": 4, "role": "x", "capabilities": ["become_root"]}
    check("requiring an unknown capability is refused, not allowed",
          denied(forged, "become_root"))

    # jsonb sometimes arrives as a JSON *string*; `has` must still be correct.
    check("capabilities as a json string still resolve",
          A.has({"capabilities": '["comment"]'}, "comment"))
    check("capabilities as a json string still deny",
          not A.has({"capabilities": '["comment"]'}, "edit_code"))

    # ---- 4. the write capabilities default OFF ---------------------------
    for tpl in A.TEMPLATES:
        for risky in ("edit_code", "commit_push", "request_deploy"):
            check(f"template {tpl['key']} does not pre-grant {risky}",
                  risky not in tpl["capabilities"],
                  "v1 keeps unattended writes off until the quota+guard work lands")
    check("the capability table marks the risky ones",
          all(A.CAPABILITIES[c]["safe_default"] is False
              for c in ("edit_code", "commit_push", "request_deploy")))

    # ---- 5. interval clamping (a runaway heartbeat is a cost bug) --------
    check("interval floor", A.clamp_interval(0) == A.MIN_INTERVAL_MIN)
    check("interval ceiling", A.clamp_interval(10 ** 9) == A.MAX_INTERVAL_MIN)
    check("interval garbage falls back", A.clamp_interval("soon", 60) == 60)
    check("interval passes a sane value", A.clamp_interval(120) == 120)

    print(f"\n{_passed} passed, {len(_failed)} failed")
    for f in _failed:
        print("  FAIL " + f)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
