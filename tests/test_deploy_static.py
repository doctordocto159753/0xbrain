"""Static checks for the deployment layer (no Docker needed).

These guard the properties the deployment promises: shell scripts parse, the
model-free rule holds, nothing in the deploy files can leak an instance prefix
through instantiate.py, compose interpolates, and the image stays runtime-only.
"""
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = [ROOT / "install.sh", ROOT / "deploy/lib.sh", ROOT / "deploy/entrypoint.sh",
         *sorted((ROOT / "scripts").glob("*.sh"))]
DEPLOY_TEXT = [ROOT / "Dockerfile", ROOT / "compose.yaml", ROOT / "Caddyfile",
               ROOT / ".env.example", *SHELL, *sorted((ROOT / "docs").glob("*.md"))]
MODEL_WORDS = re.compile(r"\b(torch|faster-whisper|whisper|rapidocr|onnxruntime|transformers|"
                         r"sentence-transformers|openai|llama-cpp-python)\b", re.I)


class DeployStatic(unittest.TestCase):
    def test_shell_scripts_parse(self):
        for f in SHELL:
            r = subprocess.run(["bash", "-n", str(f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{f}: {r.stderr}")

    def test_linux_parity_scripts_exist(self):
        for n in ("setup-after-clone", "verify-install", "refresh-search", "configure-search", "create-backup"):
            self.assertTrue((ROOT / "scripts" / f"{n}.sh").is_file(), n)
            self.assertTrue((ROOT / "scripts" / f"{n}.ps1").is_file(), f"{n}.ps1 must not be deleted")

    def test_no_embedding_or_semantic_qmd_calls(self):
        # Comments may mention the forbidden commands; executable lines may not.
        bad = re.compile(r"\bqmd\s+(embed|vsearch)\b|\bqmd\s+query\s+(?!['\"]?lex:)")
        for f in SHELL:
            for i, line in enumerate(f.read_text().splitlines(), 1):
                code = line.split("#", 1)[0] if not line.lstrip().startswith("echo") else ""
                self.assertIsNone(bad.search(code), f"{f.name}:{i}: {line}")

    def test_image_is_runtime_only(self):
        text = (ROOT / "Dockerfile").read_text() + (ROOT / "deploy/requirements-image.txt").read_text()
        self.assertIsNone(MODEL_WORDS.search(re.sub(r"#.*", "", text)), "model/STT/OCR/LLM package in image")
        self.assertNotIn("run_faithfulness_benchmark", text)
        self.assertNotIn(".gguf", text)

    def test_no_forbidden_services(self):
        c = (ROOT / "compose.yaml").read_text()
        for svc in ("postgres", "redis", "qdrant", "chroma", "weaviate", "rabbitmq", "ollama"):
            self.assertNotIn(svc, c.lower())
        self.assertEqual(set(re.findall(r"^  (\w+):$", c.split("\nservices:")[1], re.M)),
                         {"brain", "caddy"})   # no external IdP: auth is built into brain

    def test_no_instance_prefix_leakage(self):
        # instantiate.py rewrites "mozare" and "mw-<kind>-" in the files it globs.
        for f in DEPLOY_TEXT:
            t = f.read_text()
            self.assertNotRegex(t, r"(?i)mozare", str(f))
            self.assertNotRegex(t, r"\bmw-(src|cap|corpus)-", str(f))

    def test_secrets_are_ignored_by_git(self):
        gi = (ROOT / ".gitignore").read_text()
        self.assertIn(".env", gi.splitlines())
        self.assertIn("!.env.example", gi.splitlines())
        ex = (ROOT / ".env.example").read_text()
        self.assertNotRegex(ex, r"(?m)^BRAIN_OWNER_SECRET=[0-9a-f]{16,}")

    def test_single_auth_configuration_scheme(self):
        """Agent A's runtime is the only auth scheme: G's provisional variables
        and the external-IdP seam are gone from every deploy file."""
        blob = "".join(f.read_text() for f in DEPLOY_TEXT)
        for gone in ("BRAIN_SECRET_KEY", "BRAIN_OWNER_SETUP_TOKEN", "BRAIN_AUTH_IMAGE",
                     "BRAIN_AUTH_DOMAIN", "BRAIN_SERVER_CMD", "auth-idp"):
            self.assertNotIn(gone.lower(), blob.lower(), gone)
        compose = (ROOT / "compose.yaml").read_text()
        for need in ("BRAIN_STATE_DIR: /state/auth", "BRAIN_REMOTE_SESSION: \"1\"",
                     "BRAIN_MCP_ADAPTER: semantic", "WIKI_QMD_HOME: /state/qmd"):
            self.assertIn(need, compose)
        self.assertIn("BRAIN_OWNER_SECRET", (ROOT / "install.sh").read_text())

    def test_no_rejected_stack_is_installed(self):
        deps = "".join((ROOT / f).read_text() for f in ("requirements.txt", "requirements-remote.txt",
                                                         "deploy/requirements-image.txt", "Dockerfile",
                                                         "compose.yaml"))
        for gone in ("fastmcp", "supergateway", "pocket-id", "pocketid"):
            self.assertNotIn(gone, deps.lower())

    def test_entrypoint_starts_real_server_and_stub_only_on_explicit_opt_in(self):
        ep = (ROOT / "deploy/entrypoint.sh").read_text()
        self.assertIn('exec python "$WIKI/scripts/remote_mcp/server.py"', ep)
        stub_line = next(i for i, l in enumerate(ep.splitlines()) if "stub_server.py" in l)
        guard = ep.splitlines()[stub_line - 2]
        self.assertIn('BRAIN_ALLOW_STUB:-0}" = "1"', guard)
        self.assertEqual(ep.count("stub_server.py"), 1)

    def test_image_installs_remote_runtime(self):
        df = (ROOT / "Dockerfile").read_text()
        self.assertIn("requirements-remote.txt", df)
        self.assertIn("mcp==2.2.0", (ROOT / "requirements-remote.txt").read_text())
        self.assertIn("!requirements-remote.txt", (ROOT / ".dockerignore").read_text())

    @unittest.skipUnless(shutil.which("docker"), "docker CLI not installed")
    def test_compose_interpolates(self):
        env = dict(os.environ, BRAIN_UID="1", BRAIN_GID="1", BRAIN_DATA_DIR="/w", BRAIN_STATE_DIR="/s",
                   BRAIN_DOMAIN="b.example", BRAIN_OWNER_EMAIL="a@b.example")
        tmp = ROOT / ".env"
        made = not tmp.exists()
        if made:
            tmp.write_text("")
        try:
            r = subprocess.run(["docker", "compose", "-f", str(ROOT / "compose.yaml"), "config", "-q"],
                               capture_output=True, text=True, env=env, cwd=ROOT)
            if "compose" in r.stderr and "not a docker command" in r.stderr:
                self.skipTest("docker compose plugin missing")
            self.assertEqual(r.returncode, 0, r.stderr)
        finally:
            if made:
                tmp.unlink()


if __name__ == "__main__":
    unittest.main()
