"""V3 browser observations: accept cosmetic casing in accessible control names.

Keep v2 unchanged for historical replay. Comparisons still use Chromium-owned
AX nodes and the isolated world; missing or ambiguous controls still fail.
"""
from . import browser_worker_v2 as v2

DRIVER = v2.DRIVER.replace('async nodes(role,name,fold=false)',
                          'async nodes(role,name,fold=true)').replace(
                              'async one(role,name,fold=false)',
                              'async one(role,name,fold=true)')
# Playwright resolves DOM names while CDP can expose CSS-transformed names.
# Both interaction and authoritative observation must permit the same casing.
for label in ('Activity details', 'Close details', 'Status', 'Retry'):
    DRIVER = DRIVER.replace("name:'" + label + "',exact:true", "name:/^\\s*" + r'\s+'.join(label.split()) + "\\s*$/i")
DRIVER = DRIVER.replace("getByLabel('Search activities',{exact:true})",
                        r"getByLabel(/^\s*Search\s+activities\s*$/i)")
OBSERVER = DRIVER[DRIVER.index('class Observer'):DRIVER.index('async function visibleTitles')]


def program():
    return v2.program(DRIVER)
