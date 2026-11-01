"""Stable field tracking across noisy vision frames (pure logic, no I/O).

- Pages are cached by signature (page_type + title + label set, fuzzy). Field ORDER is fixed the first time
  a page is seen; later detections only update boxes / filled / focus for known labels (new labels are
  slotted in after their nearest known predecessor).
- The target is a label key, never an index.
- The target is done only when it shows a value in 2 consecutive detections, or on Next.
- Focus on a different (unfilled) field in 2 consecutive detections = the user chose it → switch target.
- Each field is explained once; a short check-in after 30 s without progress, once per field.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from backend.vision import SeenField, VisionResult

FILLED_FRAMES_TO_ADVANCE = 2
FOCUS_FRAMES_TO_SWITCH = 2
CHECK_IN_AFTER_S = 30.0
SAME_PAGE_MIN_OVERLAP = 0.5


def label_key(label: str) -> str:
    return " ".join(re.sub(r"[*:]", " ", label).lower().split())


@dataclass
class Tracked:
    key: str
    seen: SeenField  # latest detection (label text, kind, marker)
    bbox: list[int] | None = None  # box in the CURRENT detection, None if not visible now
    filled_now: bool = False
    filled_streak: int = 0
    focus_streak: int = 0
    done: bool = False
    explained: bool = False
    checked_in: bool = False


@dataclass
class PageState:
    page_type: str
    title: str
    order: list[str] = field(default_factory=list)
    fields: dict[str, Tracked] = field(default_factory=dict)
    target: str | None = None
    all_done_said: bool = False

    def labels(self) -> set[str]:
        return set(self.order)


@dataclass
class Decision:
    target: Tracked | None  # field to highlight (may have bbox None if scrolled away)
    explain: bool = False  # speak the field instruction now
    switched_by_user: bool = False
    page_changed: bool = False
    all_done: bool = False  # nothing left on this page (say once)


class Tracker:
    def __init__(self) -> None:
        self.pages: list[PageState] = []
        self.page: PageState | None = None
        self.last_progress_t = 0.0

    # ---------------------------------------------------------------- pages
    def _match_page(self, r: VisionResult) -> PageState | None:
        keys = {label_key(f.label) for f in r.fields}
        title = (r.page_title or "").strip().lower()
        best, best_score = None, 0.0
        for p in self.pages:
            if p.page_type != r.page_type.value:
                continue
            union = keys | p.labels()
            overlap = len(keys & p.labels()) / len(union) if union else 0.0
            score = overlap + (0.5 if title and title == p.title else 0.0)
            if (overlap >= SAME_PAGE_MIN_OVERLAP or (title and title == p.title and overlap > 0)) and score > best_score:
                best, best_score = p, score
        return best

    def _merge(self, p: PageState, r: VisionResult) -> None:
        det = []  # this detection's keys, in reading order, de-duplicated
        for f in r.fields:
            k = label_key(f.label)
            if k and k not in det:
                det.append(k)
        for i, k in enumerate(det):
            if k in p.fields:
                continue
            # slot a new label next to its known neighbours in this detection; order of known labels never changes
            prev = next((d for d in reversed(det[:i]) if d in p.fields), None)
            nxt = next((d for d in det[i + 1:] if d in p.fields), None)
            pos = p.order.index(prev) + 1 if prev else (p.order.index(nxt) if nxt else len(p.order))
            p.order.insert(pos, k)
            p.fields[k] = Tracked(key=k, seen=next(f for f in r.fields if label_key(f.label) == k))
        by_key = {}
        for f in r.fields:
            by_key.setdefault(label_key(f.label), f)
        for k, t in p.fields.items():
            hit = by_key.get(k)
            if hit is None:
                t.bbox = None  # not visible now (scrolled): keep filled state, reset streaks
                t.focus_streak = 0
                continue
            t.seen = hit
            t.bbox = hit.box_2d
            t.filled_now = hit.filled
            t.filled_streak = t.filled_streak + 1 if hit.filled else 0
            t.focus_streak = t.focus_streak + 1 if hit.focused else 0

    # ---------------------------------------------------------------- main step
    def update(self, r: VisionResult, now: float) -> Decision:
        p = self._match_page(r)
        page_changed = p is not self.page
        if p is None:
            p = PageState(page_type=r.page_type.value, title=(r.page_title or "").strip().lower())
            self.pages.append(p)
        if page_changed:
            self.last_progress_t = now
        self.page = p
        before_done = {k for k, t in p.fields.items() if t.done}
        self._merge(p, r)

        # target done? (value seen in N consecutive detections)
        cur = p.fields.get(p.target) if p.target else None
        if cur and cur.filled_streak >= FILLED_FRAMES_TO_ADVANCE:
            cur.done = True

        switched = False
        # user focused another unfilled field for N detections → their choice wins
        focused = [t for t in p.fields.values() if t.focus_streak >= FOCUS_FRAMES_TO_SWITCH and not t.filled_now and not t.done]
        if focused and focused[0].key != p.target:
            p.target = focused[0].key
            switched = True
        elif cur is None or cur.done:
            p.target = self._next_target(p)

        if {k for k, t in p.fields.items() if t.done} != before_done or switched:
            self.last_progress_t = now

        t = p.fields.get(p.target) if p.target else None
        if t is None:
            all_done = bool(p.fields) and not p.all_done_said
            p.all_done_said = p.all_done_said or all_done
            return Decision(target=None, page_changed=page_changed, all_done=all_done)
        p.all_done_said = False
        explain = not t.explained
        t.explained = True
        if explain:
            self.last_progress_t = now
        return Decision(target=t, explain=explain, switched_by_user=switched, page_changed=page_changed)

    def _next_target(self, p: PageState) -> str | None:
        for k in p.order:
            t = p.fields[k]
            if not t.done and not t.filled_now:
                return k
        return None

    # ---------------------------------------------------------------- user actions
    def next(self, now: float) -> None:
        """User pressed Next: current target is done (skipped or finished)."""
        p = self.page
        if not p:
            return
        if p.target and p.target in p.fields:
            p.fields[p.target].done = True
        p.target = self._next_target(p)
        self.last_progress_t = now

    def current(self) -> Tracked | None:
        p = self.page
        return p.fields.get(p.target) if p and p.target else None

    def mark_unexplained(self) -> None:
        t = self.current()
        if t:
            t.explained = False

    def check_in_due(self, now: float) -> Tracked | None:
        t = self.current()
        if t and not t.checked_in and now - self.last_progress_t >= CHECK_IN_AFTER_S:
            t.checked_in = True
            return t
        return None
