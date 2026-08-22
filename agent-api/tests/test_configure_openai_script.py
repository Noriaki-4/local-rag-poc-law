import os
import stat
import subprocess
from pathlib import Path


def test_configure_openai_script_updates_env_without_printing_key(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_PROVIDER=anthropic\n"
        "LLM_MODEL=claude-haiku-old\n"
        "OPENAI_API_KEY=old-key\n"
        "OPENAI_API_KEY=duplicate-old-key\n"
        "ANSWER_MODEL=claude-haiku-old\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["OPENAI_ENV_FILE"] = str(env_file)
    script = Path(__file__).parents[2] / "scripts" / "configure_openai.sh"

    completed = subprocess.run(
        ["bash", str(script)],
        input="sk-test-secret\n",
        capture_output=True,
        check=True,
        text=True,
        env=environment,
    )

    content = env_file.read_text(encoding="utf-8")
    assert "LLM_PROVIDER=openai" in content
    assert "LLM_MODEL=gpt-4o-mini" in content
    assert "OPENAI_API_KEY=sk-test-secret" in content
    assert content.count("OPENAI_API_KEY=") == 1
    assert "ANSWER_MODEL=claude-haiku-old" in content
    assert "sk-test-secret" not in completed.stdout
    assert "sk-test-secret" not in completed.stderr
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
