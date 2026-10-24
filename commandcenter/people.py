"""Who counts as internal, and Joe's "urgent reply needed" list (config + list edited in the app)."""
import re

from . import config, db


def internal_domains() -> list[str]:
    return [d.lower().lstrip("@") for d in config.section("followups", {}).get("internal_domains") or ["sonance.com"]]


def is_internal(email: str) -> bool:
    email = (email or "").lower()
    return any(email.endswith("@" + d) for d in internal_domains())


def urgent_people() -> list[str]:
    """Email addresses or full names. The config list plus the one edited in the app."""
    from_config = config.section("followups", {}).get("urgent_people") or []
    return list(dict.fromkeys([str(p).strip() for p in from_config + db.kv_get("people:urgent", []) if str(p).strip()]))


def set_urgent_people(people: list[str]) -> list[str]:
    clean = []
    for p in people[:100]:
        p = str(p).strip()
        if not p:
            continue
        if len(p) > 120 or not re.fullmatch(r"[\w .'@&()-]+", p):
            raise ValueError(f"{p!r} doesn't look like a name or email address")
        if p.lower() not in [c.lower() for c in clean]:
            clean.append(p)
    db.kv_set("people:urgent", clean)
    return urgent_people()


def matches(email: str, name: str, people: list[str]) -> bool:
    email, name = (email or "").lower(), (name or "").lower()
    # Outlook display names often add a suffix, e.g. "Jane Doe (Sonance)"; compare without it.
    bare = re.sub(r"\s*\(.*?\)\s*$", "", name)
    for p in people:
        p = p.lower()
        if "@" in p:
            if email == p or (p.startswith("@") and email.endswith(p)):
                return True
        elif p and (p == name or p == bare):
            return True
    return False
