"""Who counts as internal, Joe's "urgent reply needed" list and his important senders (config + lists edited in
the app).

Important senders (kv `people:important`, DPAPI-sealed like other work data): addresses or "@domain" entries. New
mail from them raises a high-priority alert, their unread mail is listed under Follow-ups > Important senders, and
Mail / follow-up rows show a star. The effective list is the senders of the sender-only, priority-high rules in
`outlook.rules` (the "VIP sender" rule; config.example.yaml's two placeholders don't count) plus the app list.
Matching uses the outlook.rules semantics: an address entry equals the address, an "@domain" entry ends it (so
subdomains don't match)."""
import re

from . import config, db

KV_IMPORTANT = "people:important"
MAX_IMPORTANT = 200
# The examples config.example.yaml ships in its "VIP sender" rule: not real people, so never important.
CONFIG_PLACEHOLDERS = {"your.manager@sonance.com", "@importantcustomer.com"}
_DOMAIN = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+"
IMPORTANT_ENTRY = re.compile(rf"(?:[^@\s<>\"'(),;:]+)?@{_DOMAIN}")  # an address, or "@domain.com"
ADDRESS_IN = re.compile(rf"[^@\s<>\"'(),;:]+@{_DOMAIN}")


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


# ---- important senders -----------------------------------------------------------------------------------------

def address_of(text: str) -> str:
    """The email address in "Name <a@b.com>", "mailto:a@b.com" or a bare address, lowercase; "" if there's none."""
    m = ADDRESS_IN.search((text or "").strip().lower())
    return m.group(0) if m else ""


def sender_matches(sender: str, patterns) -> bool:
    """outlook.rules semantics: an address entry equals the sender; an "@domain" entry ends it."""
    sender = (sender or "").lower()
    return any(sender.endswith(p.lower()) if p.startswith("@") else sender == p.lower()
               for p in (str(p).strip() for p in patterns) if p)


def config_important_senders() -> list[str]:
    """Senders of the outlook.rules entries that list only senders, at priority high (the "VIP sender" rule)."""
    out = []
    for rule in config.section("outlook", {}).get("rules") or []:
        if not isinstance(rule, dict) or str(rule.get("priority", "")).lower() != "high":
            continue
        if any(rule.get(k) for k in ("keywords", "high_importance", "flagged")):
            continue  # a rule that also needs a keyword or a flag doesn't make all their mail important
        for p in rule.get("senders") or []:
            p = str(p).strip().lower()
            if p and p not in CONFIG_PLACEHOLDERS:
                out.append(p)
    return list(dict.fromkeys(out))


def app_important_senders() -> list[str]:
    value = db.kv_get(KV_IMPORTANT, [])
    return [str(p) for p in value] if isinstance(value, list) else []


def important_senders() -> list[str]:
    """The effective list (lowercase): the config VIP rule's senders plus the list edited in the app."""
    return list(dict.fromkeys(config_important_senders() + [p.lower() for p in app_important_senders()]))


def is_important(email: str, senders=None) -> bool:
    """`email`: an address or "Name <address>". Exchange DNs and names without an address never match.
    Pass `senders` (important_senders()) when checking many messages: each read decrypts the stored list."""
    addr = address_of(email)
    return bool(addr) and sender_matches(addr, important_senders() if senders is None else senders)


def shared_providers() -> list[str]:
    """Free-mail providers (spam.shared_providers): a whole-domain entry there would mean millions of people."""
    from . import spam
    return spam.settings()["shared_providers"]


def domain_refusal(domain: str) -> str | None:
    """Why "@domain" can't be an important-senders entry (an internal domain or a shared provider), or None."""
    domain = domain.lower().lstrip("@")
    inside = lambda d: domain == d or domain.endswith("." + d)  # noqa: E731
    if any(inside(d) for d in internal_domains()):
        return f"@{domain} is an internal domain. Add the person's address instead."
    if any(inside(p) for p in shared_providers()):
        return f"@{domain} is a shared mail provider used by millions of people. Add the person's address instead."
    return None


def set_important_senders(entries: list[str]) -> list[str]:
    """Replaces the app list. Each entry is an email address or "@domain.com" (not an internal domain or a shared
    provider); entries the config rule already lists are dropped. Returns the app list as saved."""
    if not isinstance(entries, list):
        raise ValueError("Send a list of email addresses or @domains")
    clean, configured = [], set(config_important_senders())
    for p in entries:
        p = str(p).strip().lower().removeprefix("mailto:")
        if not p:
            continue
        if len(p) > 254 or not IMPORTANT_ENTRY.fullmatch(p):
            raise ValueError(f"{p[:60]!r} isn't an email address or @domain.com")
        if p.startswith("@"):
            reason = domain_refusal(p)
            if reason:
                raise ValueError(reason)
        if p not in clean and p not in configured:
            clean.append(p)
    if len(clean) > MAX_IMPORTANT:
        raise ValueError(f"Up to {MAX_IMPORTANT} important senders")
    db.kv_set(KV_IMPORTANT, clean)
    return clean
