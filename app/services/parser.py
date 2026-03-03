"""
Parser for visual-plan.md files.

Extracts structured shot data, character designs, and prompts from the
episode visual plan format used in the Gorb & Pleck production pipeline.
"""
import re
from dataclasses import dataclass, field


@dataclass
class ParsedCharacter:
    name: str
    description: str
    prompt: str = ""


@dataclass
class ParsedShot:
    number: int
    name: str
    segment: str
    shot_type: str           # "still", "veo3_clip", "title_card", "reuse", "graphic", "bumper"
    nano_prompt: str = ""
    veo3_prompt: str = ""
    dialogue: str = ""
    direction_notes: str = ""
    character_refs: list = field(default_factory=list)
    duration: str = ""
    camera_notes: str = ""


@dataclass
class ParsedVisualPlan:
    title: str = ""
    location: str = ""
    characters: list[ParsedCharacter] = field(default_factory=list)
    shots: list[ParsedShot] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def parse_visual_plan(markdown: str) -> ParsedVisualPlan:
    """Parse a visual-plan.md into structured data."""
    result = ParsedVisualPlan()

    # Extract title from first H1
    title_match = re.search(r'^#\s+(.+?)(?:\s*—\s*Visual Plan)?$', markdown, re.MULTILINE)
    if title_match:
        raw_title = title_match.group(1).strip()
        # Clean "Episode XX: Title" format
        ep_match = re.match(r'Episode\s+\d+:\s*(.+)', raw_title)
        result.title = ep_match.group(1) if ep_match else raw_title

    # Extract location from "Location Visual Identity" section
    loc_match = re.search(
        r'## Location Visual Identity\s*\n(.+?)(?=\n##|\Z)',
        markdown, re.DOTALL
    )
    if loc_match:
        first_line = loc_match.group(1).strip().split('\n')[0]
        # Often the first line describes the location
        result.location = first_line.split('—')[0].strip().rstrip('.')

    # Parse bystander characters
    result.characters = _parse_characters(markdown)

    # Parse all shots
    result.shots = _parse_shots(markdown)

    return result


def _parse_characters(markdown: str) -> list[ParsedCharacter]:
    """Extract bystander character designs from the visual plan."""
    characters = []

    # Find the character designs section
    char_section = re.search(
        r'## Bystander Character Designs\s*\n(.+?)(?=\n---|\n## (?!#))',
        markdown, re.DOTALL
    )
    if not char_section:
        return characters

    text = char_section.group(1)

    # Split on H3 headers (### Character Name)
    # Strip leading ### if the section starts with one (no preceding \n)
    text = re.sub(r'^### ', '', text.strip())
    char_blocks = re.split(r'\n### ', text)
    for block in char_blocks:  # All blocks are now character entries
        lines = block.strip().split('\n')
        name = lines[0].strip().split('(')[0].strip()
        description = '\n'.join(lines[1:]).strip()

        characters.append(ParsedCharacter(
            name=name,
            description=description,
        ))

    return characters


def _parse_shots(markdown: str) -> list[ParsedShot]:
    """Extract all shots from the visual plan."""
    shots = []
    current_segment = "Intro"

    # Find segment headers to track which segment we're in
    # Segments are marked by ## headers like "## INTRO", "## STORY SEGMENT 1: ..."
    # Shots are marked by ### headers like "### Shot 1: Location Reveal"

    # Split the document into sections by ## headers
    sections = re.split(r'\n## ', markdown)

    for section in sections:
        # Determine segment name from section header
        section_header = section.split('\n')[0].strip()

        if re.match(r'INTRO', section_header, re.IGNORECASE):
            current_segment = "Intro"
        elif re.match(r'STORY SEGMENT\s*(\d+)', section_header, re.IGNORECASE):
            seg_match = re.match(r'STORY SEGMENT\s*(\d+)[:\s]*(.+)?', section_header, re.IGNORECASE)
            if seg_match:
                seg_num = seg_match.group(1)
                seg_title = seg_match.group(2).strip('" ').strip() if seg_match.group(2) else ""
                current_segment = f"Segment {seg_num}" + (f": {seg_title}" if seg_title else "")
        elif re.match(r'REVIEW', section_header, re.IGNORECASE):
            current_segment = "Review"
        elif re.match(r'CLOSING', section_header, re.IGNORECASE):
            current_segment = "Closing"
        elif re.match(r'POST-CREDITS?', section_header, re.IGNORECASE):
            current_segment = "Post-Credits"

        # Now find individual shots within this section
        shot_blocks = re.split(r'\n### ', section)
        for block in shot_blocks[1:]:
            parsed = _parse_single_shot(block, current_segment)
            shots.extend(parsed)

    return shots


def _parse_single_shot(block: str, segment: str) -> list[ParsedShot]:
    """Parse a shot block into one or more ParsedShots (handles ranges like 'Shots 6-9')."""
    lines = block.strip().split('\n')
    header = lines[0].strip()

    # Extract shot number (and optional end number) and name
    # Formats: "Shot 1: Location Reveal", "Shots 6-9: Evidence Stills",
    #          "Shot 31: Probe Rating Reveal"
    shot_match = re.match(r'Shots?\s+(\d+)(?:-(\d+))?[:\s]*(.+)?', header)
    if not shot_match:
        return []

    start_num = int(shot_match.group(1))
    end_num = int(shot_match.group(2)) if shot_match.group(2) else None
    group_name = shot_match.group(3).strip() if shot_match.group(3) else f"Shot {start_num}"

    full_text = '\n'.join(lines[1:])

    # If it's a range, try to split into individual sub-shots
    if end_num and end_num > start_num:
        sub_shots = _expand_shot_range(start_num, end_num, group_name, full_text, segment)
        if sub_shots:
            return sub_shots

    # Single shot (or range that couldn't be split — treat as one)
    return [_build_shot(start_num, group_name, full_text, segment)]


def _expand_shot_range(start: int, end: int, group_name: str,
                       full_text: str, segment: str) -> list[ParsedShot]:
    """Expand a shot range (e.g. Shots 6-9) into individual ParsedShots.

    Looks for sub-shot markers like:
      **Still N — Name:**  (evidence stills with individual prompts)
      - Shot N: Description  (reuse lists)
    """
    shots = []

    # Pattern 1: Individual stills with their own code blocks
    # e.g. **Still 7 — Gorb at the Captain's Table, holding court:**
    still_pattern = re.compile(
        r'\*\*Still\s+(\d+)\s*[—\-]\s*(.+?):\*\*',
        re.IGNORECASE,
    )
    still_matches = list(still_pattern.finditer(full_text))

    if still_matches:
        # Split the text at each still marker to get individual blocks
        for i, m in enumerate(still_matches):
            num = int(m.group(1))
            sub_name = m.group(2).strip()
            # Get the text from this marker to the next (or end)
            block_start = m.start()
            block_end = still_matches[i + 1].start() if i + 1 < len(still_matches) else len(full_text)
            sub_text = full_text[block_start:block_end]

            # Extract the code block (nano prompt) from this sub-section
            code_match = re.search(r'```\s*\n(.+?)\n```', sub_text, re.DOTALL)
            nano_prompt = code_match.group(1).strip() if code_match else ""

            # Extract direction notes (blockquotes) from this sub-section
            direction = '\n'.join(
                line.lstrip('> ').strip()
                for line in sub_text.split('\n')
                if line.strip().startswith('>')
            )

            shots.append(ParsedShot(
                number=num,
                name=sub_name,
                segment=segment,
                shot_type="still",
                nano_prompt=nano_prompt,
                direction_notes=direction,
            ))
        return shots

    # Pattern 2: Reuse list items
    # e.g. - Shot 23: Reuse Still 7 — Gorb at the Captain's Table
    reuse_pattern = re.compile(
        r'^-\s+Shot\s+(\d+):\s*(.+)',
        re.MULTILINE | re.IGNORECASE,
    )
    reuse_matches = list(reuse_pattern.finditer(full_text))

    if reuse_matches:
        shot_type = _detect_shot_type(full_text, group_name)
        for m in reuse_matches:
            num = int(m.group(1))
            sub_name = m.group(2).strip()
            shots.append(ParsedShot(
                number=num,
                name=sub_name,
                segment=segment,
                shot_type=shot_type,
            ))
        return shots

    # Fallback: create individual shots for each number in range, all sharing the same prompt
    return []


def _build_shot(number: int, name: str, full_text: str, segment: str) -> ParsedShot:
    """Build a single ParsedShot from block text."""
    shot_type = _detect_shot_type(full_text, name)

    # Extract code blocks (prompts)
    code_blocks = re.findall(r'```\s*\n(.+?)\n```', full_text, re.DOTALL)

    nano_prompt = ""
    veo3_prompt = ""

    # Assign prompts based on context
    for i, cb in enumerate(code_blocks):
        before = full_text[:full_text.find(cb)]
        if 'veo3 prompt' in before.lower() or 'veo 3 prompt' in before.lower():
            veo3_prompt = cb.strip()
        elif 'nano banana' in before.lower() or 'start frame' in before.lower() or 'still' in before.lower():
            if not nano_prompt:
                nano_prompt = cb.strip()
            elif not veo3_prompt:
                veo3_prompt = cb.strip()
        else:
            if not nano_prompt:
                nano_prompt = cb.strip()
            elif not veo3_prompt:
                veo3_prompt = cb.strip()

    dialogue = _extract_dialogue(full_text)

    direction = '\n'.join(
        line.lstrip('> ').strip()
        for line in full_text.split('\n')
        if line.strip().startswith('>')
    )

    duration = ""
    dur_match = re.search(r'\*\*Duration:\*\*\s*(.+)', full_text)
    if dur_match:
        duration = dur_match.group(1).strip()
    else:
        dur_match = re.search(r'Duration[:\s]+([~\d\-\s]+seconds?)', full_text, re.IGNORECASE)
        if dur_match:
            duration = dur_match.group(1).strip()

    camera = ""
    cam_match = re.search(r'\*\*Camera:\*\*\s*(.+)', full_text)
    if cam_match:
        camera = cam_match.group(1).strip()

    char_refs = _detect_character_refs(nano_prompt + " " + veo3_prompt + " " + full_text)

    return ParsedShot(
        number=number,
        name=name,
        segment=segment,
        shot_type=shot_type,
        nano_prompt=nano_prompt,
        veo3_prompt=veo3_prompt,
        dialogue=dialogue,
        direction_notes=direction,
        character_refs=char_refs,
        duration=duration,
        camera_notes=camera,
    )


def _detect_shot_type(text: str, name: str) -> str:
    """Determine shot type from context clues."""
    text_lower = text.lower()
    name_lower = name.lower()

    if 'title card' in name_lower or 'title card' in text_lower:
        return "title_card"
    if 'bumper' in name_lower or 'bumper' in text_lower:
        return "bumper"
    if 'probe rating' in name_lower or 'graphic overlay' in text_lower:
        return "graphic"
    if 'reuse' in text_lower and ('reused' in text_lower or 'no new generation' in text_lower):
        return "reuse"
    if 'veo3 clip' in text_lower or 'veo3 prompt' in text_lower or 'veo 3 prompt' in text_lower:
        return "veo3_clip"
    if 'evidence stills' in name_lower or 'highlight stills' in name_lower or 'reality stills' in name_lower:
        return "still"
    if '**type:** still' in text_lower or 'nano banana still' in text_lower:
        return "still"
    if '**type:** veo3' in text_lower:
        return "veo3_clip"

    # Default: if it has a veo3 prompt, it's a clip; if only nano, it's a still
    if re.search(r'veo3?\s+prompt', text, re.IGNORECASE):
        return "veo3_clip"

    return "still"


def _extract_dialogue(text: str) -> str:
    """Extract dialogue lines from shot text."""
    lines = []

    # Match dialogue patterns:
    # **Dialogue:** "text"
    # - Gorb: "text"
    # **VO Dialogue:**
    for match in re.finditer(
        r'(?:\*\*(?:Dialogue|VO Dialogue|VO):\*\*\s*(.+?)(?=\n\n|\n\*\*|\n---|\Z))',
        text, re.DOTALL
    ):
        lines.append(match.group(1).strip())

    # Also catch individual character lines
    for match in re.finditer(r'^-\s+(Gorb|Pleck):\s*"(.+?)"', text, re.MULTILINE):
        pass  # These are typically within the VO Dialogue block already

    if lines:
        return '\n'.join(lines)

    # Fallback: look for standalone **Dialogue:** on its own line
    dial_match = re.search(r'\*\*Dialogue:\*\*\s*(.+?)(?=\n\n|\n>|\n\*\*|\Z)', text, re.DOTALL)
    if dial_match:
        return dial_match.group(1).strip()

    # Check for "(none)" explicitly
    if re.search(r'\*\*Dialogue:\*\*\s*\*?\(none\)', text, re.IGNORECASE):
        return "(none)"

    return ""


def _detect_character_refs(text: str) -> list[str]:
    """Detect which characters are referenced in prompts."""
    refs = []
    text_lower = text.lower()

    if 'gorb' in text_lower:
        refs.append("Gorb")
    if 'pleck' in text_lower:
        refs.append("Pleck")

    # Detect bystander references by common role titles
    bystander_patterns = [
        r'cruise director', r'lounge attendant', r'entertainment director',
        r'karaoke attendee', r'tour guide', r'front desk',
        r'bartender', r'concierge', r'spa attendant',
    ]
    for pattern in bystander_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            # Capitalize for consistency
            refs.append(pattern.replace(r'\s', ' ').title())

    return refs
