from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(file_name: str) -> str:
    return (PROMPTS_DIR / file_name).read_text(encoding="utf-8")

def get_cosmos_prompt(operation):

    if operation == "write":

        filename = "prompts/cosmos_write_prompt.txt"

    else:

        filename = "prompts/cosmos_read_prompt.txt"

    with open(filename,"r",encoding="utf-8") as f:

        return f.read()