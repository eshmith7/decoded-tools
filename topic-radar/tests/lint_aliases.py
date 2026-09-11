"""Flag topic aliases that are ordinary English words.

A single-word alias that is also an everyday noun matches headlines about
something else entirely, and the topic's demand figures quietly absorb them.
"boat", the alias the classifier generated for boAt, matched "22 migrants
rescued from a disabled boat near Tripoli".

Sector and macro topics are exempt: for "insurance" or "recession" the
everyday word genuinely is the subject. Companies and people are not.

Uses the system word list when there is one, so it is a developer's check
rather than a build gate — GitHub's runners have no /usr/share/dict/words.
Run with: python3 tests/lint_aliases.py
"""

import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
TOPICS = os.path.join(HERE, "..", "poller", "topics.yml")
DICTS = ("/usr/share/dict/words", "/usr/dict/words")

# In the word list but unmistakable as subjects: countries, and brands whose
# dictionary sense no business headline would mean.
EXEMPT = set("""china india japan russia israel iran nepal britain trump kodak
parle vedanta nestle amazon google elon marlboro tobacco semiconductor
antibiotic""".split())


def main() -> int:
    path = next((p for p in DICTS if os.path.exists(p)), None)
    if not path:
        print("no system word list; skipping (this check is advisory)")
        return 0
    words = {w.strip().lower() for w in open(path) if w.strip()}
    topics = yaml.safe_load(open(TOPICS))["topics"]

    bad = []
    for t in topics:
        if t["category"] not in ("company", "person"):
            continue
        for a in t["aliases"]:
            low = a.lower()
            if " " in a or low in EXEMPT:
                continue
            if low in words:
                bad.append((t["slug"], a))

    if bad:
        print(f"{len(bad)} alias(es) are everyday English words:\n")
        for slug, a in bad:
            print(f"  {slug}: {a!r} — qualify it, e.g. "
                  f"{a!r} -> '{a} company'")
        return 1
    print(f"checked {sum(len(t['aliases']) for t in topics)} aliases across "
          f"{len(topics)} topics — none collide with everyday words")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
