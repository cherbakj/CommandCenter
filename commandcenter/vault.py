"""Personal vault: Joe's personal data (personal calendar entries, overlay-calendar links), encrypted with a
key that only his own passphrase can produce.

Why not DPAPI like the work data: DPAPI keys of a domain account can be recovered with the domain's DPAPI
backup key, so company administrators could read DPAPI-protected data. The vault key instead comes from a
passphrase Joe chose, that is not stored anywhere (no file, no Credential Manager, no log, no response):

- Key: scrypt(passphrase, random 16-byte salt, n=2**17, r=8, p=1) -> 32 bytes (AES-256). Derived on unlock and
  held only in this process's memory (inside the AES-GCM object; the derived bytes and the encoded passphrase
  are overwritten as soon as that object exists, best effort). Lock, Privacy mode, midnight and every restart drop
  it; a Command Center lock, locking Windows and system sleep / hibernate (privacy.py) too, unless
  privacy.stay_unlocked_today is on (then it stays until midnight of the day it was unlocked).
- A lock generation counter (_gen) goes up on every lock and on Privacy mode on. An unlock / setup / change that
  was checking a passphrase when that happened throws its new key away, so a Lock or Privacy mode pressed during
  the (slow) key derivation is never undone.
- Records: AES-256-GCM with a random 12-byte nonce per write. The record id is the associated data, so a blob
  can't be moved to another record. The record's kind (entry / calendar) is inside the ciphertext, and the
  plaintext is padded to 64 bytes, so the database shows neither what a record is nor its exact size.
- SQLite holds only: salt, KDF parameters, an encrypted verifier, the failed-attempt counter (vault_meta), and
  id + nonce||ciphertext per record (vault_records).
- Wrong passphrases: 5 free tries, then 30 s, 60 s, 120 s ... (capped at 1 h) between tries; the counter is in
  the database, so restarting doesn't reset it. One attempt at a time.
- Forgot the passphrase: nothing can recover the data (by design). "Reset personal vault" deletes it all.

Other modules register a store (register_store) so the vault can migrate their old DPAPI / Credential Manager
copies the first time it is unlocked, wipe them on reset, and clear their caches on lock.
"""
import ctypes
import hmac
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import unicodedata
from datetime import date, datetime

from fastapi import APIRouter, Body, HTTPException

from . import db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/vault")

FORMAT = 1
KDF = {"name": "scrypt", "n": 2 ** 17, "r": 8, "p": 1, "len": 32}
SCRYPT_MAXMEM = 512 * 1024 * 1024        # n=2**17, r=8 needs 128 MiB (+ a little); OpenSSL's default cap is 32 MiB
MIN_LENGTH = 12
MAX_LENGTH = 256
FREE_TRIES = 5
BASE_WAIT = 30
MAX_WAIT = 3600
PAD = 64
VERIFIER = b"CommandCenter personal vault verifier v1"
VERIFIER_AAD = b"cc-vault|verifier|v1"
RESET_PHRASE = "DELETE PERSONAL DATA"


class VaultLocked(Exception):
    pass


_state_lock = threading.RLock()     # guards _aead and _gen. Order: _state_lock, then db._lock (never the reverse)
_attempt_lock = threading.Lock()    # one unlock / change attempt at a time
_migrate_lock = threading.Lock()    # one migration of old copies at a time
_aead = None                        # AESGCM holding the key while unlocked; None = locked
_gen = [0]                          # lock generation: +1 on every lock() and Privacy mode on
_unlocked_at = [0.0]                # time.monotonic() of the last unlock
_unlocked_day = [""]                # local date (ISO) of the last unlock (privacy.stay_unlocked_today)
_privacy: dict = {}                 # {"on": bool}, cached copy of kv "privacy:mode"
_stores: list[dict] = []


# ---- registry for the modules that keep personal data ---------------------------------------

def register_store(name: str, *, legacy_count=None, migrate=None, wipe_legacy=None, on_lock=None):
    """legacy_count() -> int: old copies not yet in the vault. migrate() -> int: moves them in (vault unlocked),
    then deletes the old copies. wipe_legacy(): deletes old copies (reset). on_lock(): drop in-memory copies."""
    _stores[:] = [s for s in _stores if s["name"] != name]
    _stores.append({"name": name, "legacy_count": legacy_count, "migrate": migrate, "wipe_legacy": wipe_legacy, "on_lock": on_lock})


# ---- storage ------------------------------------------------------------------------------------

_tables_ready = False


def _tables():
    global _tables_ready
    if _tables_ready:
        return
    with db._lock:
        c = db._db()
        c.execute("CREATE TABLE IF NOT EXISTS vault_meta (id INTEGER PRIMARY KEY CHECK (id = 1), format INTEGER NOT NULL,"
                  " salt BLOB NOT NULL, kdf TEXT NOT NULL, verifier BLOB NOT NULL, created TEXT NOT NULL,"
                  " failures INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0)")
        c.execute("CREATE TABLE IF NOT EXISTS vault_records (id TEXT PRIMARY KEY, blob BLOB NOT NULL)")
        c.commit()
    _tables_ready = True


def _meta() -> dict | None:
    _tables()
    with db._lock:
        row = db._db().execute("SELECT * FROM vault_meta WHERE id = 1").fetchone()
    return dict(row) if row else None


def _set_attempts(failures: int, retry_at: float):
    with db._lock:
        db._db().execute("UPDATE vault_meta SET failures = ?, retry_at = ? WHERE id = 1", (failures, retry_at))
        db._db().commit()


class _SecureDelete:
    """Deleted rows are overwritten in the database file (not just unlinked) while this is active."""
    def __enter__(self):
        c = db._db()
        self.old = c.execute("PRAGMA secure_delete").fetchone()[0]
        c.execute("PRAGMA secure_delete = ON")
        return c

    def __exit__(self, *exc):
        db._db().execute(f"PRAGMA secure_delete = {int(self.old)}")


def secure_delete():
    """For stores' wipe/migrate code: `with db._lock, vault.secure_delete() as c: c.execute("DELETE ...")`."""
    return _SecureDelete()


# ---- crypto -------------------------------------------------------------------------------------

def _wipe(buf):
    """Best effort: overwrite secret bytes in place once they're no longer needed (Python can't promise that no
    other copy exists). A bytearray is cleared directly; a bytes object only this module holds (a fresh scrypt
    result or encoded passphrase, never a shared or cached one) is cleared through its CPython buffer."""
    try:
        n = len(buf)
        if isinstance(buf, bytearray) and n:
            ctypes.memset((ctypes.c_char * n).from_buffer(buf), 0, n)
        elif type(buf) is bytes and n > 1 and sys.implementation.name == "cpython":
            ctypes.memset(id(buf) + bytes.__basicsize__ - 1, 0, n)  # PyBytesObject.ob_sval
    except Exception:
        pass


def _derive(passphrase: str, salt: bytes, kdf: dict):
    """The AES-GCM object for this passphrase. The derived key bytes and the encoded passphrase are wiped as soon
    as the object exists (AESGCM keeps its own copy inside OpenSSL)."""
    import hashlib
    if kdf.get("name") != "scrypt":
        raise ValueError("Unknown key derivation")
    data = unicodedata.normalize("NFC", passphrase).encode("utf-8")
    try:
        key = hashlib.scrypt(data, salt=salt, n=int(kdf["n"]), r=int(kdf["r"]), p=int(kdf["p"]),
                             maxmem=SCRYPT_MAXMEM, dklen=int(kdf.get("len", 32)))
    finally:
        _wipe(data)
    try:
        return _aesgcm(key)
    finally:
        _wipe(key)
        del key


def _aesgcm(key: bytes):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(key)


def _seal(aead, plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + aead.encrypt(nonce, plaintext, aad)


def _unseal(aead, blob: bytes, aad: bytes) -> bytes:
    return aead.decrypt(bytes(blob[:12]), bytes(blob[12:]), aad)  # raises InvalidTag on a wrong key / tampering


def _verifier_ok(aead, blob: bytes) -> bool:
    from cryptography.exceptions import InvalidTag
    try:
        plain = _unseal(aead, blob, VERIFIER_AAD)
    except (InvalidTag, ValueError):
        return False
    return hmac.compare_digest(plain, VERIFIER)


def _record_aad(rid: str) -> bytes:
    return f"cc-vault|record|v1|{rid}".encode()


def _encode(payload: dict) -> bytes:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return raw + b" " * (-len(raw) % PAD)  # JSON ignores trailing whitespace


def _current():
    with _state_lock:
        if _aead is None:
            raise VaultLocked()
        return _aead


# ---- passphrase rules -----------------------------------------------------------------------------

_COMMON = set("""
password passw passwd pass passphrase letmein welcome admin administrator login qwerty qwertyuiop asdf asdfgh
asdfghjkl zxcvbn zxcvbnm abc abcd abcdef iloveyou loveyou love monkey dragon football baseball basketball soccer
hockey princess sunshine shadow master superman batman trustno changeme secret default guest test testing user
hello welcome summer winter spring autumn fall january february march april may june july august september
october november december monday tuesday wednesday thursday friday saturday sunday sonance commandcenter command
center joe joseph cherbak joecherbak jc company work office computer windows microsoft outlook google icloud
apple iphone samsung starwars pokemon whatever freedom computer internet michael jordan charlie thomas
correcthorsebatterystaple correcthorse batterystaple
""".split())
_SEQUENCES = ("abcdefghijklmnopqrstuvwxyz", "0123456789", "qwertyuiopasdfghjklzxcvbnm", "1qaz2wsx3edc4rfv5tgb")


def passphrase_problem(passphrase: str) -> str | None:
    """Why this passphrase is too weak, or None. Never logs or returns the passphrase."""
    if not isinstance(passphrase, str):
        return "Enter a passphrase"
    if len(passphrase) < MIN_LENGTH:
        return f"Use at least {MIN_LENGTH} characters"
    if len(passphrase) > MAX_LENGTH:
        return f"Use at most {MAX_LENGTH} characters"
    if passphrase != passphrase.strip():
        return "No spaces at the very start or end (they're easy to mistype)"
    if any(unicodedata.category(ch) == "Cc" for ch in passphrase):
        return "Use letters, numbers, spaces and symbols only"
    low = passphrase.lower()
    if len(set(low)) < 6 or re.fullmatch(r"(.+?)\1+", low):
        return "Too repetitive. Use several different words or characters"
    if low.isdigit():
        return "Not just numbers: use words (a short sentence works well)"
    flat = re.sub(r"[^a-z0-9]", "", low)
    if any(flat and flat in seq * 2 for seq in _SEQUENCES):
        return "Keyboard or alphabet sequences are too easy to guess"
    letters = re.sub(r"[^a-z]", "", low)
    leftover = letters
    try:
        import getpass
        user = getpass.getuser().lower()
    except Exception:
        user = ""
    for word in sorted(_COMMON | ({user} if len(user) >= 3 else set()), key=len, reverse=True):
        if len(word) >= 3:
            leftover = leftover.replace(word, "")
    if letters in _COMMON or len(leftover) < 4:
        return "Too easy to guess (common words, names, the company or app name, dates)"
    classes = sum(bool(re.search(p, passphrase)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    if len(passphrase) < 16 and classes < 3:
        return "Under 16 characters, mix at least three of: lower case, upper case, numbers, symbols. Or use a longer phrase"
    return None


# ---- state ------------------------------------------------------------------------------------

def is_set_up() -> bool:
    return _meta() is not None


def unlocked() -> bool:
    with _state_lock:
        return _aead is not None


def privacy_on() -> bool:
    if "on" not in _privacy:
        _privacy["on"] = bool(db.kv_get("privacy:mode", False))
    return _privacy["on"]


def set_privacy(on: bool):
    with _state_lock:
        if on:
            _gen[0] += 1  # an unlock that is still checking its passphrase must not turn this off again
        _privacy["on"] = bool(on)
        db.kv_set("privacy:mode", bool(on))
    if on:
        lock("privacy mode")


def generation() -> int:
    with _state_lock:
        return _gen[0]


def available() -> bool:
    """Personal data may be read and shown: the vault is unlocked and Privacy mode is off."""
    return unlocked() and not privacy_on()


def require_available():
    if privacy_on():
        raise HTTPException(423, "Privacy mode is on. Turn it off and unlock your personal vault first")
    if not is_set_up():
        raise HTTPException(423, "Set up your personal vault first (Privacy & Data)")
    if not unlocked():
        raise HTTPException(423, "Your personal vault is locked. Unlock it first")


def _retry_after(meta: dict) -> int:
    if not meta:
        return 0
    return max(0, min(MAX_WAIT, int(round(meta["retry_at"] - time.time()))))


def _wait_for(failures: int) -> int:
    return 0 if failures < FREE_TRIES else min(MAX_WAIT, BASE_WAIT * 2 ** (failures - FREE_TRIES))


def lock(reason: str = "locked"):
    """Drops the key. Safe to call any time (from any thread)."""
    global _aead
    with _state_lock:
        _gen[0] += 1
        was = _aead is not None
        # The only long-lived reference to the key object, so it is freed here. Python can't promise that no copy
        # of the key is left in freed memory, so this is best effort.
        _aead = None
    for s in _stores:
        if s["on_lock"]:
            try:
                s["on_lock"]()
            except Exception as e:
                log.warning("Vault store %s: on_lock failed (%s)", s["name"], type(e).__name__)
    if was:
        log.info("Personal vault locked (%s)", reason)


def _set_key(aead):
    global _aead
    with _state_lock:
        _aead = aead
        _unlocked_at[0] = time.monotonic()
        _unlocked_day[0] = date.today().isoformat()


def unlocked_day() -> str:
    """Local date (ISO) the vault was unlocked on ("" while locked)."""
    with _state_lock:
        return _unlocked_day[0] if _aead is not None else ""


def unlocked_for() -> float:
    """Seconds since the vault was unlocked (0 while locked)."""
    with _state_lock:
        return time.monotonic() - _unlocked_at[0] if _aead is not None else 0.0


def legacy_counts() -> dict:
    out = {}
    for s in _stores:
        if s["legacy_count"]:
            try:
                out[s["name"]] = int(s["legacy_count"]())
            except Exception:
                out[s["name"]] = 0
    return out


def status() -> dict:
    meta = _meta()
    return {"set_up": meta is not None, "unlocked": unlocked(), "privacy": privacy_on(),
            "available": meta is not None and unlocked() and not privacy_on(),
            "retry_after": _retry_after(meta), "min_length": MIN_LENGTH, "legacy": legacy_counts()}


def _migrate() -> dict:
    # One at a time (two unlocks in a row never move the same old copies twice). The old rows' original pages
    # (e.g. personal_events' clear dates) pass through the rollback journal, so it is scrubbed afterwards.
    with _migrate_lock:
        if not any(s["legacy_count"] and _safe_count(s) for s in _stores):
            return _migrate_locked()  # nothing in SQLite to move (leftover Credential Manager entries are still cleaned)
        with db.scrubbed_journal():
            return _migrate_locked()


def _safe_count(store) -> int:
    try:
        return int(store["legacy_count"]() or 0)
    except Exception:
        return 1  # unknown: let migrate() try


def _migrate_locked() -> dict:
    moved = {}
    for s in _stores:
        if not s["migrate"]:
            continue
        try:
            n = int(s["migrate"]() or 0)
        except Exception as e:  # keep the old copies; try again on the next unlock
            log.warning("Personal vault: moving %s in failed (%s); will retry on next unlock", s["name"], type(e).__name__)
            n = 0
        if n:
            moved[s["name"]] = n
            log.info("Personal vault: moved %d %s item(s) in and deleted the old copies", n, s["name"])
    return moved


# ---- records (other modules use these) ------------------------------------------------------------

def put(kind: str, payload: dict, rid: str | None = None, also=None) -> str:
    """Encrypts and stores one record. Holds the state lock and the database lock throughout, so it can't
    interleave with a passphrase change (the key it encrypts with is the one in place when it commits).
    also(conn): optional extra statements committed in the same transaction (e.g. deleting the old copy)."""
    rid = rid or secrets.token_hex(12)
    if not re.fullmatch(r"[0-9a-f]{8,64}", rid):
        raise ValueError("Bad record id")
    _tables()
    with _state_lock, db._lock:
        aead = _current()
        blob = _seal(aead, _encode({"kind": kind, "data": payload}), _record_aad(rid))
        c = db._db()
        try:
            c.execute("INSERT OR REPLACE INTO vault_records (id, blob) VALUES (?, ?)", (rid, blob))
            if also:
                also(c)
            c.commit()
        except BaseException:
            c.rollback()
            raise
    return rid


def _decrypt_all(aead, failed: list | None = None) -> list[tuple[str, dict]]:
    """Every record this key opens. Records that don't (damaged, or not from this vault) are skipped and logged by
    a short id prefix; pass `failed` to collect their ids."""
    from cryptography.exceptions import InvalidTag
    _tables()
    with db._lock:
        rows = db._db().execute("SELECT id, blob FROM vault_records").fetchall()
    out = []
    for r in rows:
        try:
            out.append((r["id"], json.loads(_unseal(aead, r["blob"], _record_aad(r["id"])).decode("utf-8"))))
        except (InvalidTag, ValueError):
            log.warning("Personal vault: record %s failed its integrity check; skipped", r["id"][:6])
            if failed is not None:
                failed.append(r["id"])
    return out


def records(kind: str) -> list[dict]:
    """Every record of a kind as {"id": ..., **payload}. Needs the vault unlocked."""
    aead = _current()
    return [{**rec["data"], "id": rid} for rid, rec in _decrypt_all(aead) if rec.get("kind") == kind]


def get(kind: str, rid: str) -> dict | None:
    from cryptography.exceptions import InvalidTag
    aead = _current()
    if not isinstance(rid, str) or not re.fullmatch(r"[0-9a-f]{8,64}", rid):
        return None
    _tables()
    with db._lock:
        row = db._db().execute("SELECT blob FROM vault_records WHERE id = ?", (rid,)).fetchone()
    if not row:
        return None
    try:
        rec = json.loads(_unseal(aead, row["blob"], _record_aad(rid)).decode("utf-8"))
    except (InvalidTag, ValueError):
        return None
    return {**rec["data"], "id": rid} if rec.get("kind") == kind else None


def delete(kind: str, rid: str) -> bool:
    if get(kind, rid) is None:  # also checks the vault is unlocked
        return False
    with db._lock, secure_delete() as c:
        c.execute("DELETE FROM vault_records WHERE id = ?", (rid,))
        c.commit()
    return True


def record_count() -> int:
    _tables()
    with db._lock:
        return db._db().execute("SELECT COUNT(*) FROM vault_records").fetchone()[0]


def counts() -> dict:
    """Records per kind (needs the vault unlocked; otherwise only the total is known)."""
    aead = _current()
    out: dict[str, int] = {}
    for _, rec in _decrypt_all(aead):
        out[rec.get("kind", "?")] = out.get(rec.get("kind", "?"), 0) + 1
    return out


# ---- set up / unlock / change / reset ---------------------------------------------------------------

def setup(passphrase: str, confirm: str) -> dict:
    with _attempt_lock:
        if is_set_up():
            raise HTTPException(409, "Your personal vault is already set up")
        if not isinstance(confirm, str) or not hmac.compare_digest(passphrase.encode("utf-8", "replace"), confirm.encode("utf-8", "replace")):
            raise HTTPException(400, "The two passphrases don't match")
        problem = passphrase_problem(passphrase)
        if problem:
            raise HTTPException(400, problem)
        with _state_lock:
            gen, privacy_at_start = _gen[0], privacy_on()
        salt = os.urandom(16)
        aead = _derive(passphrase, salt, KDF)
        verifier = _seal(aead, VERIFIER, VERIFIER_AAD)
        _tables()
        with db._lock:
            db._db().execute("INSERT INTO vault_meta (id, format, salt, kdf, verifier, created, failures, retry_at)"
                             " VALUES (1, ?, ?, ?, ?, ?, 0, 0)", (FORMAT, salt, json.dumps(KDF), verifier, db.now()))
            db._db().commit()
        log.info("Personal vault set up")
        _take_key(aead, gen, privacy_at_start, "set up")
    return {"moved": _migrate()}


class LockedMeanwhile(HTTPException):
    def __init__(self, what: str):
        super().__init__(409, f"The vault was locked (Lock, Privacy mode, Windows lock or sleep) while {what}, "
                              "so it stays locked. Unlock it again.")


def _take_key(aead, gen: int, privacy_at_start: bool, what: str):
    """Installs a freshly derived key, unless the vault was locked or Privacy mode was turned on since the request
    began (generation changed): then the key is dropped and LockedMeanwhile raised.
    Privacy mode is cleared only if it was already on when the request began. The unlock dialog says so ("Privacy
    mode is on. Unlocking turns it off"), so that is Joe's explicit choice; a Privacy mode turned on during the
    passphrase check changes the generation, so it is never undone here."""
    with _state_lock:
        if _gen[0] != gen:
            log.info("Personal vault: %s finished after a lock; the new key was dropped", what)
            raise LockedMeanwhile("your passphrase was being checked")
        _set_key(aead)
        if privacy_at_start and privacy_on():
            _privacy["on"] = False
            db.kv_set("privacy:mode", False)
            log.info("Privacy mode off (unlocked)")


def _check(passphrase: str):
    """Verifies the passphrase (rate limited). Returns (meta, aead). Caller holds _attempt_lock."""
    meta = _meta()
    if not meta:
        raise HTTPException(409, "Set up your personal vault first")
    wait = _retry_after(meta)
    if wait:
        raise HTTPException(429, f"Too many wrong passphrases. Try again in {wait} seconds")
    if not isinstance(passphrase, str) or not passphrase or len(passphrase) > MAX_LENGTH:
        raise HTTPException(400, "Enter your passphrase")
    try:
        aead = _derive(passphrase, bytes(meta["salt"]), json.loads(meta["kdf"]))
        ok = _verifier_ok(aead, bytes(meta["verifier"]))
    except Exception as e:
        log.warning("Personal vault: unlock error (%s)", type(e).__name__)
        raise HTTPException(500, "Couldn't check the passphrase")
    if not ok:
        failures = meta["failures"] + 1
        wait = _wait_for(failures)
        _set_attempts(failures, time.time() + wait if wait else 0)
        log.warning("Personal vault: wrong passphrase (%d in a row)", failures)
        left = FREE_TRIES - failures
        hint = f" Next try in {wait} seconds." if wait else (f" {left} more {'try' if left == 1 else 'tries'} before a wait." if left <= 3 else "")
        raise HTTPException(401, "Wrong passphrase." + hint)
    if meta["failures"] or meta["retry_at"]:
        _set_attempts(0, 0)
    return meta, aead


def unlock(passphrase: str) -> dict:
    if not _attempt_lock.acquire(blocking=False):
        raise HTTPException(429, "Already checking a passphrase. One moment")
    try:
        with _state_lock:
            gen, privacy_at_start = _gen[0], privacy_on()
        _, aead = _check(passphrase)
        _take_key(aead, gen, privacy_at_start, "unlock")
        log.info("Personal vault unlocked")
    finally:
        _attempt_lock.release()
    return {"moved": _migrate()}


def change(current: str, passphrase: str, confirm: str) -> dict:
    if not _attempt_lock.acquire(blocking=False):
        raise HTTPException(429, "Already checking a passphrase. One moment")
    try:
        gen = generation()
        _, old = _check(current)
        if not isinstance(confirm, str) or not hmac.compare_digest(passphrase.encode("utf-8", "replace"), confirm.encode("utf-8", "replace")):
            raise HTTPException(400, "The two new passphrases don't match")
        if hmac.compare_digest(passphrase.encode("utf-8", "replace"), current.encode("utf-8", "replace")):
            raise HTTPException(400, "The new passphrase is the same as the current one")
        problem = passphrase_problem(passphrase)
        if problem:
            raise HTTPException(400, problem)
        salt = os.urandom(16)
        new = _derive(passphrase, salt, KDF)
        # Snapshot, re-encrypt, commit and key swap under the state lock and the database lock: put() takes the
        # same locks, so no record can be written with the old key after the snapshot (it would be unreadable).
        # BEGIN IMMEDIATE also keeps another process (an older copy still running) from writing in between.
        _tables()
        with _state_lock, db._lock:
            c = db._db()
            if c.in_transaction:
                c.commit()
            c.execute("BEGIN IMMEDIATE")
            try:
                failed: list[str] = []
                items = _decrypt_all(old, failed)
                if failed:
                    ids = ", ".join(f[:6] for f in failed[:10]) + (f" +{len(failed) - 10} more" if len(failed) > 10 else "")
                    log.warning("Personal vault: passphrase change stopped; %d record(s) can't be decrypted", len(failed))
                    raise HTTPException(409, f"Nothing was changed: {len(failed)} vault record(s) can't be decrypted "
                                             f"with your current passphrase (damaged, or not from this vault): {ids}. "
                                             "Changing the passphrase would make them unreadable for good.")
                rows = [(_seal(new, _encode(rec), _record_aad(rid)), rid) for rid, rec in items]
                c.executemany("UPDATE vault_records SET blob = ? WHERE id = ?", rows)
                c.execute("UPDATE vault_meta SET salt = ?, kdf = ?, verifier = ?, failures = 0, retry_at = 0 WHERE id = 1",
                          (salt, json.dumps(KDF), _seal(new, VERIFIER, VERIFIER_AAD)))
                c.commit()
            except BaseException:
                c.rollback()
                raise
            # Same key swap rule as unlock: if the vault was locked meanwhile it stays locked (with the new key).
            stays_locked = _gen[0] != gen
            if not stays_locked:
                _set_key(new)
        log.info("Personal vault passphrase changed; %d record(s) re-encrypted", len(items))
        return {"ok": True, "reencrypted": len(items), "locked": stays_locked}
    finally:
        _attempt_lock.release()


def reset(confirm: str) -> dict:
    if not isinstance(confirm, str) or confirm.strip().upper() != RESET_PHRASE:
        raise HTTPException(400, f'Type {RESET_PHRASE} to confirm')
    with _attempt_lock:
        lock("reset")
        _tables()
        with db._lock, secure_delete() as c:
            n = c.execute("SELECT COUNT(*) FROM vault_records").fetchone()[0]
            c.execute("DELETE FROM vault_records")
            c.execute("DELETE FROM vault_meta")
            c.commit()
        for s in _stores:
            if s["wipe_legacy"]:
                try:
                    s["wipe_legacy"]()
                except Exception as e:
                    log.warning("Vault store %s: wiping old copies failed (%s)", s["name"], type(e).__name__)
    log.warning("Personal vault reset: %d record(s) and all old personal copies deleted", n)
    return {"ok": True, "deleted": n}


# ---- routes ---------------------------------------------------------------------------------------
# Passphrases arrive only in POST bodies, are never logged, stored or echoed, and are dropped after use.

@router.get("/status")
async def vault_status():
    import asyncio
    return await asyncio.to_thread(status)


@router.post("/setup")
async def vault_setup(passphrase: str = Body(...), confirm: str = Body(...)):
    import asyncio
    result = await asyncio.to_thread(setup, passphrase, confirm)
    return {"ok": True, **result, "status": await asyncio.to_thread(status)}


@router.post("/unlock")
async def vault_unlock(passphrase: str = Body(..., embed=True)):
    import asyncio
    result = await asyncio.to_thread(unlock, passphrase)
    return {"ok": True, **result, "status": await asyncio.to_thread(status)}


@router.post("/lock")
async def vault_lock():
    lock("Lock button")
    return {"ok": True, "status": status()}


@router.post("/change")
async def vault_change(current: str = Body(...), passphrase: str = Body(...), confirm: str = Body(...)):
    import asyncio
    result = await asyncio.to_thread(change, current, passphrase, confirm)
    return {**result, "status": await asyncio.to_thread(status)}


@router.post("/reset")
async def vault_reset(confirm: str = Body(..., embed=True)):
    import asyncio
    result = await asyncio.to_thread(reset, confirm)
    return {**result, "status": await asyncio.to_thread(status)}
