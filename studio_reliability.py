"""Observed orchestration counts, with explicit measurement boundaries."""
from collections import Counter
from contextlib import closing


COUNTERS = ('duplicateEvents', 'duplicateReports', 'manualReconciliations')


def increment(db, fid, name, now, amount=1):
    if name not in COUNTERS:
        raise ValueError('Unknown reliability counter.')
    db.execute('INSERT INTO reliability_counters VALUES(?,?,?,?,?) ON CONFLICT(fleet,name) DO UPDATE SET count=count+excluded.count,last_at=excluded.last_at',
               (fid, name, amount, now, now))


def summary(progress, fid, handoffs, reports):
    with progress.lock, closing(progress.connect()) as db:
        rows = db.execute('SELECT * FROM reliability_counters WHERE fleet=?', (fid,)).fetchall()
    counters = {r['name']: r['count'] for r in rows}
    blocked = [r for r in reports if r.get('blockedSince') and r.get('bindingCurrent')]
    return {'countersSince': min((r['first_at'] for r in rows), default=None),
            'counts': {k: counters.get(k) for k in COUNTERS},
            'currentDeliveries': dict(Counter(h['checkpoint']['delivery'] for h in handoffs)),
            'currentSubmittedObligations': sum(h['checkpoint']['work'] == 'submitted' and not h.get('continuationOf') for h in handoffs),
            'currentBlockerObservedSeconds': sum(r.get('waitingObservedSeconds', 0) for r in blocked) if blocked else None,
            'blockerObservationGaps': any(r.get('waitingObservationGap') or progress.clock() - r['at'] > 180 for r in blocked),
            'notice': 'Counters start when this version begins observing the fleet. Delivery counts describe retained obligations, not lifetime totals. '
                      'Blocker time includes only bounded observed intervals for current blockers; downtime and earlier history are unknown. '
                      'These measurements do not establish acceptance, exactly-once execution, saved time or monetary cost.'}

