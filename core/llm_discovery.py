"""
================================================================================
LLM DISCOVERY - ask a chatbot who the creators are, then verify every answer
================================================================================
Queries ChatGPT / duck.ai / Perplexity / Gemini through a real browser and turns
their answers into audited Instagram creators.

WHY A BROWSER AND NOT AN API
  All of these are free only through their web UI. A plain HTTP client does not
  work: duck.ai returns HTTP 418 ERR_CHALLENGE because its `x-vqd-hash-1` header
  is obfuscated JavaScript that has to be executed, and ChatGPT needs a logged-in
  session. A browser solves both for free - the challenge runs natively, and a
  persistent profile keeps the login across runs.

THE PART THAT MAKES LLM OUTPUT ACTUALLY USABLE
  Chatbots mostly answer with NAMES, and the handles they do give are frequently
  wrong or invented - they are a language model, not a directory. So nothing here
  is trusted:

    1. pull both @handles and person names out of the answer
    2. resolve every name to real handles via Instagram's own topsearch
       (verified: "Soham Sinha Kolkata Blogger" -> @kolkatadelites,
        "Bong Eats" -> @bongeats, "Indrani Banerjee Kolkata" -> @indranibanerjee22)
    3. write everything as a PENDING_AUDIT lead with source "llm"
    4. the audit phase resolves the exact follower count and decides

  An LLM never contributes a number, only a name to go and check.

ONE-TIME SETUP, DONE BY YOU
  ChatGPT, Perplexity and Gemini need a login, and duck.ai shows a terms dialog.
  This module will not accept terms or log in on its own:

    python core/llm_discovery.py login chatgpt      # opens a real window, you log in
    python core/llm_discovery.py login duckai       # you click Continue once

  The profile is saved under llm_profiles/<provider> and reused headless after
  that. Pass --accept-terms to let a run click a consent dialog itself.

USAGE
  python core/llm_discovery.py ask duckai  --region kolkata --categories food
  python core/llm_discovery.py ask chatgpt --region punjab --categories fashion --resolve
  python core/llm_discovery.py resolve "Soham Sinha" "Bong Eats"
================================================================================
"""

import sys, os, re, json, time
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Iterable

sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from playwright.sync_api import sync_playwright

from core import creator_db as cdb
from core.discovery_sources import (
    REGIONS, CATEGORIES, LLM_PROMPT_TEMPLATES, clean_handle, topsearch_candidates,
)

PROFILE_ROOT = os.path.join(BASE_DIR, "llm_profiles")
ANSWERS_FILE = os.path.join(BASE_DIR, "llm_discovery_answers.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ==============================================================================
# PROVIDERS
# ==============================================================================
# `composer` selectors are tried in order. `consent` is only ever clicked when
# the caller passes accept_terms=True.
PROVIDERS: Dict[str, Dict[str, Any]] = {
    "duckai": {
        "label": "duck.ai (DuckDuckGo)",
        "url": "https://duck.ai",
        "needs_login": False,
        "composer": ["textarea", "[contenteditable='true']"],
        "consent": ["Continue", "Get Started", "I Agree", "Agree", "Accept"],
        "submit": "Enter",
    },
    "chatgpt": {
        "label": "ChatGPT",
        "url": "https://chatgpt.com/",
        "needs_login": True,
        "composer": ["#prompt-textarea", "[contenteditable='true']", "textarea"],
        "consent": ["Okay, let's go", "Continue", "Stay logged out", "Accept all"],
        "submit": "Enter",
    },
    "perplexity": {
        "label": "Perplexity",
        "url": "https://www.perplexity.ai/",
        "needs_login": True,
        "composer": ["textarea", "[contenteditable='true']"],
        "consent": ["Accept", "Continue", "Got it"],
        "submit": "Enter",
    },
    "gemini": {
        "label": "Google Gemini",
        "url": "https://gemini.google.com/app",
        "needs_login": True,
        "composer": ["[contenteditable='true']", "textarea", "rich-textarea"],
        "consent": ["Got it", "I agree", "Continue", "Accept all"],
        "submit": "Enter",
    },
}


def profile_dir(provider: str) -> str:
    d = os.path.join(PROFILE_ROOT, provider)
    os.makedirs(d, exist_ok=True)
    return d


# ==============================================================================
# BROWSER DRIVER
# ==============================================================================
def _dismiss_consent(page, cfg: Dict[str, Any], accept_terms: bool) -> bool:
    """
    Returns True if a consent dialog is still blocking the page.

    Consent dialogs are only clicked when the caller explicitly opted in - a tool
    should not be agreeing to anyone's terms of service on its own initiative.
    """
    blocked = False
    for label in cfg.get("consent", []):
        try:
            btn = page.get_by_role("button", name=re.compile(f"^{re.escape(label)}", re.I))
            if not btn.count():
                continue
            if accept_terms:
                btn.first.click(timeout=2500)
                page.wait_for_timeout(1500)
            else:
                blocked = True
        except Exception:
            continue
    return blocked


def _find_composer(page, cfg: Dict[str, Any]):
    for sel in cfg["composer"]:
        try:
            loc = page.locator(sel)
            if loc.count():
                return loc.first
        except Exception:
            continue
    return None


def _wait_for_answer(page, settle_rounds: int = 3, max_wait: int = 90) -> str:
    """Polls the page text until it stops growing - these UIs stream their output."""
    last, stable, waited = "", 0, 0
    while waited < max_wait:
        page.wait_for_timeout(2000)
        waited += 2
        try:
            txt = page.inner_text("body")
        except Exception:
            continue
        if txt == last and len(txt) > 0:
            stable += 1
            if stable >= settle_rounds:
                break
        else:
            stable = 0
        last = txt
    return last


def ask_via_browser(prompts: List[str], provider: str = "duckai",
                    headless: bool = True, accept_terms: bool = False,
                    pause: float = 8.0) -> Dict[str, Any]:
    """Puts each prompt to one provider and returns the raw answers."""
    if provider not in PROVIDERS:
        raise SystemExit(f"unknown provider '{provider}'. known: {', '.join(PROVIDERS)}")
    cfg = PROVIDERS[provider]
    out = {"provider": provider, "answers": [], "status": "ok", "asked_at": now_iso()}

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            profile_dir(provider), headless=headless, user_agent=UA,
            viewport={"width": 1400, "height": 950}, locale="en-US")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(cfg["url"], wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)

            if _dismiss_consent(page, cfg, accept_terms):
                out["status"] = "consent_required"
                out["note"] = (
                    f"{cfg['label']} is showing a terms/consent dialog. This tool will not "
                    f"accept terms for you. Either run:\n"
                    f"    python core/llm_discovery.py login {provider}\n"
                    f"and click through it once, or re-run with --accept-terms.")
                ctx.close()
                return out

            body = page.inner_text("body")[:3000].lower()
            if cfg["needs_login"] and re.search(r'\b(log in|sign up|create an account)\b', body):
                composer_present = _find_composer(page, cfg) is not None
                if not composer_present:
                    out["status"] = "login_required"
                    out["note"] = (
                        f"{cfg['label']} needs a logged-in session. Run:\n"
                        f"    python core/llm_discovery.py login {provider}\n"
                        f"log in once in the window that opens, then re-run. The profile "
                        f"persists under llm_profiles/{provider}.")
                    ctx.close()
                    return out

            for i, prompt in enumerate(prompts, 1):
                box = _find_composer(page, cfg)
                if box is None:
                    out["status"] = "no_composer"
                    out["note"] = (f"Could not find the input box on {cfg['label']}. "
                                   f"Their UI likely changed - update PROVIDERS['{provider}']"
                                   f"['composer'].")
                    break
                try:
                    box.click(timeout=8000)
                    box.fill(prompt)
                except Exception:
                    try:
                        box.click(timeout=5000)
                        page.keyboard.type(prompt[:1200], delay=4)
                    except Exception as e:
                        out["answers"].append({"prompt": prompt, "answer": "",
                                               "error": f"{type(e).__name__}"})
                        continue
                page.wait_for_timeout(600)
                page.keyboard.press(cfg["submit"])
                print(f"  [{i}/{len(prompts)}] asked, waiting for the answer ...",
                      flush=True)

                text = _wait_for_answer(page)
                answer = text
                head = prompt[:60]
                idx = answer.rfind(head)
                if idx >= 0:
                    answer = answer[idx + len(head):]
                out["answers"].append({"prompt": prompt, "answer": answer,
                                       "chars": len(answer), "at": now_iso()})
                print(f"      got {len(answer)} chars")

                # Start each prompt in a fresh chat so answers do not bleed together.
                try:
                    page.goto(cfg["url"], wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(2500)
                    _dismiss_consent(page, cfg, accept_terms)
                except Exception:
                    pass
                time.sleep(pause)
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    return out


def login(provider: str) -> None:
    """Opens a real window so you can log in or accept terms once."""
    if provider not in PROVIDERS:
        raise SystemExit(f"unknown provider '{provider}'. known: {', '.join(PROVIDERS)}")
    cfg = PROVIDERS[provider]
    print("=" * 72)
    print(f"LOGIN / CONSENT SETUP - {cfg['label']}")
    print("=" * 72)
    print("A browser window is opening. Log in, or click through any consent dialog.")
    print("Everything is saved to this profile and reused headless afterwards:")
    print(f"  {profile_dir(provider)}")
    print("\nPress Enter here when you are done.")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            profile_dir(provider), headless=False, user_agent=UA,
            viewport={"width": 1400, "height": 950}, locale="en-US")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(cfg["url"], wait_until="domcontentloaded", timeout=60000)
        try:
            input()
        except EOFError:
            print("(no stdin - close the window when done)")
            time.sleep(120)
        ctx.close()
    print(f"[OK] profile saved. Now run: "
          f"python core/llm_discovery.py ask {provider} --region kolkata")


# ==============================================================================
# EXTRACTION - handles AND names
# ==============================================================================
IG_URL_RE = re.compile(r'instagram\.com/([A-Za-z0-9_.]{3,30})', re.I)
AT_HANDLE_RE = re.compile(r'@([A-Za-z0-9_.]{3,30})')

# "1. Soham Sinha", "- Indrani Banerjee (@x)", "**Bong Eats**"
NAME_LINE_RE = re.compile(
    r'^\s*(?:\d{1,2}[\.\)]\s*|[-*•]\s*)?\*{0,2}'
    r'([A-Z][A-Za-z.\'\-]+(?:\s+[A-Z][A-Za-z.\'\-]+){0,3})\*{0,2}'
    r'\s*(?:[\-–:(]|$)')

NAME_STOPWORDS = {
    "instagram", "kolkata", "west bengal", "the", "note", "here", "these", "their",
    "food", "creators", "bloggers", "influencers", "list", "top", "please", "however",
    "disclaimer", "keep", "make", "you", "they", "some", "many", "for", "and", "this",
    "follow", "based", "handle", "handles", "name", "names", "real", "exact",
}


def extract_leads(answer: str) -> Dict[str, List[str]]:
    """Splits an LLM answer into claimed handles and claimed person names."""
    handles, names = [], []

    for m in IG_URL_RE.finditer(answer):
        h = clean_handle(m.group(1))
        if h:
            handles.append(h)
    for m in AT_HANDLE_RE.finditer(answer):
        h = clean_handle(m.group(1))
        if h:
            handles.append(h)

    for raw_line in answer.splitlines():
        line = raw_line.strip()
        if len(line) < 3 or len(line) > 160:
            continue
        m = NAME_LINE_RE.match(line)
        if not m:
            continue
        cand = m.group(1).strip()
        if len(cand) < 4 or " " not in cand:
            continue
        if cand.lower() in NAME_STOPWORDS:
            continue
        if any(w in cand.lower().split() for w in ("instagram", "handle", "note")):
            continue
        names.append(cand)

    return {"handles": list(dict.fromkeys(handles)),
            "names": list(dict.fromkeys(names))}


# ==============================================================================
# NAME -> HANDLE, via Instagram's own search
# ==============================================================================
def resolve_names_to_handles(names: Iterable[str], region: Optional[str] = None,
                             per_name: int = 3, pause: float = 2.0,
                             on_progress=None) -> Dict[str, List[Dict[str, Any]]]:
    """
    Turns names an LLM produced into real Instagram handles.

    The region token is appended to the query because it sharpens the match a lot
    ("Soham Sinha Kolkata Blogger" -> @kolkatadelites). Everything returned is a
    CANDIDATE - the audit phase decides whether it is real and big enough.
    """
    token = ""
    if region and region in REGIONS:
        token = " " + REGIONS[region]["search_tokens"][0]

    out: Dict[str, List[Dict[str, Any]]] = {}
    for i, name in enumerate(names, 1):
        q = f"{name}{token}".strip()
        res = topsearch_candidates(q)
        hits = []
        if res["status"] == "ok":
            for u in res["users"][:per_name]:
                hits.append({"username": u["username"], "full_name": u["full_name"],
                             "is_verified": u["is_verified"], "matched_query": q})
        out[name] = hits
        if on_progress:
            on_progress(f"  [{i}] {name:32s} -> "
                        f"{[h['username'] for h in hits] or res['status']}")
        time.sleep(pause)
    return out


# ==============================================================================
# BUILD PROMPTS
# ==============================================================================
def build_prompts(region: str, categories: Optional[List[str]] = None,
                  n: int = 30) -> List[str]:
    cfg = REGIONS.get(region, REGIONS["kolkata"])
    cats = categories or ["food", "fashion", "lifestyle"]
    prompts = []
    for cat in cats:
        cat_label = CATEGORIES[cat]["label"].split(" & ")[0].lower()
        for tpl in LLM_PROMPT_TEMPLATES:
            prompts.append(tpl.format(n=n, category=cat_label, region=cfg["label"]))
    return prompts


# ==============================================================================
# END TO END
# ==============================================================================
def run(provider: str, region: str, categories: Optional[List[str]] = None,
        n: int = 30, headless: bool = True, accept_terms: bool = False,
        resolve: bool = True, max_prompts: int = 4) -> Dict[str, Any]:
    prompts = build_prompts(region, categories, n)[:max_prompts]
    print("=" * 72)
    print(f"LLM DISCOVERY  provider={provider}  region={region}  "
          f"categories={categories or 'default'}")
    print(f"               {len(prompts)} prompt(s), headless={headless}")
    print("=" * 72)

    res = ask_via_browser(prompts, provider=provider, headless=headless,
                          accept_terms=accept_terms)
    if res["status"] != "ok":
        print(f"\n[{res['status'].upper()}]\n{res.get('note', '')}")
        return res

    conn = cdb.connect()
    all_handles, all_names = [], []
    for a in res["answers"]:
        leads = extract_leads(a.get("answer", ""))
        a["extracted"] = leads
        all_handles += leads["handles"]
        all_names += leads["names"]
    all_handles = list(dict.fromkeys(all_handles))
    all_names = list(dict.fromkeys(all_names))

    print(f"\nclaimed handles: {len(all_handles)}")
    print(f"claimed names  : {len(all_names)}")

    added = 0
    for h in all_handles:
        cdb.add_candidate_only(conn, h, "llm", detail=f"{provider}:stated_handle",
                               region=region)
        added += 1

    resolved_map: Dict[str, List[Dict[str, Any]]] = {}
    if resolve and all_names:
        print(f"\nresolving {len(all_names)} names through Instagram search ...")
        resolved_map = resolve_names_to_handles(all_names, region, on_progress=print)
        for name, hits in resolved_map.items():
            for hit in hits:
                cdb.add_candidate_only(conn, hit["username"], "llm",
                                       detail=f"{provider}:name:{name[:40]}",
                                       region=region)
                added += 1
    conn.commit()

    payload = {"provider": provider, "region": region, "at": now_iso(),
               "answers": res["answers"], "handles": all_handles,
               "names": all_names, "resolved": resolved_map}
    existing = []
    if os.path.exists(ANSWERS_FILE):
        try:
            existing = json.load(open(ANSWERS_FILE, encoding="utf-8"))
        except Exception:
            existing = []
    existing.append(payload)
    json.dump(existing, open(ANSWERS_FILE, "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)

    s = cdb.stats(conn)
    conn.close()
    print(f"\n[SAVED] raw answers -> {ANSWERS_FILE}")
    print(f"[DB] {added} lead rows written with source 'llm' "
          f"(pending audit: {s['creators_pending_audit']:,})")
    print(f"\nnext: python core/regional_engine.py audit --region {region} "
          f"--sources-min 2 --limit 200")
    return payload


# ==============================================================================
# CLI
# ==============================================================================
def _cli():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        print(f"Providers: {', '.join(PROVIDERS)}")
        print(f"Regions:   {', '.join(REGIONS)}")
        return

    cmd = args[0]

    if cmd == "login":
        if len(args) < 2:
            print(f"usage: login <{'|'.join(PROVIDERS)}>")
            return
        login(args[1])
        return

    if cmd == "resolve":
        names = [a for a in args[1:] if not a.startswith("--")]
        region = None
        if "--region" in args:
            region = args[args.index("--region") + 1]
        if not names:
            print('usage: resolve "Name One" "Name Two" [--region kolkata]')
            return
        conn = cdb.connect()
        res = resolve_names_to_handles(names, region, on_progress=print)
        added = 0
        for name, hits in res.items():
            for h in hits:
                cdb.add_candidate_only(conn, h["username"], "llm",
                                       detail=f"manual:name:{name[:40]}", region=region or "")
                added += 1
        conn.commit()
        conn.close()
        print(f"\n{added} candidate handles written as 'llm' leads")
        return

    if cmd == "ask":
        if len(args) < 2:
            print(f"usage: ask <{'|'.join(PROVIDERS)}> --region R [--categories a,b]")
            return
        provider = args[1]
        opts: Dict[str, Any] = {"region": "kolkata"}
        i = 2
        rest = args[2:]
        while i - 2 < len(rest):
            a = rest[i - 2]
            nxt = rest[i - 1] if i - 1 < len(rest) else None
            if a == "--region":
                opts["region"] = nxt; i += 2
            elif a == "--categories":
                opts["categories"] = [x.strip() for x in nxt.split(",") if x.strip()]; i += 2
            elif a == "--prompts":
                opts["max_prompts"] = int(nxt); i += 2
            elif a == "--n":
                opts["n"] = int(nxt); i += 2
            elif a == "--headful":
                opts["headless"] = False; i += 1
            elif a == "--accept-terms":
                opts["accept_terms"] = True; i += 1
            elif a == "--no-resolve":
                opts["resolve"] = False; i += 1
            else:
                print(f"unknown option: {a}")
                return
        run(provider, **opts)
        return

    print(f"unknown command: {cmd}")


if __name__ == "__main__":
    _cli()
