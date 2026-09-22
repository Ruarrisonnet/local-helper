"""Generated corpora for bench.py, with ground truth computed as they are written.

The point of each generator is that the answer CANNOT be reached by grepping for a keyword: the
categories are written with phrasing banks that share no distinctive word with each other or with
the question. Every generator returns the truth it just planted, so graders never guess.

Run `python bench_fixture.py` to self-check (asserts that the naive grep gets the wrong answer).
"""
import random
import re

# ---------------------------------------------------------------- incidents (prose corpus)
# Five root causes. No phrasing contains a word that identifies its own category, and no two banks
# share a distinctive word, so "how many were caused by X" cannot be counted with grep.
CAUSES = {
    # Deliberately varied vocabulary: no noun or verb spans the whole bank, so no single word
    # identifies the category (enforced by best_keyword_f1 in demo()).
    "db_pool": [
        "every worker sat waiting for a free handle to the primary store, and new requests queued behind them",
        "the service ran dry of sessions against the main database and each arriving request joined the back of the queue",
        "all of the write master's client slots were checked out, so work piled up waiting for one to come back",
        "requests stacked up because nothing could reach the datastore until an earlier caller released its place",
        "the fixed set of cursors was entirely in use, and the wait to acquire one grew without bound",
        "acquiring a link to the relational tier became the bottleneck; callers blocked until somebody finished",
        "no spare channel to the records service remained, so each new request waited on an older one",
        "the database's client allowance was used up and the backlog of waiters grew until callers gave up",
    ],
    "bad_deploy": [
        "the change that went out that morning had an inverted condition in the routing rules",
        "a release shipped with a mistyped field name, so the response body came back empty",
        "the version promoted at 09:00 removed a guard that downstream callers still depended on",
        "a build went out with a stale template, and every rendered page referenced a removed helper",
        "the rollout carried a wrong default, so the service answered with an empty result set",
        "the newly promoted artefact reversed the order of two arguments in the pricing call",
        "the change shipped without its companion migration, so reads referenced a column that did not exist",
        "a release replaced a working code path with one that had never been exercised outside a test",
    ],
    "disk_full": [
        "the volume backing the write-ahead area reached capacity and further writes were rejected",
        "the partition holding temporary artefacts had no room left, so every save failed",
        "log rotation had stopped weeks earlier and the device filled to its last free block",
        "the mount used for uploads had nothing left to allocate and the writer errored on every attempt",
        "free space on the data device fell to zero, so the service could not persist anything new",
        "the archive directory grew unchecked until the underlying device could take no more bytes",
        "no free blocks remained on the volume, and the writer failed on its first flush of the hour",
        "the staging area ran out of room, so each attempt to persist a record was rejected outright",
    ],
    "memory_leak": [
        "the process grew steadily over four days until the supervisor terminated it",
        "resident size climbed without bound because a cache never evicted its oldest entries",
        "the worker's footprint doubled every day until the kernel reaped it",
        "an unbounded queue retained every message it had ever seen, and the process was eventually killed",
        "retained objects accumulated across requests until the runtime could allocate nothing further",
        "the service's footprint crept upward for a week and it was terminated by the platform",
        "a listener list grew on every reconnect and was never pruned, exhausting the process",
        "the long-lived worker held on to each response it produced until it was shut down for excess use",
    ],
    "upstream_slow": [
        "the partner service answered far slower than usual and every call waited the full allowance",
        "a third party degraded, so each outbound request burned its whole waiting allowance before returning",
        "the vendor's endpoint took thirty times longer than normal and callers blocked on it",
        "responses from the external provider crawled, and our workers spent all their time waiting",
        "the remote dependency slowed to a crawl and each of our calls sat until it gave up",
        "an external system's latency rose sharply, and our requests waited out their full allowance",
        "the provider we call for enrichment became unresponsive for minutes at a time",
        "calls out to the partner took so long that every worker was parked waiting on one",
    ],
}
# Real post-incident reports name the hypotheses they ruled out, and that is also what stops a single
# keyword from counting the categories: every distinctive word appears in incidents it did not cause.
RULED_OUT = {
    "db_pool": "We first suspected the primary store had run out of free handles and that requests were "
               "queueing for one, but the handle counts were flat throughout.",
    "bad_deploy": "The change released that morning was the obvious suspect, and we rolled it back "
                  "early, but the errors continued unchanged afterwards.",
    "disk_full": "An early theory was that the volume had filled up and writes were being rejected, "
                 "though free space never dropped below 40%.",
    "memory_leak": "We thought at first that the process was growing without bound and being "
                   "terminated, but its footprint was steady all week.",
    "upstream_slow": "It looked at first like the partner service had slowed down and our calls were "
                     "waiting out their allowance, yet their latency was normal throughout.",
}
IMPACTS = ["checkout was unavailable", "search returned errors", "the dashboard failed to load",
           "uploads were rejected", "sign-in stalled", "the mobile app showed stale data",
           "report generation stopped", "webhooks were not delivered"]
ACTIONS = ["rolled back and paged the owning team", "restarted the affected workers",
           "shifted traffic to the secondary region", "raised the limit and redeployed",
           "cleared the backlog by hand", "disabled the feature flag", "scaled out and drained the queue"]
DETECTED = ["a customer report", "the on-call alert", "a dashboard anomaly", "an automated probe",
            "a partner's complaint", "the error-rate monitor"]


def make_incidents(path, n_incidents=130, seed=11):
    """Post-incident write-ups, one per '## INC-' heading. Returns ({cause: count}, [label per incident])."""
    rnd = random.Random(seed)
    labels, out = [], []
    for i in range(n_incidents):
        label = rnd.choice(list(CAUSES))
        labels.append(label)
        out.append(f"## INC-{1000 + i}")
        out.append("")
        out.append(f"**Detected by:** {rnd.choice(DETECTED)} at "
                   f"{rnd.randint(0, 23):02d}:{rnd.randint(0, 59):02d} UTC on day {rnd.randint(1, 28)}.")
        out.append("")
        out.append(f"**Impact:** for {rnd.randint(4, 90)} minutes, {rnd.choice(IMPACTS)}.")
        out.append("")
        body = (rnd.choice(CAUSES[label]).capitalize() + ". The effect was visible to roughly "
                f"{rnd.randint(2, 80)}% of traffic in "
                f"{rnd.choice(['eu-west', 'us-east', 'ap-south', 'us-west'])}.")
        if rnd.random() < 0.55:          # a ruled-out hypothesis borrowed from a DIFFERENT category
            herring = RULED_OUT[rnd.choice([c for c in CAUSES if c != label])]
            body = f"{herring} {body}" if rnd.random() < 0.5 else f"{body} {herring}"
        out.append("**What happened:** " + body)
        out.append("")
        out.append(f"**Resolution:** we {rnd.choice(ACTIONS)}. Normal service resumed after "
                   f"{rnd.randint(5, 120)} minutes.")
        out.append("")
        out.append(f"**Follow-up:** ticket OPS-{rnd.randint(2000, 9999)} tracks the permanent fix.")
        out.append("")
    open(path, "w", encoding="utf-8").write("\n".join(out) + "\n")
    return {c: labels.count(c) for c in CAUSES}, labels


def best_keyword_f1(path, labels, target):
    """The strongest single word anyone could grep for, scored against the true incident set.

    A word is only a shortcut if the incidents containing it ARE the incidents with that cause, so
    score every word in the corpus by F1 and return the best. Near 1.0 means the task is greppable.
    """
    chunks = open(path, encoding="utf-8").read().split("## INC-")[1:]
    assert len(chunks) == len(labels), (len(chunks), len(labels))
    truth = {i for i, l in enumerate(labels) if l == target}
    per_incident = [set(re.findall(r"[a-z]{4,}", c.lower())) for c in chunks]
    best, word = 0.0, None
    for w in set().union(*per_incident):
        hits = {i for i, toks in enumerate(per_incident) if w in toks}
        tp = len(hits & truth)
        if not tp:
            continue
        prec, rec = tp / len(hits), tp / len(truth)
        f1 = 2 * prec * rec / (prec + rec)
        if f1 > best:
            best, word = f1, w
    return best, word


# ---------------------------------------------------------------- log (20k lines, unique errors)
# Two error meanings: "gave up after waiting" (the graded one) and everything else. Every emitted
# line is unique, and the waiting ones avoid the words a grep would reach for first.
WAITED_TOO_LONG = [
    "abandoned {what} for {who} after {n}s of waiting",
    "stopped waiting on {who} while fetching {what}; nothing came back in {n}s",
    "gave up on {what}: {who} had not answered after {n}s",
    "{who} never responded for {what}; the call was dropped at {n}s",
    "dropped {what} because {who} stayed silent for {n}s",
    "ended the wait for {who} on {what} once {n}s had passed with no reply",
]
OTHER_ERRORS = [
    "rejected {what} for {who}: the payload failed validation at field {n}",
    "{who} refused {what} with a permission error (rule {n})",
    "could not parse the response for {what} from {who}: unexpected token at byte {n}",
    "{what} failed for {who}: the record was already present (revision {n})",
    "checksum mismatch on {what} from {who}; {n} bytes differed",
    "{who} returned a malformed header while serving {what} (code {n})",
]
WHATS = ["the invoice export", "a profile lookup", "the pricing quote", "an inventory check",
         "the audit write", "a settlement batch", "the fraud score", "an address validation"]
WHOS = ["billing-api", "identity-svc", "catalogue", "risk-engine", "ledger", "notify-worker",
        "geo-resolver", "tax-service"]


def make_log(path, n_lines=20000, n_errors=900, seed=7):
    """A service log whose ERROR lines are all distinct free text.

    Returns {"waited": n, "other": n, "top_repeated": (msg, count)} - `waited` is the graded truth,
    `top_repeated` supports the old count-the-repeats task.
    """
    rnd = random.Random(seed)
    lines = [f"2026-09-{1 + i % 28:02d}T{i % 24:02d}:{i % 60:02d}:{i * 7 % 60:02d}Z INFO "
             f"req={rnd.getrandbits(64):016x} path=/api/v1/items/{rnd.randint(1, 9999)} "
             f"took={rnd.randint(1, 900)}ms" for i in range(n_lines)]
    slots = rnd.sample(range(n_lines), n_errors)
    waited = 0
    for k, slot in enumerate(slots):
        is_wait = k % 3 == 0                      # a third of the errors are the graded category
        waited += is_wait
        tmpl = rnd.choice(WAITED_TOO_LONG if is_wait else OTHER_ERRORS)
        msg = tmpl.format(what=rnd.choice(WHATS), who=rnd.choice(WHOS), n=rnd.randint(2, 120))
        lines[slot] = (f"2026-09-{1 + slot % 28:02d}T{slot % 24:02d}:{slot % 60:02d}:{slot * 7 % 60:02d}Z "
                       f"ERROR {msg} req={rnd.getrandbits(64):016x}")
    # One repeated message for the "which error repeats most" task. It must NOT belong to the
    # waited-too-long category: the old text ("db pool exhausted: waited 30s for a connection") did,
    # so both models counted it and were marked wrong against a truth that was itself wrong.
    repeated, n_rep = "ERROR config checksum mismatch on shard 4: refusing to serve", 57
    for slot in rnd.sample([i for i in range(n_lines) if i not in set(slots)], n_rep):
        lines[slot] = (f"2026-09-19T12:00:00Z {repeated} req={rnd.getrandbits(64):016x}")
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return {"waited": waited, "other": n_errors - waited, "top_repeated": (repeated, n_rep)}


# ---------------------------------------------------------------- noisy test script
FAIL_WORDINGS = [
    "case {n} did not match expectation (wanted {a}, saw {b})",
    "assertion broke in case {n}: {a} is not {b}",
    "case {n} -> MISMATCH: expected {a}, got {b}",
    "!! case {n} disagreed: {a} vs {b}",
    "case {n} came back wrong ({b} where {a} was required)",
    "check for case {n} was not satisfied: {a} != {b}",
    "case {n} produced {b}, which is not {a}",
]


def make_noisy_tests(path, n_cases=6000, n_fail=7, seed=23):
    """Write a chatty test script. Returns (n_fail, first_failing_case_number)."""
    rnd = random.Random(seed)
    failing = sorted(rnd.sample(range(1, n_cases + 1), n_fail))
    wordings = {c: rnd.choice(FAIL_WORDINGS).format(n=c, a=rnd.randint(10, 99), b=rnd.randint(100, 999))
                for c in failing}
    src = f'''"""A deliberately chatty test runner (generated fixture)."""
import sys

FAILING = {wordings!r}
N = {n_cases}

def main():
    failed = 0
    for i in range(1, N + 1):
        print(f"[case {{i:05d}}] setting up fixtures for scenario {{i % 37}}")
        print(f"[case {{i:05d}}] loading {{i % 11 + 1}} records, warming caches")
        print(f"[case {{i:05d}}] running assertions ({{i % 5 + 1}} of them)")
        if i in FAILING:
            print(FAILING[i])
            failed += 1
        else:
            print(f"[case {{i:05d}}] ok")
        print(f"[case {{i:05d}}] tearing down")
    print(f"ran {{N}} cases")
    sys.exit(1 if failed else 0)

main()
'''
    open(path, "w", encoding="utf-8").write(src)
    return n_fail, failing[0]


def demo():
    """Self-check: the planted truth must be right, and grep must get it WRONG."""
    import os
    import subprocess
    import sys
    import tempfile
    d = tempfile.mkdtemp(prefix="lh_fx_")

    inc = os.path.join(d, "incidents.md")
    truth, labels = make_incidents(inc, n_incidents=260, seed=11)
    text = open(inc, encoding="utf-8").read()
    assert text.count("## INC-") == 260, "wrong number of incidents"
    assert sum(truth.values()) == 260, truth
    # The whole point of the task: no single word identifies the db_pool incidents. Scored over
    # EVERY word in the corpus, not a list of guesses - an earlier version shared "handle" across
    # seven of eight phrasings, and both models got the exact answer by grepping for it.
    f1, word = best_keyword_f1(inc, labels, "db_pool")
    assert f1 < 0.6, f"prose task is greppable: {word!r} scores F1 {f1:.2f} against the true set"
    assert 30 < truth["db_pool"] < 80, truth

    log = os.path.join(d, "app.log")
    lt = make_log(log, n_lines=20000, n_errors=900, seed=7)
    ltext = open(log, encoding="utf-8").read()
    err_lines = [l for l in ltext.splitlines() if " ERROR " in l]
    assert len(err_lines) == 900 + 57, len(err_lines)
    assert lt["waited"] == 300, lt
    for word in ("timeout", "timed out", "deadline", "slow"):
        assert len(re.findall(word, ltext, re.I)) != lt["waited"], f"grep for {word!r} answers the log task"
    assert "waited" not in lt["top_repeated"][0], "the repeated line must not be a waited-too-long error"
    free = [l.split(" ERROR ", 1)[1].rsplit(" req=", 1)[0] for l in err_lines
            if lt["top_repeated"][0] not in l]
    assert len(set(free)) > 800, f"error lines are not distinct enough: {len(set(free))}"

    nt = os.path.join(d, "noisy_tests.py")
    n_fail, first = make_noisy_tests(nt, n_cases=300, n_fail=7, seed=23)
    r = subprocess.run([sys.executable, nt], capture_output=True, text=True, timeout=300)
    assert r.returncode == 1, r.returncode
    assert len(r.stdout.splitlines()) > 300 * 5, "not chatty enough"
    assert sum(1 for l in r.stdout.splitlines() if str(first) in l and "ok" not in l) >= 1
    for word in ("FAIL", "fail", "error", "ERROR"):
        assert word not in r.stdout, f"{word!r} appears: the noisy task would be greppable"
    print(f"ok: incidents {truth}, log waited={lt['waited']}, noisy {n_fail} fail first={first}")


if __name__ == "__main__":
    demo()
