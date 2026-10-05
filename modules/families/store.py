"""
store.py — persistence for families, request families and lead lists.

Durability rule, learned the hard way: JSON is the truth, Redis is a mirror.
Redis keys expire and get flushed; the lead list in particular must never lose
a row, so every write goes to disk first and Redis second. A write that
succeeds on only one side is logged loudly rather than silently accepted.

Layout under data/:
  families/families.json          every Family + RequestFamily
  leads/<family_id>.json          accepted ad records, deduped, never dropped
"""

import json
import logging
from pathlib import Path

from core import Family, RequestFamily, run_state_broker, safe_redis_operation

from genesis.config import FAMILIES_DIR, LEADS_DIR

logger = logging.getLogger(__name__)

FAMILIES_FILE = FAMILIES_DIR / "families.json"

FAMILY_KEY = "tesserae:family:{family_id}"
LEADS_KEY = "tesserae:leads:{family_id}"
LEADS_INDEX_KEY = "tesserae:leads:index"

# Lead lists are permanent by design — the user has seen these. The TTL is only
# a safety net against unbounded growth if a family is deleted.
LEADS_TTL_SECONDS = 365 * 24 * 3600


# ── helpers ──────────────────────────────────────────────────────────────────
def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def new_family_id(about: str) -> str:
    """Stable-ish, readable, and unique. Slug + short suffix."""
    import uuid

    slug = "-".join(
        ch for ch in "".join(c.lower() if c.isalnum() else " " for c in about).split() if ch
    )[:40] or "family"
    return f"fam_{slug}_{uuid.uuid4().hex[:6]}"


def new_request_family_id(family_id: str) -> str:
    import uuid

    return f"req_{family_id.removeprefix('fam_')}_{uuid.uuid4().hex[:6]}"


def _read_all() -> dict:
    if not FAMILIES_FILE.exists():
        return {"families": {}, "requests": {}}
    try:
        data = json.loads(FAMILIES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("❌ families.json unreadable (%s) — starting empty", exc)
        return {"families": {}, "requests": {}}
    data.setdefault("families", {})
    data.setdefault("requests", {})
    return data


def _write_all(data: dict) -> None:
    FAMILIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    FAMILIES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── families ─────────────────────────────────────────────────────────────────
def list_families() -> list[Family]:
    data = _read_all()
    return [Family(**raw) for raw in data["families"].values()]


def get_family(family_id: str) -> Family | None:
    raw = _read_all()["families"].get(family_id)
    return Family(**raw) if raw else None


def find_family_by_about(about: str) -> Family | None:
    """Used to avoid accidental duplicates without ever BLOCKING creation."""
    target = about.strip().lower()
    for fam in list_families():
        if fam.about.strip().lower() == target:
            return fam
    return None


def related_families(domain: str, specialisations: list[str]) -> list[Family]:
    """
    Families whose verdicts can seed a new node with this coverage.

    Shares at least one specialisation. Sharing a domain is far too loose —
    all of them are "Programming", and a frontend verdict says nothing about a
    data-science ad.
    """
    from .taxonomy import shares_scope

    return [
        f
        for f in list_families()
        if shares_scope(domain, specialisations, f.category_domain, f.specialisations)
    ]


def save_family(family: Family) -> Family:
    family.updated_at = _now()
    if not family.created_at:
        family.created_at = family.updated_at
    if not family.founding_text:
        family.founding_text = family.profile_text
    data = _read_all()
    data["families"][family.family_id] = family.model_dump(mode="json")
    _write_all(data)                      # disk first — the truth
    safe_redis_operation(                                  # mirror second
        run_state_broker.set, FAMILY_KEY.format(family_id=family.family_id),
        json.dumps(family.model_dump(mode="json"), ensure_ascii=False),
    )
    return family


def create_family(
    about: str,
    category_domain: str,
    specialisations: list[str],
    profile_text: str,
    seeded_from: list[str] | None = None,
) -> Family:
    return save_family(
        Family(
            family_id=new_family_id(about),
            about=about,
            category_domain=category_domain,
            specialisations=list(specialisations),
            profile_text=profile_text,
            founding_text=profile_text,
            seeded_from=list(seeded_from or []),
        )
    )


# ── request families ─────────────────────────────────────────────────────────
def list_request_families(family_id: str | None = None) -> list[RequestFamily]:
    data = _read_all()["requests"]
    out = [RequestFamily(**raw) for raw in data.values()]
    return [r for r in out if family_id is None or r.family_id == family_id]


def get_request_family(request_family_id: str) -> RequestFamily | None:
    raw = _read_all()["requests"].get(request_family_id)
    return RequestFamily(**raw) if raw else None


def save_request_family(request_family: RequestFamily) -> RequestFamily:
    request_family.updated_at = _now()
    if not request_family.created_at:
        request_family.created_at = request_family.updated_at
    if not request_family.founding_text:
        request_family.founding_text = request_family.request_text
    data = _read_all()
    data["requests"][request_family.request_family_id] = request_family.model_dump(mode="json")
    _write_all(data)
    return request_family


def create_request_family(
    family_id: str,
    request_text: str,
    seeded_from: list[str] | None = None,
) -> RequestFamily:
    return save_request_family(
        RequestFamily(
            request_family_id=new_request_family_id(family_id),
            family_id=family_id,
            request_text=request_text,
            founding_text=request_text,
            seeded_from=list(seeded_from or []),
        )
    )


# ── lead lists ───────────────────────────────────────────────────────────────
def leads_path(family_id: str) -> Path:
    return LEADS_DIR / f"{family_id}.json"


def load_leads(family_id: str) -> list[dict]:
    """The user's leads for one family. Never pruned, never duplicated."""
    path = leads_path(family_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("❌ lead list %s unreadable (%s)", path, exc)
        return []
    return data if isinstance(data, list) else []


def add_leads(family_id: str, ads: list[dict]) -> int:
    """
    Append leads, deduped by ad_archive_id. Returns how many were new.

    Disk first: this is the one store the user was told can never lose a row.
    """
    existing = load_leads(family_id)
    seen = {a.get("ad_archive_id") for a in existing}
    added = 0
    for ad in ads:
        ad_id = ad.get("ad_archive_id")
        if not ad_id or ad_id in seen:
            continue
        seen.add(ad_id)
        existing.append(ad)
        added += 1
    if not added:
        return 0
    leads_path(family_id).parent.mkdir(parents=True, exist_ok=True)
    leads_path(family_id).write_text(
        json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    safe_redis_operation(                     # mirror for fast membership checks
        run_state_broker.sadd,
        LEADS_KEY.format(family_id=family_id),
        *[a["ad_archive_id"] for a in existing],
    )
    safe_redis_operation(
        run_state_broker.expire, LEADS_KEY.format(family_id=family_id), LEADS_TTL_SECONDS
    )
    safe_redis_operation(run_state_broker.sadd, LEADS_INDEX_KEY, family_id)
    return added


def lead_ids(family_id: str) -> set[str]:
    """Membership from Redis, falling back to disk if the mirror is cold."""
    ids = safe_redis_operation(
        run_state_broker.smembers, LEADS_KEY.format(family_id=family_id)
    )
    if ids:
        return set(ids)
    return {a["ad_archive_id"] for a in load_leads(family_id) if a.get("ad_archive_id")}


# ── lineage ──────────────────────────────────────────────────────────────────
def ancestry(family_id: str, _seen: set[str] | None = None) -> list[str]:
    """
    This family's ancestors, nearest first, cycle-safe.

    Lineage is what makes seeding free: a new node names the families it
    descends from instead of copying thousands of verdict rows. Widening walks
    the ancestors and RE-JUDGES what they rejected; narrowing just inherits.
    """
    _seen = _seen or set()
    if family_id in _seen:
        return []
    _seen.add(family_id)
    fam = get_family(family_id)
    if not fam:
        return []
    out: list[str] = []
    for parent in fam.seeded_from:
        out.append(parent)
        out.extend(ancestry(parent, _seen))
    return out