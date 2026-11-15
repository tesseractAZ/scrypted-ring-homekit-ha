import glob, json, os, sys

# Pushing faults deliberately NOT recorded for the all-clear, with the reason.
NOT_RECORDED = {
    'camera_host_down_alert': 'reports an outage that had already ended when it pushed; nothing to clear',
}


def walk(node):
    """Yield (parent_list, index, action_dict) for every action, in list order."""
    if isinstance(node, list):
        for i, x in enumerate(node):
            if isinstance(x, dict):
                yield node, i, x
            yield from walk(x)
    elif isinstance(node, dict):
        for v in node.values():
            if isinstance(v, (list, dict)):
                yield from walk(v)


autos = {os.path.basename(f)[:-5]: json.load(open(f)) for f in sorted(glob.glob('ha/automations/*.json'))}
bad, recorded, dismissed = [], {}, set()
for a in autos.values():
    for _, _, x in walk(a.get('actions', [])):
        if x.get('action') == 'persistent_notification.dismiss':
            dismissed.add((x.get('data') or {}).get('notification_id'))
for aid, a in autos.items():
    acts = a.get('actions', [])
    created = {(x.get('data') or {}).get('notification_id') for _, _, x in walk(acts)
               if x.get('action') == 'persistent_notification.create'}
    pushes = [(lst, i) for lst, i, x in walk(acts) if str(x.get('action', '')).startswith('notify.')]
    for _, _, x in walk(acts):
        if x.get('event') == 'camera_alert_pushed':
            recorded[(x.get('event_data') or {}).get('card')] = aid
    clearable = created & dismissed
    if aid == 'camera_all_clear' or aid in NOT_RECORDED or not pushes or not clearable:
        continue                      # never pushes, excluded, or its card is never cleared (the walk test)
    for lst, i in pushes:
        nxt = lst[i + 1] if i + 1 < len(lst) else {}
        card = (nxt.get('event_data') or {}).get('card') if nxt.get('event') == 'camera_alert_pushed' else None
        if card not in clearable:
            bad.append(f'{aid}: its push is not followed by camera_alert_pushed for its card {sorted(clearable)}')
for aid in NOT_RECORDED:
    if aid in autos and any(x.get('event') == 'camera_alert_pushed' for _, _, x in walk(autos[aid].get('actions', []))):
        bad.append(f'{aid}: listed as not recorded, yet it records')
ac = autos.get('camera_all_clear')
if ac is None:
    bad.append('camera_all_clear.json is missing')
else:
    decide = json.dumps(ac.get('actions', []))
    for card, aid in sorted(recorded.items()):
        if f'\\"{card}\\"' not in decide and f"'{card}'" not in decide:
            bad.append(f'{aid}: records card {card!r}, which has no recovery test in camera_all_clear (it would close only by the 7-day fallback)')
for b in bad:
    print(b, file=sys.stderr)
print(f'all-clear wiring: {len(recorded)} pushing faults recorded, each with a recovery test' if not bad else 'all-clear wiring: BROKEN')
sys.exit(1 if bad else 0)
