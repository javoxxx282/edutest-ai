import json
import re
import os
from google import genai
from google.genai import types

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))

QUIZ_PROMPT = """
Siz o'zbek tilida test savollari tayyorlaydigan mutaxasssissiz.

Quyidagi matn asosida {count} ta test savoli tayyorlang.

Qoidalar:
- Har bir savol faqat matn asosida bo'lsin
- Har bir savolda 4 ta javob varianti bo'lsin (A, B, C, D)
- Faqat bitta to'g'ri javob bo'lsin
- Savollar va javoblar O'ZBEK tilida bo'lsin
- JSON formatida qaytaring

JSON format:
[
  {{
    "question": "Savol matni?",
    "options": {{
      "A": "Birinchi variant",
      "B": "Ikkinchi variant",
      "C": "Uchinchi variant",
      "D": "To'rtinchi variant"
    }},
    "correct": "A",
    "explanation": "To'g'ri javob izohlanishi"
  }}
]

Matn:
{text}
"""


def generate_quiz(text: str, count: int = 10) -> list[dict]:
    char_limit = min(max(count * 200, 8000), 40000)
    truncated_text = text[:char_limit] if len(text) > char_limit else text
    prompt = QUIZ_PROMPT.format(count=count, text=truncated_text)

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            max_output_tokens=8192,
            temperature=0.7,
        ),
    )

    raw = response.text.strip()

    json_match = re.search(r"\[.*\]", raw, re.DOTALL)
    if json_match:
        raw = json_match.group(0)

    questions = json.loads(raw)

    validated = []
    for q in questions:
        if (
            isinstance(q, dict)
            and "question" in q
            and "options" in q
            and "correct" in q
            and isinstance(q["options"], dict)
            and len(q["options"]) == 4
            and q["correct"] in q["options"]
        ):
            validated.append(q)

    return validated
