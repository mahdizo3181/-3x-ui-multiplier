"""A production-shaped dataset for the scale tests and the benchmark: a big untouched inbound (#1) and two
multiplied tunnel inbounds (#2, #3) with hundreds of clients each, some on both."""
import random

import xui_mult as xm

K = {2: 1.2, 3: 1.5}


def build(env, per_inbound=600, overlap=100, noise=2000, seed=1):
    """#2 and #3 each get `per_inbound` clients, `overlap` of them on both (they pay the higher k); #1 gets `noise`
    clients nobody multiplies. Returns {email: [inbound ids]}."""
    members = {}
    rows = []
    for i in range(per_inbound):
        ids = [2, 3] if i < overlap else [2]
        members[f"t2-{i}"] = ids
    for i in range(per_inbound - overlap):
        members[f"t3-{i}"] = [3]
    for i in range(noise):
        members[f"n-{i}"] = [1]
    rng = random.Random(seed)
    for email, ids in members.items():
        rows.append((email, ids, rng.randrange(0, 10 ** 9), rng.randrange(0, 10 ** 10)))   # history: never billed
    for i in range(0, len(rows), 2000):
        env.seed_many(rows[i:i + 2000])
    return members


def traffic(members, share=1.0, seed=2, step=10):
    """One Xray report: [(email, up, down)] for `share` of the clients; multiples of `step` so that x1.2 and x1.5
    land on whole bytes and the expected extra is exact."""
    rng = random.Random(seed)
    return [(e, step * rng.randrange(0, 200_000), step * rng.randrange(0, 2_000_000))
            for e in members if rng.random() < share]


def expected_extra(members, deltas):
    """{email: (extra up, extra down)} that one tick must credit for these deltas."""
    out = {}
    for email, up, down in deltas:
        ks = [K[i] for i in members[email] if i in K]
        if ks:
            k = xm.mult_fp(max(ks)) - xm.SCALE
            out[email] = (up * k // xm.SCALE, down * k // xm.SCALE)
    return out
