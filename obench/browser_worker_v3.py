"""V3 browser observations: accept cosmetic casing in accessible control names.

Keep v2 unchanged for historical replay. Comparisons still use Chromium-owned
AX nodes and the isolated world; missing or ambiguous controls still fail.
"""
from . import browser_worker_v2 as v2

DRIVER = v2.DRIVER.replace('async nodes(role,name,fold=false)',
                          'async nodes(role,name,fold=true)').replace(
                              'async one(role,name,fold=false)',
                              'async one(role,name,fold=true)')
OBSERVER = DRIVER[DRIVER.index('class Observer'):DRIVER.index('async function visibleTitles')]


def program():
    return v2.program(DRIVER)
