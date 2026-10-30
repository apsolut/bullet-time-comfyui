"""Hosted (public) mode: who pays for a Comfy Cloud job, and the guards around the operator's key.
Ported from one-shot-multicam (same author, MIT): the live demos work the same way.

Three ways to pay, chosen per request (header `X-Pay-Mode`: key | promo | trial):
  key    the visitor's own Comfy Cloud key (`X-Comfy-Key`); never stored or logged
  promo  a promo code (`X-Judge-Code`) spends the operator's key, capped per code (BULLETTIME_JUDGE_CODE with
         BULLETTIME_JUDGE_MAX_JOBS; more codes in BULLETTIME_PROMO_CODES="CODE:LIMIT,CODE:LIMIT"). Codes are
         compared in constant time and only their hashes land on disk; remove a code from the env to revoke it
  trial  one free run per visitor on the operator's key (BULLETTIME_HOSTED_RUNS = size of the pool,
         BULLETTIME_TRIAL_JOBS_PER_RUN = Comfy jobs one run may use), enforced by a salted IP hash AND a
         first-party cookie. The pool counter lives in <data>/hosted_jobs.json; raise HOSTED_RUNS to top up
Access to the operator key: BULLETTIME_HOSTED_ACCESS = "judge" (promo codes and trial runs only; the default)
or "open" (anyone without a key, capped by BULLETTIME_MAX_JOBS and BULLETTIME_IP_JOBS_PER_DAY).
BULLETTIME_HOSTED_FLOOR_USD is the hard stop for all of them: a job is refused when the operator's real balance
would drop below it (asked from Comfy Cloud, so it survives restarts and a lost volume).
Jobs are reserved before a run starts and refunded for whatever never reached Comfy Cloud.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

from . import comfy

VISITOR_COOKIE = "bt_vid"  # random first-party id for the one-free-run-per-visitor rule


def _int(v, default=None):
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        return default


def _float(v, default=None):
    try:
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


class Hosted:
    def __init__(self, data_dir: str | Path, env: dict | None = None):
        """All settings come from BULLETTIME_* env vars (see the module docstring); `env` overrides for tests."""
        E = (env or os.environ).get
        self.data = Path(data_dir)
        self.data.mkdir(parents=True, exist_ok=True)
        self.counter_file = self.data / "hosted_jobs.json"
        self.lock = threading.Lock()
        self.env = env
        self.hosted_key_env = E("BULLETTIME_HOSTED_KEY_ENV", "COMFY_API_KEY")
        self.hosted_access = E("BULLETTIME_HOSTED_ACCESS", "judge")
        if self.hosted_access not in ("open", "judge"):
            self.hosted_access = "judge"
        self.judge_max_jobs = _int(E("BULLETTIME_JUDGE_MAX_JOBS"), 10)
        self.max_jobs = _int(E("BULLETTIME_MAX_JOBS"))  # open mode: total jobs on the operator key (None = no cap)
        self.trial_runs = max(0, _int(E("BULLETTIME_HOSTED_RUNS"), 0))
        self.trial_jobs_per_run = max(1, _int(E("BULLETTIME_TRIAL_JOBS_PER_RUN"), 4))
        self.ip_jobs_per_day = _int(E("BULLETTIME_IP_JOBS_PER_DAY"))
        self.ip_projects_per_hour = _int(E("BULLETTIME_IP_PROJECTS_PER_HOUR"))
        self.hosted_floor_usd = _float(E("BULLETTIME_HOSTED_FLOOR_USD"))
        self.proxy_hops = _int(E("BULLETTIME_PROXY_HOPS"), 0)
        self.ttl_minutes = _float(E("BULLETTIME_TTL_MINUTES"))
        self.public = E("BULLETTIME_PUBLIC") == "1"

    def _env(self, name: str) -> str:
        return ((self.env or os.environ).get(name) or "").strip()

    # ---------- the operator's key and who may use it
    @property
    def operator_key(self) -> str | None:
        return self._env(self.hosted_key_env) or None

    @property
    def hosted(self) -> bool:
        """Open hosted mode: any visitor without a key uses the operator key (capped)."""
        return bool(self.operator_key) and self.hosted_access == "open"

    @property
    def judge_enabled(self) -> bool:
        return bool(self.operator_key) and bool(self.promo_codes())

    @property
    def trial_enabled(self) -> bool:
        return bool(self.operator_key) and self.trial_runs > 0

    @property
    def any_free(self) -> bool:
        return self.hosted or self.judge_enabled or self.trial_enabled

    # ---------- promo codes
    @staticmethod
    def promo_id(code: str) -> str:
        """Ledger key of a promo code: a hash, so plaintext codes never land on the data volume."""
        return hashlib.sha256(("bullettime-promo:" + code.strip()).encode()).hexdigest()[:16]

    def promo_codes(self) -> dict[str, tuple[str, int]]:
        """-> {ledger id: (code, job limit)}. BULLETTIME_JUDGE_CODE keeps the id "judge" (limit
        judge_max_jobs); BULLETTIME_PROMO_CODES = "CODE:LIMIT,CODE:LIMIT" adds more (limit optional, default
        judge_max_jobs). Codes shorter than 8 characters are ignored."""
        out: dict[str, tuple[str, int]] = {}
        legacy = self._env("BULLETTIME_JUDGE_CODE")
        if len(legacy) >= 8:
            out["judge"] = (legacy, int(self.judge_max_jobs))
        for item in self._env("BULLETTIME_PROMO_CODES").replace("\n", ",").split(","):
            code, _, lim = item.strip().partition(":")
            code, lim = code.strip(), lim.strip()
            if len(code) < 8 or code == legacy:
                continue
            limit = _int(lim, int(self.judge_max_jobs)) if lim else int(self.judge_max_jobs)
            if limit is not None and limit > 0:
                out[self.promo_id(code)] = (code, limit)
        return out

    def promo_match(self, code: str | None) -> str | None:
        """Ledger id of a valid promo code, else None (constant-time compare against every code)."""
        if not (self.judge_enabled and code and code.strip()):
            return None
        c, hit = code.strip().encode(), None
        for pid, (real, _lim) in self.promo_codes().items():
            if hmac.compare_digest(c, real.encode()):
                hit = pid
        return hit

    def promo_limit(self, pid: str) -> int:
        return self.promo_codes().get(pid, ("", 0))[1]

    def promo_used(self, pid: str = "judge", d: dict | None = None) -> int:
        d = self._ledger() if d is None else d
        return int(d.get("judge_jobs", 0)) if pid == "judge" else int(d.get("promo_jobs", {}).get(pid, 0))

    @staticmethod
    def _promo_set(d: dict, pid: str, v: int):
        if pid == "judge":
            d["judge_jobs"] = v
        else:
            d.setdefault("promo_jobs", {})[pid] = v

    # ---------- ledger (hosted_jobs.json on the data volume)
    def _ledger(self) -> dict:
        d = json.loads(self.counter_file.read_text()) if self.counter_file.exists() else {}
        d.setdefault("jobs", 0)
        d.setdefault("ip_jobs", {})
        d.setdefault("ip_projects", {})
        return d

    def _save(self, d: dict):
        tmp = self.counter_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1))
        tmp.replace(self.counter_file)

    @staticmethod
    def _ip_id(ip: str) -> str:
        return hashlib.sha256(("bullettime:" + ip).encode()).hexdigest()[:16]  # never store raw IPs

    @staticmethod
    def _cookie_id(cookie: str | None) -> str | None:
        return hashlib.sha256(("bullettime-vid:" + cookie).encode()).hexdigest()[:16] if cookie else None

    @staticmethod
    def new_cookie() -> str:
        return secrets.token_hex(12)

    def client_ip(self, request) -> str:
        """Behind N trusted proxies, the N-th address from the right of X-Forwarded-For (Fly: 1)."""
        if self.proxy_hops:
            xff = [x.strip() for x in request.headers.get("x-forwarded-for", "").split(",") if x.strip()]
            if len(xff) >= self.proxy_hops:
                return xff[-self.proxy_hops]
        return request.client.host if request.client else "?"

    def _floor_ok(self, n: int) -> bool:
        if self.hosted_floor_usd is None:
            return True
        try:
            bal = comfy.balance_usd(self.operator_key)
        except Exception:  # noqa: BLE001
            bal = None
        return bal is not None and bal - n * comfy.COST_PER_JOB_USD >= self.hosted_floor_usd

    # ---------- free trial runs
    def _trial(self, d: dict) -> dict:
        t = d.setdefault("trial", {})
        for k, v in (("used", 0), ("ips", {}), ("cookies", {}), ("jobs", {})):
            t.setdefault(k, v)
        return t

    def trial_status(self, ip: str, cookie: str | None) -> dict | None:
        if not self.trial_enabled:
            return None
        t = self._trial(self._ledger())
        mine = t["ips"].get(self._ip_id(ip)) or t["cookies"].get(self._cookie_id(cookie) or "-")
        return {"runs_total": self.trial_runs, "runs_left": max(0, self.trial_runs - int(t["used"])),
                "used_by_you": bool(mine), "jobs_per_run": self.trial_jobs_per_run}

    def charge_trial(self, n: int, ip: str, cookie: str | None, pkey: str):
        """Reserve n jobs of a free run. The first paid action on a project starts the visitor's run; later
        actions on the same project (a re-roll, an upscale) share it."""
        with self.lock:
            d = self._ledger()
            t = self._trial(d)
            ipk, ck = self._ip_id(ip), self._cookie_id(cookie)
            owner = t["ips"].get(ipk) or (t["cookies"].get(ck) if ck else None)
            if owner and owner != pkey:
                raise PermissionError("you've used your free run (one per visitor). Paste your own Comfy Cloud key "
                                      "to keep going (platform.comfy.org → API keys), or use a promo code")
            if pkey not in t["jobs"] and int(t["used"]) >= self.trial_runs:
                raise PermissionError(f"all {self.trial_runs} free runs are used up. Paste your own Comfy Cloud key "
                                      "(platform.comfy.org → API keys), or use a promo code")
            used = int(t["jobs"].get(pkey, 0))
            if used + n > self.trial_jobs_per_run:
                raise PermissionError(f"a free run covers up to {self.trial_jobs_per_run} Comfy jobs ({used} used on "
                                      f"this project, this needs {n}). Paste your own key for more")
            if not self._floor_ok(n):
                raise PermissionError("the free runs are used up. Paste your own Comfy Cloud key to keep going")
            if pkey not in t["jobs"]:
                t["used"] = int(t["used"]) + 1
            t["jobs"][pkey] = used + n
            t["ips"][ipk] = pkey
            if ck:
                t["cookies"][ck] = pkey
            self._save(d)

    def refund_trial(self, n: int, pkey: str):
        """Give back unsubmitted trial jobs; a run with nothing submitted is released entirely."""
        if n <= 0:
            return
        with self.lock:
            d = self._ledger()
            t = self._trial(d)
            if pkey not in t["jobs"]:
                return
            left = int(t["jobs"][pkey]) - n
            if left > 0:
                t["jobs"][pkey] = left
            else:
                del t["jobs"][pkey]
                t["used"] = max(0, int(t["used"]) - 1)
                for m in (t["ips"], t["cookies"]):
                    for k in [k for k, v in m.items() if v == pkey]:
                        del m[k]
            self._save(d)

    # ---------- operator-key budget (open mode and promo codes)
    def hosted_used(self) -> int:
        return self._ledger()["jobs"]

    def charge_hosted(self, n: int, ip: str = "?", promo: str | None = None):
        """Atomically reserve n jobs of the operator's budget. Promo-code jobs count against their own code
        (no per-IP cap); open-mode jobs against the global cap and the per-IP daily cap. Floor applies to both."""
        with self.lock:
            d = self._ledger()
            if promo:
                used, limit = self.promo_used(promo, d), self.promo_limit(promo)
                if limit <= 0:
                    raise PermissionError("that promo code isn't valid any more. Paste your own Comfy Cloud key")
                if used + n > limit:
                    raise PermissionError(f"this promo code is used up ({used}/{limit} jobs). "
                                          "Paste your own Comfy Cloud key to keep going")
            elif self.max_jobs is not None and d["jobs"] + n > self.max_jobs:
                raise PermissionError(f"the public demo budget is used up ({d['jobs']}/{self.max_jobs} jobs). "
                                      "Paste your own Comfy Cloud key to keep going")
            if not self._floor_ok(n):
                raise PermissionError("the public demo budget is used up. Paste your own Comfy Cloud key to keep going")
            if promo:
                self._promo_set(d, promo, self.promo_used(promo, d) + n)
                self._save(d)
                return
            k = self._ip_id(ip)
            ts = [t for t in d["ip_jobs"].get(k, []) if time.time() - t < 86400]
            if self.ip_jobs_per_day is not None and len(ts) + n > self.ip_jobs_per_day:
                raise PermissionError(f"rate limit: {self.ip_jobs_per_day} demo jobs per visitor per day "
                                      f"({len(ts)} used). Paste your own key for more")
            d["jobs"] += n
            d["ip_jobs"][k] = ts + [time.time()] * n
            self._save(d)

    def refund_hosted(self, n: int, ip: str = "?", promo: str | None = None):
        """Give back jobs that were reserved but never reached Comfy Cloud."""
        if n <= 0:
            return
        with self.lock:
            d = self._ledger()
            if promo:
                self._promo_set(d, promo, max(0, self.promo_used(promo, d) - n))
            else:
                d["jobs"] = max(0, d["jobs"] - n)
                k = self._ip_id(ip)
                d["ip_jobs"][k] = d["ip_jobs"].get(k, [])[:-n]
            self._save(d)

    def check_project_quota(self, ip: str, record: bool = False):
        """Per-IP upload rate limit on the public site; record=True after a valid upload."""
        if not (self.public and self.ip_projects_per_hour):
            return
        with self.lock:
            d = self._ledger()
            k = self._ip_id(ip)
            ts = [t for t in d["ip_projects"].get(k, []) if time.time() - t < 3600]
            if not record and len(ts) >= self.ip_projects_per_hour:
                raise PermissionError(f"rate limit: {self.ip_projects_per_hour} uploads per hour on the public demo; "
                                      "try again later or run it yourself (it's open source)")
            if record:
                d["ip_projects"][k] = ts + [time.time()]
                self._save(d)

    # ---------- which key pays
    PAY_MODES = {"key": "key", "promo": "promo", "judge": "promo", "trial": "trial", "free": "trial"}

    def resolve_key(self, header_key: str | None, code: str | None = None, pay_mode: str | None = None) -> tuple[str, str]:
        """-> (key, how): how = "key" (the visitor's own), "open" (operator key, open mode), "promo:<id>"
        (operator key via a promo code) or "trial" (a free run).
        pay_mode (header X-Pay-Mode) is the visitor's explicit choice and always wins: "key" never falls back
        to the operator's credits, "promo" never to a free run, "trial" never charges the visitor's key.
        Without it (CLI, API users): own key > open demo > promo code > free trial."""
        own = (header_key or "").strip()
        code = (code or "").strip()
        mode = self.PAY_MODES.get((pay_mode or "").strip().lower()) if pay_mode else None
        if pay_mode and not mode:
            raise PermissionError(f"unknown payment mode {pay_mode!r} (key, promo or trial)")
        if mode == "key":
            if not own:
                raise PermissionError("you chose your own Comfy Cloud key, but none was sent from this browser. "
                                      "Paste it again (platform.comfy.org → API keys)")
            return own, "key"
        if mode == "promo":
            if not code:
                raise PermissionError("you chose a promo code, but none was sent. Enter it again")
            return self._promo_key(code)
        if mode == "trial":
            if self.hosted:
                return self.operator_key, "open"
            if self.trial_enabled:
                return self.operator_key, "trial"
            raise PermissionError("free runs aren't available here. Paste your own Comfy Cloud key "
                                  "(platform.comfy.org → API keys)")
        if own:
            return own, "key"
        if self.hosted:
            return self.operator_key, "open"
        if code:
            return self._promo_key(code)
        if self.trial_enabled:
            return self.operator_key, "trial"
        raise PermissionError("Comfy Cloud API key required: paste your own key "
                              "(get one at platform.comfy.org → API keys)")

    def _promo_key(self, code: str) -> tuple[str, str]:
        pid = self.promo_match(code)
        if pid:
            return self.operator_key, "promo:" + pid
        raise PermissionError("that promo code isn't valid. Paste your own Comfy Cloud key instead "
                              "(platform.comfy.org → API keys)")

    def charge(self, how: str, n: int, ip: str, cookie: str | None, pkey: str):
        """Reserve n jobs for a paid action paid `how` (from resolve_key). The visitor's own key costs nothing here."""
        if how == "key":
            return
        if how == "trial":
            self.charge_trial(n, ip, cookie, pkey)
        else:
            self.charge_hosted(n, ip, promo=how[6:] if how.startswith("promo:") else None)

    def refund(self, how: str, n: int, ip: str, pkey: str):
        if how == "key" or n <= 0:
            return
        if how == "trial":
            self.refund_trial(n, pkey)
        else:
            self.refund_hosted(n, ip, promo=how[6:] if how.startswith("promo:") else None)

    def config(self, ip: str, cookie: str | None) -> dict:
        """What the web app needs to offer the right payment choices."""
        return {"hosted": self.hosted,
                "hosted_jobs_left": (max(0, self.max_jobs - self.hosted_used()) if self.hosted and self.max_jobs is not None
                                     else None),
                "trial": self.trial_status(ip, cookie), "judge_code": self.judge_enabled, "public": self.public,
                "byok_only": not self.any_free, "get_key_url": "https://platform.comfy.org/profile/api-keys"}
