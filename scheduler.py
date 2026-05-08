"""
scheduler.py — Timeline planner + Claude-based clip matcher.

Core constraints enforced here
────────────────────────────────
  • Every group has exactly 3-5 consecutive 4-second clips  (GROUP_MIN/MAX)
  • A blank gap of 4-8 s follows each group                 (GAP_MIN/MAX)
  • Groups + gaps must continuously cover the full voiceover duration
  • No clip ID may appear more than once in a single video
  • Clips in a group should be semantically coherent with each other
    AND with the voiceover topic playing at that moment
"""

import json
import math
import random
import re
from dataclasses import dataclass, field

import openai

import config
import library


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class GroupPlan:
    """Structural slot before clips are assigned."""
    index: int
    clip_count: int          # 3-5
    gap_duration: float      # 4-8 s (blank after this group)
    vo_start: float          # voiceover timestamp this group starts at
    vo_end: float            # voiceover timestamp this group ends at (incl. gap)
    vo_topic: str = ""       # dominant topic label from transcript
    vo_text: str  = ""       # actual voiceover words during this window

@dataclass
class ScheduledGroup:
    """Fully resolved group ready for assembly."""
    index: int
    clip_ids: list[str]
    clip_paths: list[str]
    gap_duration: float
    vo_start: float
    vo_end: float
    vo_topic: str


# ── Step 1: Plan the structural timeline (pure math) ──────────────────────────

def plan_timeline(total_duration: float, available_clip_count: int) -> list[GroupPlan]:
    """
    Produce a sequence of GroupPlan objects whose clip durations + gaps
    sum to approximately total_duration.

    Constraint: each group is 3-5 clips × 4 s, followed by 4-8 s gap.
    Minimum unit = 3×4 + 4 = 16 s.  Maximum unit = 5×4 + 8 = 28 s.
    Average unit ≈ 20 s.
    """
    if total_duration < 16:
        raise ValueError(f"Voiceover too short ({total_duration:.1f}s) — minimum is 16 s.")

    # Estimate group count, then fine-tune
    avg_unit   = (config.GROUP_MIN_CLIPS + config.GROUP_MAX_CLIPS) / 2 * config.CLIP_DURATION \
                 + (config.GAP_MIN_SEC + config.GAP_MAX_SEC) / 2
    n_groups   = max(1, round(total_duration / avg_unit))

    # Ensure we won't need more clips than are available
    max_clips_needed = n_groups * config.GROUP_MAX_CLIPS
    if max_clips_needed > available_clip_count:
        n_groups = max(1, available_clip_count // config.GROUP_MAX_CLIPS)

    groups: list[GroupPlan] = []
    cursor = 0.0

    for i in range(n_groups):
        is_last    = (i == n_groups - 1)
        remaining  = total_duration - cursor

        if is_last:
            # Fill exactly the remaining time.
            # Prefer a gap at the end so the final face-cam cut feels natural.
            # Solve: clip_count × 4 + gap = remaining
            # Clamp clip_count to [3,5], then derive gap.
            ideal_clips = (remaining - config.GAP_MIN_SEC) / config.CLIP_DURATION
            clip_count  = int(max(config.GROUP_MIN_CLIPS,
                                  min(config.GROUP_MAX_CLIPS, round(ideal_clips))))
            gap = remaining - clip_count * config.CLIP_DURATION
            # If gap falls outside [4,8], adjust clip_count by ±1 if possible
            if gap < config.GAP_MIN_SEC and clip_count > config.GROUP_MIN_CLIPS:
                clip_count -= 1
                gap = remaining - clip_count * config.CLIP_DURATION
            elif gap > config.GAP_MAX_SEC and clip_count < config.GROUP_MAX_CLIPS:
                clip_count += 1
                gap = remaining - clip_count * config.CLIP_DURATION
            # Hard-clamp gap to valid range as a last resort
            gap = max(config.GAP_MIN_SEC, min(config.GAP_MAX_SEC, gap))
        else:
            clip_count = random.randint(config.GROUP_MIN_CLIPS, config.GROUP_MAX_CLIPS)
            gap        = random.randint(config.GAP_MIN_SEC, config.GAP_MAX_SEC)

        unit_duration = clip_count * config.CLIP_DURATION + gap
        vo_end        = min(cursor + unit_duration, total_duration)

        groups.append(GroupPlan(
            index=i,
            clip_count=clip_count,
            gap_duration=round(gap, 1),
            vo_start=round(cursor, 2),
            vo_end=round(vo_end, 2),
        ))
        cursor = vo_end
        if cursor >= total_duration:
            break

    return groups


# ── Step 2: Map voiceover topic windows onto each group ───────────────────────

def annotate_groups_with_topics(
    groups: list[GroupPlan],
    topic_windows: list[dict],
    word_timestamps: list[dict],
) -> list[GroupPlan]:
    """
    For each group, find which topic window its midpoint falls in,
    then collect the actual voiceover words spoken during that group.
    """
    for g in groups:
        midpoint = (g.vo_start + g.vo_end) / 2

        # Find dominant topic
        g.vo_topic = "general"
        for tw in topic_windows:
            if tw["start"] <= midpoint < tw["end"]:
                g.vo_topic = tw["label"]
                break

        # Collect transcript words covering this group's time window
        words = [
            w["word"] for w in word_timestamps
            if w["start"] < g.vo_end and w["end"] > g.vo_start
        ]
        g.vo_text = " ".join(words).strip()

    return groups


# ── Step 3: Semantic pre-filter ────────────────────────────────────────────────

def _tag_overlap(clip: library.Clip, query_tokens: set[str]) -> int:
    return sum(1 for t in clip.tags if any(qt in t.lower() for qt in query_tokens))


def prefilter_clips(
    clips: list[library.Clip],
    group: GroupPlan,
    n: int = config.PREFILTER_TOP_N,
) -> list[library.Clip]:
    """
    Fast tag-overlap pre-filter to shrink the candidate pool before
    sending to Claude. Returns up to n most relevant clips for a group.
    """
    query_tokens = set(group.vo_topic.lower().split() +
                       [w.lower() for w in group.vo_text.split() if len(w) > 3])

    scored = sorted(clips, key=lambda c: _tag_overlap(c, query_tokens), reverse=True)
    return scored[:n]


# ── Step 4: Claude assigns clips to groups ────────────────────────────────────

SCHEDULE_SYSTEM = """You are a video editor selecting b-roll clips for a single group.

Rules — follow exactly:
1. Select EXACTLY clip_count clips from the candidates list.
2. Every id you return must come from the candidates list — do not invent IDs.
3. Choose clips that are visually and thematically coherent with the voiceover topic.

Respond ONLY with valid JSON (no markdown):
{"clip_ids": ["id1", "id2", "id3"]}"""


def _assign_group(
    client: openai.OpenAI,
    group: GroupPlan,
    candidates: list[library.Clip],
    max_retries: int,
) -> list[str]:
    """Ask OpenAI to select clips for a single group. Returns real clip IDs."""
    # Map real IDs to simple sequential IDs so the model can't hallucinate them
    id_map = {f"c{i}": c.id for i, c in enumerate(candidates)}
    valid_simple = set(id_map)

    payload = json.dumps({
        "clip_count":     group.clip_count,
        "vo_topic":       group.vo_topic,
        "vo_text_sample": group.vo_text[:200],
        "candidates": [
            {"id": f"c{i}", "description": c.description, "tags": c.tags}
            for i, c in enumerate(candidates)
        ],
    })

    for attempt in range(max_retries):
        response = client.chat.completions.create(
            model=config.REASONING_MODEL,
            max_completion_tokens=256,
            messages=[
                {"role": "system", "content": SCHEDULE_SYSTEM},
                {"role": "user", "content": payload},
            ],
        )

        raw = response.choices[0].message.content.strip()
        raw = re.sub(r"^```json\s*|```$", "", raw, flags=re.MULTILINE).strip()

        try:
            simple_ids = json.loads(raw)["clip_ids"]

            if not set(simple_ids).issubset(valid_simple):
                unknown = set(simple_ids) - valid_simple
                print(f"  ⚠ Group {group.index} attempt {attempt+1}: unknown IDs {unknown}. Retrying...")
                continue

            real_ids = [id_map[s] for s in simple_ids]

            if len(real_ids) != group.clip_count:
                print(f"  ⚠ Group {group.index} attempt {attempt+1}: got {len(real_ids)} clips "
                      f"(need {group.clip_count}). Retrying...")
                continue

            if len(set(real_ids)) != len(real_ids):
                print(f"  ⚠ Group {group.index} attempt {attempt+1}: duplicate IDs. Retrying...")
                continue

            return real_ids

        except (json.JSONDecodeError, KeyError) as e:
            print(f"  ⚠ Group {group.index} attempt {attempt+1}: parse error — {e}")

    raise RuntimeError(f"Failed to assign group {group.index} after {max_retries} attempts.")


def assign_clips_with_openai(
    groups: list[GroupPlan],
    all_available: list[library.Clip],
    max_retries: int = 3,
) -> list[dict]:
    """
    Assign clips one group at a time. Cross-group uniqueness is guaranteed
    structurally — used IDs are removed from the pool before each call.
    """
    client   = openai.OpenAI()
    used_ids: set[str] = set()
    results:  list[dict] = []

    for group in groups:
        pool = [c for c in all_available if c.id not in used_ids]
        candidates = prefilter_clips(pool, group)

        if len(candidates) < group.clip_count:
            raise RuntimeError(
                f"Group {group.index} needs {group.clip_count} clips but only "
                f"{len(candidates)} candidates remain."
            )

        real_ids = _assign_group(client, group, candidates, max_retries)
        used_ids.update(real_ids)
        results.append({"index": group.index, "clip_ids": real_ids})
        print(f"  Group {group.index:02d} ✓  [{group.vo_topic}]")

    return results


# ── Public entry point ─────────────────────────────────────────────────────────

def schedule(
    topic_windows: list[dict],
    word_timestamps: list[dict],
    total_duration: float,
    used_clip_ids: set[str] | None = None,
) -> list[ScheduledGroup]:
    """
    Full scheduling pipeline:
      1. Plan structural timeline (math)
      2. Annotate groups with voiceover topics
      3. Pre-filter candidate clips per group
      4. Claude assigns clips (with retry)
      5. Return resolved ScheduledGroup list

    used_clip_ids: IDs already consumed in this video (passed in and updated
                   in-place as the schedule is built, preventing within-video repeats).
    """
    used_clip_ids = used_clip_ids if used_clip_ids is not None else set()

    # 1. Fetch all available clips excluding already-used ones
    all_clips = library.get_all_tagged_clips(exclude_ids=used_clip_ids)
    if not all_clips:
        raise RuntimeError("No tagged clips in library. Run `ingest` first.")

    print(f"  Library: {len(all_clips)} available clips (excluding {len(used_clip_ids)} already used)")

    # 2. Plan structural timeline
    groups = plan_timeline(total_duration, len(all_clips))
    print(f"  Planned {len(groups)} clip groups to cover {total_duration:.1f}s voiceover:")
    for g in groups:
        print(f"    Group {g.index:02d}: {g.clip_count} clips × 4s + {g.gap_duration}s gap "
              f"  [{g.vo_start:.1f}s – {g.vo_end:.1f}s]")

    # 3. Annotate with topics
    groups = annotate_groups_with_topics(groups, topic_windows, word_timestamps)

    # 4. OpenAI assigns clips (one group at a time)
    print(f"  Asking OpenAI to assign clips ({len(groups)} groups)...")
    raw_assignments = assign_clips_with_openai(groups, all_clips)

    # 5. Build resolved ScheduledGroup list
    clip_map = {c.id: c for c in library.get_all_tagged_clips()}
    scheduled: list[ScheduledGroup] = []

    for entry in raw_assignments:
        g = next(x for x in groups if x.index == entry["index"])
        ids = entry["clip_ids"]

        scheduled.append(ScheduledGroup(
            index=g.index,
            clip_ids=ids,
            clip_paths=[clip_map[cid].clip_path for cid in ids],
            gap_duration=g.gap_duration,
            vo_start=g.vo_start,
            vo_end=g.vo_end,
            vo_topic=g.vo_topic,
        ))
        used_clip_ids.update(ids)

    scheduled.sort(key=lambda s: s.index)
    return scheduled
