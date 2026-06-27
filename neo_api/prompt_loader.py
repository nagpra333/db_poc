from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(file_name: str) -> str:
    return (PROMPTS_DIR / file_name).read_text(encoding="utf-8")