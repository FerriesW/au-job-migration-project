"""Prompt templates for Qwen-driven extraction."""

from __future__ import annotations

import json
from typing import Final

SYSTEM_PROMPT: Final[str] = (
    "You extract structured signals from Australian job postings. Respond with "
    "exactly one JSON object that matches the schema below. Output JSON only, "
    "with no preamble, commentary, or trailing prose.\n"
    "\n"
    "Schema:\n"
    "{\n"
    '  "required_skills": [<string>],            // Up to 20 technical skills, each <= 40 chars.\n'
    "                                            //   Include ONLY specific tools / technologies /\n"
    "                                            //   frameworks / libraries / named certifications\n"
    "                                            //   that appear verbatim in the description text.\n"
    "                                            //   EXCLUDE all of the following:\n"
    '                                            //     - soft skills ("communication", "leadership")\n'
    '                                            //     - role descriptors ("data center",\n'
    '                                            //       "infrastructure", "support", "analysis")\n'
    "                                            //     - skills typically associated with the\n"
    "                                            //       role title but not actually mentioned.\n"
    "                                            //       Do NOT add Python / SQL just because the\n"
    "                                            //       role is data-related; only include them\n"
    "                                            //       if the description names them explicitly.\n"
    '  "years_experience": <int|null>,           // Minimum years required. Use the lower bound for\n'
    "                                            //   ranges. null when not stated.\n"
    '  "sponsorship_signal": "explicit_yes" | "explicit_no" | "unspecified",\n'
    "                                            //   explicit_yes: text welcomes visa sponsorship.\n"
    "                                            //   explicit_no:  role restricted to AU citizens or\n"
    "                                            //                 permanent residents.\n"
    "                                            //   unspecified:  not addressed in the text.\n"
    "                                            //   Note: 'certification sponsorship', 'training\n"
    "                                            //   sponsorship', or similar professional-development\n"
    "                                            //   benefits are NOT visa sponsorship signals.\n"
    '  "local_experience_required": true | false,\n'
    "                                            //   true ONLY when the text states a candidate\n"
    "                                            //   requirement for prior Australian or local\n"
    "                                            //   work experience (e.g. 'Australian experience\n"
    "                                            //   required', 'must have local experience',\n"
    "                                            //   'AU work history preferred').\n"
    "                                            //   The fact that the role itself is based in or\n"
    "                                            //   located in Australia is a LOCATION attribute,\n"
    "                                            //   NOT a candidate-experience requirement, and\n"
    "                                            //   must NOT trigger true.\n"
    '  "remote_friendly": "remote" | "hybrid" | "onsite" | "unspecified"\n'
    "                                            //   remote: fully remote / work from anywhere.\n"
    "                                            //   hybrid: split between office and remote.\n"
    "                                            //   onsite: office attendance required.\n"
    "                                            //   unspecified: not stated.\n"
    "}\n"
    "\n"
    "Conservative inference: when the text does not clearly state a value, return "
    "the unspecified / null / false default rather than guessing."
)


_FEW_SHOT_EXAMPLES: Final[list[dict]] = [
    {
        "description": (
            "Senior Python Developer based in Sydney, hybrid working arrangement "
            "(3 days in office). 5+ years experience required. Must have Australian "
            "working rights (citizen or permanent resident). Skills: Python, Django, "
            "AWS, PostgreSQL, Docker."
        ),
        "expected": {
            "required_skills": ["Python", "Django", "AWS", "PostgreSQL", "Docker"],
            "years_experience": 5,
            "sponsorship_signal": "explicit_no",
            "local_experience_required": False,
            "remote_friendly": "hybrid",
        },
    },
    {
        "description": (
            "Data Engineer — fully remote position. Visa sponsorship is available "
            "for the right candidate. Looking for hands-on experience with Snowflake, "
            "dbt, Airflow, Python, and SQL."
        ),
        "expected": {
            "required_skills": ["Snowflake", "dbt", "Airflow", "Python", "SQL"],
            "years_experience": None,
            "sponsorship_signal": "explicit_yes",
            "local_experience_required": False,
            "remote_friendly": "remote",
        },
    },
    {
        # Negative example: header-only marketing copy with no extractable
        # specifics. Teaches the model to return defaults rather than copy
        # skills, work-mode, or seniority signals from earlier examples.
        "description": (
            "Marketing Manager | Sydney. Our client is a leading consumer brand "
            "looking to grow their team. Excellent benefits and career progression "
            "on offer. Apply now!"
        ),
        "expected": {
            "required_skills": [],
            "years_experience": None,
            "sponsorship_signal": "unspecified",
            "local_experience_required": False,
            "remote_friendly": "unspecified",
        },
    },
]


def build_messages(description: str) -> list[dict[str, str]]:
    """Construct the chat-completions messages array with few-shot turns prepended.

    Args:
        description: Job description text to extract from.

    Returns:
        Ordered list of role/content message dicts ready for the chat API.
    """
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for example in _FEW_SHOT_EXAMPLES:
        messages.append({
            "role": "user",
            "content": f"Description:\n{example['description']}",
        })
        messages.append({
            "role": "assistant",
            "content": json.dumps(example["expected"], ensure_ascii=False),
        })
    messages.append({
        "role": "user",
        "content": f"Description:\n{description}",
    })
    return messages
